from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from zabbix_ai.clients.slack import SlackClient
from zabbix_ai.config import Settings
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.renderers.slack import render_blocks, render_placeholder
from zabbix_ai.services.connection_health import record_health
from zabbix_ai.services.investigation_runner import InvestigationRunner


class SlackSignatureError(Exception):
    pass


_TIMESTAMP_TOLERANCE_SECONDS = 60 * 5  # 5 minutes per Slack docs

# ── event_id dedupe cache (#10) ───────────────────────────────────────────────
# LRU cache: keeps the most-recently-seen event IDs with their received timestamp.
# Slack retries up to 3x; we dedupe within the timestamp tolerance window.
_SEEN_EVENTS: OrderedDict[str, float] = OrderedDict()
_SEEN_EVENTS_MAX = 10_000
_DEDUPE_WINDOW_SECONDS = 60 * 10  # 10 minutes


def _check_event_seen(event_id: str) -> bool:
    """Return True if this event_id was already processed within the dedupe window."""
    now = time.time()
    # Prune expired entries
    expired = [k for k, v in _SEEN_EVENTS.items() if now - v > _DEDUPE_WINDOW_SECONDS]
    for k in expired:
        _SEEN_EVENTS.pop(k, None)

    if event_id in _SEEN_EVENTS:
        return True

    # Record new event
    _SEEN_EVENTS[event_id] = now
    if len(_SEEN_EVENTS) > _SEEN_EVENTS_MAX:
        _SEEN_EVENTS.popitem(last=False)  # evict oldest
    return False


def verify_slack_signature(body: bytes, timestamp: str, signature: str,
                           signing_secret: str) -> None:
    """Raise SlackSignatureError if the request is not authentic.

    Implements https://api.slack.com/authentication/verifying-requests-from-slack
    """
    if not signature or not timestamp:
        raise SlackSignatureError("missing signature or timestamp header")
    try:
        ts_int = int(timestamp)
    except ValueError as e:
        raise SlackSignatureError("non-integer timestamp") from e
    if abs(int(time.time()) - ts_int) > _TIMESTAMP_TOLERANCE_SECONDS:
        raise SlackSignatureError("timestamp outside tolerance window")
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base,
                                 hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise SlackSignatureError("signature mismatch")


_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_KEY_VALUE_RE = re.compile(r"(eventid|hostid|instance)\s*=\s*([A-Za-z0-9._-]+)",
                            re.IGNORECASE)
_PARENT_EVENTID_RE = re.compile(r"EventID[:\s]+(\d+)", re.IGNORECASE)
_PARENT_HOSTID_RE = re.compile(r"\((\d{2,})\)")  # "host-name (12345)"


@dataclass
class ParsedMention:
    question: str
    eventid: int | None = None
    hostid: int | None = None
    instance: str = ""


def parse_mention(*, text: str, parent_text: str | None,
                  default_instance: str,
                  known_instances: list[str] | None = None) -> ParsedMention:
    """Extract investigation context from a Slack mention."""
    cleaned = _MENTION_RE.sub("", text).strip()
    kv = {m.group(1).lower(): m.group(2) for m in _KEY_VALUE_RE.finditer(cleaned)}
    eventid = int(kv["eventid"]) if "eventid" in kv else None
    hostid = int(kv["hostid"]) if "hostid" in kv else None
    instance = kv.get("instance", default_instance)

    if eventid is None and parent_text:
        m = _PARENT_EVENTID_RE.search(parent_text)
        if m:
            eventid = int(m.group(1))
    if hostid is None and parent_text:
        m = _PARENT_HOSTID_RE.search(parent_text)
        if m:
            hostid = int(m.group(1))

    question = _KEY_VALUE_RE.sub("", cleaned).strip()
    return ParsedMention(question=question, eventid=eventid,
                         hostid=hostid, instance=instance)


def build_router(settings: Settings) -> APIRouter:
    if settings.slack is None:
        raise RuntimeError("build_router called without Slack settings")
    slack_settings = settings.slack
    signing_secret = slack_settings.signing_secret.get_secret_value()
    bot_token = slack_settings.bot_token.get_secret_value()
    allowed_channels = set(slack_settings.channel_allowlist)
    default_instance = slack_settings.default_instance or (
        settings.zabbix_instances[0].name if settings.zabbix_instances else ""
    )
    known_instances = [i.name for i in settings.zabbix_instances]

    router = APIRouter()

    @router.post("/slack/events")
    async def events(request: Request) -> Any:
        body = await request.body()
        ts = request.headers.get("X-Slack-Request-Timestamp", "")
        sig = request.headers.get("X-Slack-Signature", "")
        try:
            verify_slack_signature(body, ts, sig, signing_secret)
        except SlackSignatureError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

        payload = json.loads(body)

        if payload.get("type") == "url_verification":
            return JSONResponse({"challenge": payload.get("challenge", "")})

        if payload.get("type") != "event_callback":
            return JSONResponse({"ok": True})

        # #10: Deduplicate by event_id (Slack retries)
        event_id = payload.get("event_id", "")
        if event_id and _check_event_seen(event_id):
            return JSONResponse({"ok": True})

        event = payload.get("event") or {}
        if event.get("type") != "app_mention":
            return JSONResponse({"ok": True})

        channel = event.get("channel", "")
        if allowed_channels and channel not in allowed_channels:
            return JSONResponse({"ok": True})

        memory = getattr(request.app.state, "memory", None)
        try:
            await _handle_mention(
                event=event, channel=channel, settings=settings,
                bot_token=bot_token, default_instance=default_instance,
                known_instances=known_instances,
            )
        except Exception as e:
            with contextlib.suppress(Exception):
                await record_health(memory, kind="slack", name="primary",
                                    ok=False, error=str(e))
            raise
        with contextlib.suppress(Exception):
            await record_health(memory, kind="slack", name="primary", ok=True)
        return JSONResponse({"ok": True})

    return router


async def _handle_mention(*, event: dict[str, Any], channel: str,
                          settings: Settings, bot_token: str,
                          default_instance: str,
                          known_instances: list[str]) -> None:
    text = event.get("text", "") or ""
    parent_text: str | None = None
    thread_ts = event.get("thread_ts") or event.get("ts")
    parsed = parse_mention(text=text, parent_text=parent_text,
                           default_instance=default_instance,
                           known_instances=known_instances)
    if parsed.instance not in known_instances and known_instances:
        parsed.instance = default_instance

    slack = SlackClient(bot_token=bot_token)
    try:
        placeholder = await slack.post_message(
            channel=channel,
            text="Investigating…",
            blocks=render_placeholder(question=parsed.question),
            thread_ts=thread_ts,
        )
        ts = placeholder["ts"]
        try:
            async with InvestigationRunner(settings) as runner:
                ctx = InvestigationContext(
                    source="slack", instance=parsed.instance,
                    eventid=parsed.eventid, hostid=parsed.hostid,
                    question=parsed.question,
                )
                result = await runner.investigate(ctx)
            await slack.update_message(
                channel=channel, ts=ts,
                text=result.summary[:200],
                blocks=render_blocks(result),
            )
        except Exception as e:
            await slack.update_message(
                channel=channel, ts=ts,
                text=f"Investigation failed: {e}",
                blocks=[{"type": "section",
                         "text": {"type": "mrkdwn",
                                  "text": f":warning: Investigation failed: `{e}`"}}],
            )
    finally:
        await slack.aclose()
