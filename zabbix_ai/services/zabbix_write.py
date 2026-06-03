"""zabbix_write — the ONLY module that writes to Zabbix (ticket-flow Phase 4).

The service is otherwise a read-only fortress. The single destructive action —
disabling monitoring for a host that has been alerting unresolved for
``disable_after_days`` — lives here, is **approval-gated in Slack**, uses a
SEPARATE write-role token (``ZabbixInstance.write_token``), and is audited.

Scope is chosen from the problem type (the user's rule):
  * ICMP-only host (ping/host-down)      → disable the whole host
  * agent-based Linux/Windows trigger    → disable just the firing trigger
  * anything else / ambiguous            → a reversible maintenance window

If no write token is configured for the instance the disable is unavailable and
the caller falls back to an alert-only Slack notice.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from zabbix_ai.clients.zabbix import ZabbixClient
from zabbix_ai.services import ticket_flow as tf

_log = logging.getLogger(__name__)

# Item-key prefixes that indicate active-agent monitoring (→ disable trigger).
_AGENT_PREFIXES = (
    "agent.", "system.", "vfs.", "vm.", "proc.", "net.", "perf_counter",
    "service.", "sensor.", "wmi.", "kernel.",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def has_write_capability(settings: Any, instance_name: str) -> bool:
    for inst in settings.zabbix_instances:
        if inst.name == instance_name:
            wt = getattr(inst, "write_token", None)
            return bool(wt and wt.get_secret_value())
    return False


def build_write_client(settings: Any, instance_name: str,
                       memory: Any) -> ZabbixClient | None:
    for inst in settings.zabbix_instances:
        if inst.name == instance_name:
            wt = getattr(inst, "write_token", None)
            tok = wt.get_secret_value() if wt else ""
            if not tok:
                return None
            return ZabbixClient(inst.name, str(inst.url), tok, memory=memory)
    return None


async def classify_problem(client: Any, *, eventid: int) -> tuple[str, dict]:
    """Return (scope, target) where scope ∈ {'icmp','agent','other'}.

    target carries the ids needed to act: {'hostid': ..} and/or {'triggerid': ..}.
    """
    try:
        rows = await client.call("event.get", {
            "eventids": [str(eventid)],
            "output": ["objectid", "object"],
            "selectHosts": ["hostid"],
        })
    except Exception as exc:  # noqa: BLE001
        _log.debug("classify_problem event.get(%s) failed: %s", eventid, exc)
        return "other", {}
    if not rows:
        return "other", {}
    ev = rows[0]
    hostid = (ev.get("hosts") or [{}])[0].get("hostid")
    triggerid = ev.get("objectid")

    keys: list[str] = []
    if triggerid:
        with contextlib.suppress(Exception):
            trs = await client.call("trigger.get", {
                "triggerids": [str(triggerid)],
                "output": ["triggerid"],
                "selectItems": ["key_"],
            })
            if trs:
                keys = [it.get("key_", "") for it in (trs[0].get("items") or [])]

    if keys and all(k.startswith("icmpping") for k in keys):
        return "icmp", {"hostid": hostid}
    if any(any(k.startswith(p) for p in _AGENT_PREFIXES) for k in keys):
        return "agent", {"triggerid": triggerid, "hostid": hostid}
    return "other", {"hostid": hostid, "triggerid": triggerid}


async def _audit(memory: Any, event_type: str, detail: str) -> None:
    await memory.execute(
        "INSERT INTO audit_log (ts, event_type, tool_name, tool_output) "
        "VALUES (?,?,?,?)",
        (_now_iso(), event_type, "zabbix_write", detail),
    )


async def disable_host(client, memory, *, hostid, incident_id, approver) -> None:
    await client.call("host.update", {"hostid": str(hostid), "status": 1})
    await _audit(memory, "zabbix_disable_host",
                 f"hostid={hostid} incident={incident_id} by={approver}")


async def disable_trigger(client, memory, *, triggerid, incident_id, approver) -> None:
    await client.call("trigger.update", {"triggerid": str(triggerid), "status": 1})
    await _audit(memory, "zabbix_disable_trigger",
                 f"triggerid={triggerid} incident={incident_id} by={approver}")


async def create_maintenance(client, memory, *, hostid, name, incident_id,
                             approver, period: int = 86400) -> None:
    now_ts = int(datetime.now(UTC).timestamp())
    base = {"name": name[:128], "active_since": now_ts,
            "active_till": now_ts + period,
            "timeperiods": [{"timeperiod_type": 0, "start_date": now_ts,
                             "period": period}]}
    # Zabbix 6.0+ uses hosts:[{hostid}]; older uses hostids:[id]. Try both.
    try:
        await client.call("maintenance.create",
                          {**base, "hosts": [{"hostid": str(hostid)}]})
    except Exception:  # noqa: BLE001
        await client.call("maintenance.create",
                          {**base, "hostids": [str(hostid)]})
    await _audit(memory, "zabbix_maintenance",
                 f"hostid={hostid} incident={incident_id} by={approver}")


async def perform_disable(*, settings: Any, memory: Any, incident_id: int,
                          approver: str) -> dict:
    """Execute the scope-appropriate disable for a ``disable_pending`` incident."""
    inc = await tf.get_incident(memory, incident_id)
    if inc is None:
        return {"ok": False, "msg": "incident not found"}
    if inc["state"] != "disable_pending":
        return {"ok": False, "msg": f"not pending (state={inc['state']})"}
    # Atomically claim so a double-click can't disable twice.
    if not await tf.claim_transition(memory, incident_id,
                                     "disable_pending", "disabling"):
        cur = await tf.get_incident(memory, incident_id)
        return {"ok": False,
                "msg": f"not pending (state={cur['state'] if cur else 'gone'})"}
    client = build_write_client(settings, inc["zabbix_instance"], memory)
    if client is None:
        await tf.claim_transition(memory, incident_id, "disabling", "disable_pending")
        return {"ok": False, "msg": "no Zabbix write token configured"}
    try:
        scope, target = await classify_problem(client, eventid=int(inc["eventid"]))
        if scope == "icmp" and target.get("hostid"):
            await disable_host(client, memory, hostid=target["hostid"],
                               incident_id=incident_id, approver=approver)
            what = f"whole host `{inc['hostname'] or target['hostid']}`"
        elif scope == "agent" and target.get("triggerid"):
            await disable_trigger(client, memory, triggerid=target["triggerid"],
                                  incident_id=incident_id, approver=approver)
            what = "the firing trigger"
        else:
            await create_maintenance(
                client, memory, hostid=target.get("hostid"),
                name=f"zabbix-ai incident {incident_id}",
                incident_id=incident_id, approver=approver)
            what = f"a maintenance window on `{inc['hostname']}`"
    except Exception as exc:  # noqa: BLE001
        _log.exception("perform_disable failed for incident %s", incident_id)
        await tf.claim_transition(memory, incident_id, "disabling", "disable_pending")
        return {"ok": False, "msg": f"disable failed: {exc}"}
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    await tf.update_incident(memory, incident_id, state="disabled",
                             disabled_at=_now_iso(),
                             disable_approved_by=approver, disable_scope=scope)
    return {"ok": True,
            "msg": f":mute: Monitoring disabled — {what}. Approved by <@{approver}>."}


async def dismiss_disable(memory: Any, incident_id: int, *, by: str) -> dict:
    inc = await tf.get_incident(memory, incident_id)
    if inc is None:
        return {"ok": False, "msg": "incident not found"}
    # disable_scope='dismissed' is the sentinel that stops re-arming.
    await tf.update_incident(memory, incident_id, state="quiet",
                             disable_scope="dismissed")
    return {"ok": True}


def build_disable_blocks(*, incident_id: int, hostname: str | None,
                         scope: str) -> list[dict]:
    label = {"icmp": "disable the *whole host*",
             "agent": "disable *only the firing trigger*",
             "other": "put the host into a *maintenance window*"}.get(
                 scope, "disable monitoring")
    title = (f":rotating_light: `{hostname or 'host'}` has been unresolved for "
             f"the configured limit. Proposed action: {label}.")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        {"type": "actions", "block_id": f"disable_{incident_id}", "elements": [
            {"type": "button", "style": "danger",
             "action_id": "disable_approve",
             "text": {"type": "plain_text", "text": "Approve disable"},
             "value": str(incident_id)},
            {"type": "button", "action_id": "disable_dismiss",
             "text": {"type": "plain_text", "text": "Dismiss"},
             "value": str(incident_id)},
        ]},
    ]
