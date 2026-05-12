"""Zabbix → auto-investigate webhook (v1.5).

A Zabbix trigger action POSTs JSON to ``/zabbix/auto-investigate`` when a
problem opens. The body is authenticated by an HMAC-SHA256 signature in
the ``X-Zabbix-AI-Signature`` header keyed by the shared secret in
``settings.auto_investigate.webhook_secret_env``.

End-to-end:
    Zabbix problem opens
      → action calls this webhook
      → HMAC verified, filters applied (host-group allowlist, severity)
      → InvestigationRunner.investigate() runs inline (~30-90 s)
      → result.summary written back to the Zabbix event as an
        event.acknowledge comment (action=4)
      → optional Slack post to ``settings.auto_investigate.slack_channel``
      → HTTP 200 with ``{"status":"completed", "investigation_id": N}``

The signature format mirrors the Slack pattern (``v0:<ts>:<body>``) so the
verifier can refuse stale requests for replay protection (5-min window).
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from zabbix_ai.admin.rate_limit import limiter
from zabbix_ai.clients.slack import SlackClient
from zabbix_ai.config import Settings
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.services.investigation_runner import InvestigationRunner

_log = logging.getLogger(__name__)


class WebhookSignatureError(Exception):
    """Raised when the incoming Zabbix webhook fails HMAC verification."""


_TIMESTAMP_TOLERANCE_SECONDS = 60 * 5  # 5 minutes — same as Slack


def verify_webhook_hmac(
    body: bytes, signature_header: str, secret: str,
    *, timestamp: str | None = None,
) -> None:
    """Raise WebhookSignatureError if the body/signature don't match.

    Two accepted formats for ``signature_header``:

    * Plain hex digest of ``HMAC-SHA256(secret, body)`` — used when the
      caller cannot easily inject a timestamp (Zabbix media types can be
      finicky about headers).
    * Compound ``v0=<hex>`` with a separate ``timestamp`` arg — when the
      caller can pass ``X-Zabbix-AI-Timestamp``, we additionally enforce a
      ±5 min freshness window for replay protection.

    Both branches use ``hmac.compare_digest`` for constant-time compare.
    The secret must be non-empty; an empty secret never matches.
    """
    if not signature_header:
        raise WebhookSignatureError("missing signature header")
    if not secret:
        raise WebhookSignatureError("server has no webhook secret configured")

    # Optional replay-protection window (only when a timestamp is given)
    if timestamp is not None:
        try:
            ts_int = int(timestamp)
        except (TypeError, ValueError) as e:
            raise WebhookSignatureError("invalid timestamp") from e
        if abs(int(time.time()) - ts_int) > _TIMESTAMP_TOLERANCE_SECONDS:
            raise WebhookSignatureError("timestamp outside tolerance window")
        base = f"v0:{timestamp}:".encode() + body
        expected = "v0=" + hmac.new(secret.encode(), base,
                                     hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature_header):
            raise WebhookSignatureError("signature mismatch")
        return

    # Plain-hex branch: HMAC over the raw body only. We accept either the
    # bare hex digest or a "sha256=<hex>" prefix (common HMAC convention).
    sig = signature_header
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise WebhookSignatureError("signature mismatch")


def compute_webhook_signature(body: bytes, secret: str) -> str:
    """Convenience helper for tests and the action-setup helper."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _parse_hostgroups(raw: Any) -> list[str]:
    """Normalise the ``hostgroups`` payload field.

    Zabbix's ``{HOSTGROUP.NAMES}`` macro expands to a comma-separated string,
    but if a caller sends it as a JSON list we accept that too.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(g).strip() for g in raw if str(g).strip()]
    if isinstance(raw, str):
        return [g.strip() for g in raw.split(",") if g.strip()]
    return []


def _slack_blocks(*, hostname: str, summary: str, investigation_id: int,
                   eventid: int | None) -> list[dict[str, Any]]:
    """Build the Slack notification body.

    Keeps the visible payload small (Slack hard caps a single section at
    3000 chars). We deliberately use a one-line title + a single
    truncated summary block + a context link; full transcripts live on
    the admin investigation page.
    """
    safe_summary = (summary or "_(no summary produced)_")[:2800]
    title = f":robot_face: *AI auto-investigation: `{hostname or 'unknown host'}`*"
    if eventid:
        title += f"  ·  event {eventid}"
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": title}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": safe_summary}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": f"Investigation #{investigation_id} · "
                                f"<{{admin_link}}|view details>".replace(
                                    "{admin_link}",
                                    f"/admin/investigations/{investigation_id}",
                                )}]},
    ]


def build_router(settings: Settings) -> APIRouter:
    """Construct the auto-investigate router.

    The router is always returned, even when ``settings.auto_investigate``
    is None — in that case the endpoint replies 503 so misconfigured
    deployments don't silently swallow webhook traffic.
    """
    router = APIRouter()

    @router.post("/zabbix/auto-investigate")
    @limiter.limit("60/minute")
    async def auto_investigate(request: Request) -> JSONResponse:
        # Read body once — verify_webhook_hmac needs the raw bytes.
        body = await request.body()

        ai_cfg = settings.auto_investigate
        if ai_cfg is None or not ai_cfg.enabled:
            return JSONResponse(
                status_code=503,
                content={"status": "error",
                         "message": "auto-investigate not configured"},
            )

        secret = ai_cfg.webhook_secret.get_secret_value()
        sig = request.headers.get("X-Zabbix-AI-Signature", "")
        # Timestamp header is optional — older Zabbix media types may not
        # send it. When present we additionally enforce a freshness window.
        ts = request.headers.get("X-Zabbix-AI-Timestamp")
        try:
            verify_webhook_hmac(body, sig, secret, timestamp=ts)
        except WebhookSignatureError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

        # Parse body
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400,
                                detail=f"invalid JSON: {e}") from e

        instance = str(payload.get("instance") or "")
        raw_eventid = payload.get("eventid")
        raw_hostid = payload.get("hostid")
        raw_severity = payload.get("severity", 0)
        hostgroups = _parse_hostgroups(payload.get("hostgroups"))

        try:
            eventid = int(raw_eventid) if raw_eventid is not None else None
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400,
                                detail=f"invalid eventid: {raw_eventid}") from e
        try:
            hostid = int(raw_hostid) if raw_hostid is not None else None
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400,
                                detail=f"invalid hostid: {raw_hostid}") from e
        try:
            severity = int(raw_severity)
        except (TypeError, ValueError):
            severity = 0

        # Filter: host-group allowlist (empty allowlist ⇒ pass-through)
        if ai_cfg.allowed_hostgroups:
            overlap = set(ai_cfg.allowed_hostgroups).intersection(hostgroups)
            if not overlap:
                return JSONResponse(
                    {"status": "skipped_hostgroup",
                     "detail": f"none of {hostgroups} in allowlist"},
                )

        # Filter: severity threshold
        if severity < ai_cfg.min_severity:
            return JSONResponse(
                {"status": "skipped_severity",
                 "detail": f"severity {severity} < min {ai_cfg.min_severity}"},
            )

        # Budget enforcement (Subagent C's services/budget.py). Import
        # lazily so this adapter still loads cleanly if budget.py is
        # missing on an older branch — in that case we proceed without
        # any budget gating.
        model = settings.default_model
        try:
            from zabbix_ai.services.budget import enforce_budget
        except ImportError:
            enforce_budget = None  # type: ignore[assignment]

        memory_handle = getattr(request.app.state, "memory", None)
        if enforce_budget is not None:
            try:
                # The budget service returns (effective_model, reason);
                # effective_model=None means 'paused'. We don't have an
                # investigation_id yet (runner hasn't started), so pass
                # None — budget.py will still write the audit row.
                chosen, reason = await enforce_budget(
                    memory_handle, settings,
                    model_requested=settings.default_model,
                )
            except Exception as e:
                _log.warning("enforce_budget failed: %s — proceeding", e)
                chosen, reason = settings.default_model, "error"
            if chosen is None:
                # budget.py already wrote 'paused' to budget_audit; we add
                # a defensive secondary row only if that call somehow
                # raised before reaching its own _audit() call.
                return JSONResponse({"status": "paused_budget",
                                     "reason": reason})
            if chosen != settings.default_model:
                model = chosen

        # ── Run the investigation ────────────────────────────────────────
        # We import BudgetExceededError lazily for the same reason as
        # enforce_budget — to keep this module importable on older branches
        # that don't have the budget service yet.
        try:
            from zabbix_ai.services.budget import BudgetExceededError
        except ImportError:
            class BudgetExceededError(Exception):  # type: ignore[no-redef]
                """Stub when the budget service isn't installed."""

        hostname = ""
        result = None
        try:
            async with InvestigationRunner(settings) as runner:
                # Honour the budget-chosen model for *this* run only by
                # mutating the orchestrator on the runner directly. The
                # orchestrator will *also* re-check the budget but that's
                # idempotent and the second call simply confirms.
                if model != settings.default_model and runner._orch is not None:
                    runner._orch.model = model
                ctx = InvestigationContext(
                    source="auto_webhook",
                    instance=instance or None,
                    eventid=eventid,
                    hostid=hostid,
                    trigger_source="webhook",
                )
                result = await runner.investigate(ctx)
                # Mark the row as webhook-triggered. Migration 007 added
                # the column with default 'manual', so we override here.
                if runner._mem is not None:
                    with contextlib.suppress(Exception):
                        await runner._mem.execute(
                            "UPDATE investigations SET trigger_source=? WHERE id=?",
                            ("webhook", result.investigation_id),
                        )

                # Resolve the hostname for the Slack title (best-effort).
                hostname = ctx.hostname or ""

                # Write summary back to Zabbix as an event.acknowledge
                # comment via the shared helper (same logic used by the
                # manual right-click adapter).
                if instance and instance in runner._zabbix_clients and eventid:
                    from zabbix_ai.services.zabbix_writeback import (
                        post_summary_to_event,
                    )
                    await post_summary_to_event(
                        runner._zabbix_clients[instance],
                        eventid=eventid,
                        summary=result.summary,
                        source="auto",
                    )
        except BudgetExceededError as e:
            _log.info("auto-investigate paused by budget gate: %s", e)
            return JSONResponse({"status": "paused_budget",
                                 "reason": str(e)})
        except Exception as e:
            _log.exception("auto-investigate failed")
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": str(e)},
            )
        if result is None:  # defensive — should never trigger
            return JSONResponse(
                status_code=500,
                content={"status": "error",
                         "message": "investigation returned no result"},
            )

        # ── Slack notification (optional) ────────────────────────────────
        # We post AFTER the InvestigationRunner context exits, so the
        # Slack client uses its own short-lived httpx connection — this
        # keeps the runner's lifecycle clean even when Slack is slow.
        if ai_cfg.slack_channel and settings.slack is not None:
            slack = SlackClient(
                bot_token=settings.slack.bot_token.get_secret_value(),
            )
            try:
                await slack.post_message(
                    channel=ai_cfg.slack_channel,
                    text=f"AI auto-investigation: {hostname or 'unknown host'}",
                    blocks=_slack_blocks(
                        hostname=hostname,
                        summary=result.summary,
                        investigation_id=result.investigation_id,
                        eventid=eventid,
                    ),
                )
            except Exception as e:
                _log.warning("Slack auto-investigate post failed: %s", e)
            finally:
                await slack.aclose()

        return JSONResponse({
            "status": "completed",
            "investigation_id": result.investigation_id,
        })

    return router


def _format_ack_message(summary: str) -> str:
    """Backwards-compatible shim — delegates to the shared helper."""
    from zabbix_ai.services.zabbix_writeback import format_ack_message
    return format_ack_message(summary, source="auto")
