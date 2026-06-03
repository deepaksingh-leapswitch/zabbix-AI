"""followup_worker — escalating Slack follow-up loop for ticket-flow incidents.

After a ticket is created (see ``services/ticket_flow.py``) this background loop
chases it until one of the stop conditions is met:

  * The Zabbix problem recovers           → post "resolved", close the ticket.
  * A human replies on the HostBill ticket → hand off to the NOC, stop nudging.
  * ``quiet_after_days`` (3) with no reply → go quiet (ticket stays open).
  * ``disable_after_days`` (6) still active → arm the approval-gated disable
        (implemented in Phase 4; until then we simply stay quiet).

Otherwise it posts an escalating reminder into the incident's Slack thread on
the ``nudge_schedule_minutes`` cadence.

``process_incident`` takes its Slack / HostBill / Zabbix clients as arguments so
it can be unit-tested with fakes and a fixed ``now``; the polling pass builds the
real clients once per cycle.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from zabbix_ai.clients.hostbill import HostBillClient
from zabbix_ai.clients.slack import SlackClient
from zabbix_ai.clients.zabbix import ZabbixClient
from zabbix_ai.services import ticket_flow as tf

_log = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 60
# States the worker still acts on; everything else is terminal.
_ACTIVE_STATES = ("created", "awaiting_reply", "quiet")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _age_days(created_at: str | None, now: datetime) -> float:
    dt = _parse_iso(created_at)
    if dt is None:
        return 0.0
    return (now - dt).total_seconds() / 86400.0


def _nudge_due(next_nudge_at: str | None, now: datetime) -> bool:
    dt = _parse_iso(next_nudge_at)
    return dt is not None and now >= dt


async def event_is_resolved(client: Any, eventid: int) -> bool:
    """True if the Zabbix event has recovered (r_clock set or value=OK)."""
    try:
        rows = await client.call("event.get", {
            "eventids": [str(eventid)],
            "output": ["eventid", "r_clock", "value"],
        })
    except Exception as exc:  # noqa: BLE001
        _log.debug("event.get(%s) failed: %s", eventid, exc)
        return False  # unknown → treat as still active, retry next cycle
    if not rows:
        return False
    r = rows[0]
    r_clock = str(r.get("r_clock", "0"))
    value = str(r.get("value", "1"))  # 1 = problem, 0 = OK/resolved
    return (r_clock not in ("", "0")) or value == "0"


async def _post_thread(slack: SlackClient | None, inc: dict, text: str) -> None:
    if slack is None or not inc.get("slack_channel"):
        return
    with contextlib.suppress(Exception):
        await slack.post_message(
            channel=inc["slack_channel"], text=text,
            thread_ts=inc.get("slack_thread_ts") or None,
        )


async def process_incident(
    *, settings: Any, memory: Any, inc: dict,
    zabbix: ZabbixClient | None, slack: SlackClient | None,
    hostbill: HostBillClient | None, now: datetime,
) -> str:
    """Advance one incident by one tick. Returns the action taken (for tests)."""
    cfg = settings.ticket_flow
    state = inc["state"]
    if state not in _ACTIVE_STATES:
        return "skip"

    # 1) Zabbix recovery → resolve + close ticket.
    if zabbix is not None and inc.get("eventid") is not None:
        if await event_is_resolved(zabbix, int(inc["eventid"])):
            await _post_thread(
                slack, inc,
                f":white_check_mark: Problem recovered on "
                f"`{inc.get('hostname') or 'host'}` — closing out.")
            tid = inc.get("hostbill_ticket_id")
            if hostbill is not None and tid and not cfg.dry_run:
                with contextlib.suppress(Exception):
                    await hostbill.add_ticket_reply(
                        ticket_id=int(tid),
                        message="Zabbix problem recovered; auto-closing.")
                    await hostbill.set_ticket_status(
                        ticket_id=int(tid), status="Closed")
            await tf.update_incident(memory, inc["id"], state="resolved",
                                     problem_active=0, resolved_at=_now_iso())
            return "resolved"

    # 2) Human reply on the ticket → hand off, stop nudging.
    tid = inc.get("hostbill_ticket_id")
    if hostbill is not None and tid and not cfg.dry_run:
        count = await hostbill.get_ticket_reply_count(int(tid))
        if count > (inc.get("baseline_reply_count") or 0):
            await _post_thread(
                slack, inc,
                ":speech_balloon: A reply was received on the ticket — "
                "NOC please take it forward. Pausing auto follow-up.")
            await tf.update_incident(memory, inc["id"], state="handed_off")
            return "handed_off"

    age = _age_days(inc.get("created_at"), now)

    # 3) 6-day backstop — arm the approval-gated disable (Phase 4 fills this in).
    if age >= cfg.disable_after_days:
        armed = await _maybe_arm_disable(
            settings=settings, memory=memory, inc=inc,
            zabbix=zabbix, slack=slack, now=now)
        if armed:
            return "disable_armed"
        # No disable handler yet (Phase 3) → stay quiet, keep the backstop.
        if state != "quiet":
            await tf.update_incident(memory, inc["id"], state="quiet")
        return "quiet"

    # 4) 3-day quiet — stop nudging, leave the ticket open.
    if age >= cfg.quiet_after_days:
        if state != "quiet":
            await tf.update_incident(memory, inc["id"], state="quiet")
        return "quiet"

    # 5) Escalating nudge if due.
    if state in ("created", "awaiting_reply") and _nudge_due(inc.get("next_nudge_at"), now):
        n = (inc.get("nudge_count") or 0) + 1
        mention = "<!here> " if n >= 2 else ""
        tid_txt = f" (ticket #{tid})" if tid else ""
        await _post_thread(
            slack, inc,
            f"{mention}:bell: Follow-up #{n}: `{inc.get('hostname') or 'host'}` — "
            f"{inc.get('trigger_name') or 'problem'} still unresolved{tid_txt}. "
            "Please update or resolve.")
        await tf.update_incident(
            memory, inc["id"], state="awaiting_reply", nudge_count=n,
            last_nudge_at=_now_iso(),
            next_nudge_at=tf.compute_next_nudge_at(cfg, n))
        return "nudged"

    return "noop"


async def _maybe_arm_disable(*, settings, memory, inc, zabbix, slack, now) -> bool:
    """Post the approval-gated disable prompt once (Phase 4).

    Returns True when the 6-day backstop is handled (armed, dismissed, or
    unavailable) so the caller stops nudging. ``disable_scope`` being set is
    the idempotency sentinel that prevents re-posting every cycle.
    """
    from zabbix_ai.services import zabbix_write as zw

    if inc.get("disable_scope"):  # already armed / dismissed / unavailable
        return True
    cfg = settings.ticket_flow
    instance = inc["zabbix_instance"]
    if (slack is None or zabbix is None or inc.get("eventid") is None
            or not zw.has_write_capability(settings, instance)):
        await _post_thread(
            slack, inc,
            f":warning: Still unresolved after {cfg.disable_after_days} days, "
            "but automatic monitoring-disable is unavailable "
            "(no Zabbix write token). Please handle manually.")
        await tf.update_incident(memory, inc["id"], disable_scope="unavailable")
        return True
    scope, _target = await zw.classify_problem(zabbix, eventid=int(inc["eventid"]))
    with contextlib.suppress(Exception):
        await slack.post_message(
            channel=inc["slack_channel"],
            text="Disable monitoring?",
            blocks=zw.build_disable_blocks(
                incident_id=inc["id"], hostname=inc.get("hostname"),
                scope=scope),
            thread_ts=inc.get("slack_thread_ts") or None)
    await tf.update_incident(memory, inc["id"], state="disable_pending",
                             disable_scope=scope)
    return True


# ── polling pass + background task ───────────────────────────────────────────

def _build_clients(settings: Any, memory: Any) -> dict[str, ZabbixClient]:
    clients: dict[str, ZabbixClient] = {}
    for inst in settings.zabbix_instances:
        try:
            clients[inst.name] = ZabbixClient(
                inst.name, str(inst.url), inst.token.get_secret_value(),
                memory=memory)
        except Exception as exc:  # noqa: BLE001
            _log.warning("followup_worker: could not build client %s: %s",
                         inst.name, exc)
    return clients


async def poll_followups(settings: Any, memory: Any,
                         clients: dict[str, ZabbixClient]) -> int:
    """One pass over all active incidents. Returns the number processed."""
    cfg = getattr(settings, "ticket_flow", None)
    if cfg is None or not cfg.enabled:
        return 0
    placeholders = ",".join("?" for _ in _ACTIVE_STATES)
    rows = await memory.fetchall(
        f"SELECT id FROM incidents WHERE state IN ({placeholders})",
        tuple(_ACTIVE_STATES),
    )
    if not rows:
        return 0

    slack = (SlackClient(bot_token=settings.slack.bot_token.get_secret_value())
             if settings.slack is not None else None)
    hostbill = None
    if settings.hostbill is not None and not cfg.dry_run:
        hostbill = HostBillClient(
            api_url=str(settings.hostbill.api_url),
            api_id=settings.hostbill.api_id.get_secret_value(),
            api_key=settings.hostbill.api_key.get_secret_value())
    now = datetime.now(UTC)
    processed = 0
    try:
        for (incident_id,) in rows:
            inc = await tf.get_incident(memory, int(incident_id))
            if inc is None:
                continue
            zc = clients.get(inc["zabbix_instance"])
            try:
                await process_incident(
                    settings=settings, memory=memory, inc=inc,
                    zabbix=zc, slack=slack, hostbill=hostbill, now=now)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning("followup_worker: incident %s failed: %s",
                             incident_id, exc)
    finally:
        if slack is not None:
            with contextlib.suppress(Exception):
                await slack.aclose()
        if hostbill is not None:
            with contextlib.suppress(Exception):
                await hostbill.aclose()
    return processed


async def _run_worker(settings: Any, memory: Any,
                      clients: dict[str, ZabbixClient],
                      interval: int = _POLL_INTERVAL_SEC) -> None:
    _log.info("followup_worker started (interval=%ds)", interval)
    while True:
        try:
            await poll_followups(settings, memory, clients)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.warning("followup_worker cycle failed: %s", exc)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(interval)


def start_followup_worker(app: Any, settings: Any, memory: Any,
                          clients: dict[str, ZabbixClient] | None = None,
                          *, interval: int = _POLL_INTERVAL_SEC) -> asyncio.Task:
    """Spawn the follow-up loop and stash it on app.state so it isn't GC'd."""
    if not clients:
        clients = _build_clients(settings, memory)
    task = asyncio.create_task(
        _run_worker(settings, memory, clients, interval=interval),
        name="followup_worker")
    app.state.followup_worker_task = task
    return task
