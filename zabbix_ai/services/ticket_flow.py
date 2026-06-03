"""Ticket-flow state machine (migration 008): draft → approve → create → follow-up.

Phase 2 covers draft / approve / create. The follow-up loop (Phase 3) and the
6-day approval-gated disable (Phase 4) build on the same ``incidents`` table and
the helpers here.

Design notes:
  * One ``incidents`` row per qualifying Zabbix problem. ``create_incident`` is
    idempotent on (instance, eventid) so a retried webhook never double-drafts.
  * Tickets are created ONLY on human approval in Slack — never automatically.
  * ``dry_run`` routes every ticket to the internal department and skips the
    real HostBill write, so the whole flow can be exercised safely.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from zabbix_ai.clients.hostbill import HostBillClient
from zabbix_ai.clients.slack import SlackClient
from zabbix_ai.config import Settings, TicketFlowSettings

_log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Column order for incidents row → dict mapping. Mirrors migration 008.
_COLS = [
    "id", "zabbix_instance", "eventid", "hostid", "hostname", "severity",
    "trigger_name", "problem_type", "investigation_id", "ticket_kind",
    "hostbill_client_id", "hostbill_ticket_id", "slack_channel",
    "slack_thread_ts", "state", "nudge_count", "last_nudge_at", "next_nudge_at",
    "baseline_reply_count", "problem_active", "resolved_at", "created_at",
    "approved_at", "approved_by", "ticket_created_at", "disable_scope",
    "disable_approved_by", "disabled_at",
]


# ── incidents DB helpers ─────────────────────────────────────────────────────

async def create_incident(
    memory, *, instance: str, eventid: int | None, hostid: int | None,
    hostname: str | None, severity: int, trigger_name: str, problem_type: str,
    investigation_id: int | None, ticket_kind: str,
    hostbill_client_id: int | None, slack_channel: str | None,
) -> int:
    """Insert a drafted incident (or return the existing one for this event).

    Idempotent on (instance, eventid) when eventid is not None — a webhook
    retry returns the same incident id instead of creating a duplicate.
    """
    if eventid is not None:
        existing = await memory.fetchone(
            "SELECT id FROM incidents WHERE zabbix_instance=? AND eventid=?",
            (instance, eventid),
        )
        if existing:
            return int(existing[0])
    await memory.execute(
        """INSERT INTO incidents
             (zabbix_instance, eventid, hostid, hostname, severity,
              trigger_name, problem_type, investigation_id, ticket_kind,
              hostbill_client_id, slack_channel, state, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'drafted',?)""",
        (instance, eventid, hostid, hostname, severity, trigger_name,
         problem_type, investigation_id, ticket_kind, hostbill_client_id,
         slack_channel, _now_iso()),
    )
    row = await memory.fetchone("SELECT last_insert_rowid()")
    return int(row[0]) if row else 0


async def claim_transition(memory, incident_id: int, from_state: str,
                           to_state: str, **extra) -> bool:
    """Atomically move an incident from from_state -> to_state.

    Returns True iff this caller won the transition (rowcount == 1) — the
    guard against concurrent double-actions (e.g. two Slack approvals).
    Optional extra columns are set in the same atomic UPDATE.
    """
    cols = "state=?" + "".join(f", {k}=?" for k in extra)
    params = (to_state, *extra.values(), incident_id, from_state)
    rc = await memory.execute_write(
        f"UPDATE incidents SET {cols} WHERE id=? AND state=?", params)
    return rc == 1


async def get_incident(memory, incident_id: int) -> dict | None:
    row = await memory.fetchone(
        f"SELECT {', '.join(_COLS)} FROM incidents WHERE id=?", (incident_id,),
    )
    return dict(zip(_COLS, row)) if row else None


async def update_incident(memory, incident_id: int, **fields: Any) -> None:
    if not fields:
        return
    # Column names are internal constants, never user input — safe to inline.
    set_clause = ", ".join(f"{k}=?" for k in fields)
    await memory.execute(
        f"UPDATE incidents SET {set_clause} WHERE id=?",
        (*fields.values(), incident_id),
    )


# ── Routing + cadence ────────────────────────────────────────────────────────

def resolve_ticket_target(
    cfg: TicketFlowSettings, link: Any,
) -> tuple[str, int | None, int | None]:
    """Decide customer-vs-internal ticket. Returns (kind, client_id, dept_id).

    A customer ticket is used only when the host is confidently linked to a
    HostBill client; otherwise (infra hosts, low-confidence, or dry-run) the
    ticket goes to the internal department.
    """
    if (not cfg.dry_run and link is not None
            and getattr(link, "is_linked", False)
            and getattr(link, "hostbill_client_id", None)
            and getattr(link, "confidence", "low") in ("high", "medium")):
        return "customer", int(link.hostbill_client_id), None
    return "internal", cfg.internal_client_id, cfg.internal_department_id


def matches_trigger_patterns(cfg: TicketFlowSettings, problem_name: str) -> bool:
    """True if the problem is in ticket-flow scope.

    Empty patterns => match everything (backward compatible). Each pattern
    is a case-insensitive regex; a malformed pattern degrades to a plain
    case-insensitive substring test so a bad config line never crashes the
    webhook.
    """
    patterns = getattr(cfg, "trigger_name_patterns", None) or []
    if not patterns:
        return True
    name = problem_name or ""
    for p in patterns:
        try:
            if re.search(p, name, re.IGNORECASE):
                return True
        except re.error:
            if p.lower() in name.lower():
                return True
    return False


def compute_next_nudge_at(cfg: TicketFlowSettings, nudge_count: int) -> str:
    """ISO timestamp for the next follow-up nudge given how many have been sent.

    Walks the escalating ``nudge_schedule_minutes`` list, clamping at the last
    (repeating) entry once exhausted.
    """
    sched = cfg.nudge_schedule_minutes or [60]
    mins = sched[min(nudge_count, len(sched) - 1)]
    return (datetime.now(UTC) + timedelta(minutes=mins)).isoformat()


# ── Slack draft rendering ────────────────────────────────────────────────────

def build_draft_blocks(
    *, incident_id: int, hostname: str | None, summary: str,
    ticket_kind: str, eventid: int | None,
) -> list[dict[str, Any]]:
    safe = (summary or "_(no summary produced)_")[:2800]
    kind_label = "Customer ticket" if ticket_kind == "customer" else "Internal ticket"
    title = f":memo: *Proposed {kind_label}: `{hostname or 'unknown host'}`*"
    if eventid:
        title += f"  ·  event {eventid}"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": safe}},
        {"type": "actions", "block_id": f"ticket_{incident_id}", "elements": [
            {"type": "button", "style": "primary",
             "action_id": "ticket_approve",
             "text": {"type": "plain_text", "text": "Approve & raise ticket"},
             "value": str(incident_id)},
            {"type": "button", "style": "danger",
             "action_id": "ticket_discard",
             "text": {"type": "plain_text", "text": "Discard"},
             "value": str(incident_id)},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "Awaiting approval — no ticket has been raised yet."}]},
    ]


async def post_draft(
    slack: SlackClient, *, channel: str, incident_id: int,
    hostname: str | None, summary: str, ticket_kind: str, eventid: int | None,
) -> dict:
    return await slack.post_message(
        channel=channel,
        text=f"Proposed ticket: {hostname or 'unknown host'}",
        blocks=build_draft_blocks(
            incident_id=incident_id, hostname=hostname, summary=summary,
            ticket_kind=ticket_kind, eventid=eventid),
    )


# ── Approve / discard ────────────────────────────────────────────────────────

async def approve_and_create_ticket(
    *, settings: Settings, memory, incident_id: int, approver: str,
) -> dict:
    """Create the HostBill ticket for a drafted incident. Idempotent per state."""
    inc = await get_incident(memory, incident_id)
    if inc is None:
        return {"ok": False, "msg": "incident not found"}
    if inc["state"] != "drafted":
        return {"ok": False, "msg": f"already {inc['state']}"}
    # Atomically claim the draft so concurrent approvals can't double-create.
    if not await claim_transition(memory, incident_id, "drafted", "approving"):
        cur = await get_incident(memory, incident_id)
        return {"ok": False, "msg": f"already {cur['state'] if cur else 'gone'}"}

    cfg = settings.ticket_flow
    srow = await memory.fetchone(
        "SELECT summary FROM investigations WHERE id=?",
        (inc["investigation_id"],),
    )
    summary = srow[0] if srow and srow[0] else ""
    subject = f"[Zabbix] {inc['hostname'] or 'host'} — {inc['trigger_name'] or 'problem'}"
    body = summary or "Automated ticket raised from a Zabbix problem (AI RCA)."

    ticket_id: int | None = None
    baseline = 0
    if cfg.dry_run or settings.hostbill is None:
        _log.info("ticket_flow: dry-run/no-hostbill — would create %s ticket "
                  "for incident %s", inc["ticket_kind"], incident_id)
    else:
        hb = HostBillClient(
            api_url=str(settings.hostbill.api_url),
            api_id=settings.hostbill.api_id.get_secret_value(),
            api_key=settings.hostbill.api_key.get_secret_value(),
        )
        try:
            client_id = (inc["hostbill_client_id"]
                         if inc["ticket_kind"] == "customer"
                         else cfg.internal_client_id)
            # A ticket with no client_id needs a requester name + email.
            req_name = None if client_id else (cfg.internal_requester_name or "Zabbix RCA AI")
            req_email = None if client_id else (cfg.internal_requester_email or None)
            ticket_id = await hb.add_ticket(
                subject=subject, body=body,
                dept_id=cfg.internal_department_id, client_id=client_id,
                priority=3 if (inc.get("severity") or 0) >= 5 else 2,
                name=req_name, email=req_email,
            )
            baseline = await hb.get_ticket_reply_count(ticket_id)
        except Exception:
            # release the claim so the draft can be retried
            await claim_transition(memory, incident_id, "approving", "drafted")
            raise
        finally:
            await hb.aclose()

    await update_incident(
        memory, incident_id,
        state="created", approved_at=_now_iso(), approved_by=approver,
        hostbill_ticket_id=ticket_id, ticket_created_at=_now_iso(),
        baseline_reply_count=baseline,
        next_nudge_at=compute_next_nudge_at(cfg, 0),
    )
    # Audit the write (audit_log shape from audit.py).
    await memory.execute(
        "INSERT INTO audit_log (ts, investigation_id, event_type, tool_name, "
        "tool_output) VALUES (?,?,?,?,?)",
        (_now_iso(), inc["investigation_id"], "ticket_created",
         "hostbill.add_ticket",
         f"incident={incident_id} ticket_id={ticket_id} "
         f"kind={inc['ticket_kind']} approver={approver} dry_run={cfg.dry_run}"),
    )
    return {"ok": True, "ticket_id": ticket_id,
            "ticket_kind": inc["ticket_kind"], "dry_run": cfg.dry_run}


async def discard_incident(memory, incident_id: int, *, by: str) -> dict:
    inc = await get_incident(memory, incident_id)
    if inc is None:
        return {"ok": False, "msg": "incident not found"}
    await update_incident(memory, incident_id,
                          state="discarded", approved_by=by)
    return {"ok": True}
