"""resolution_notes — capture Zabbix ack messages onto investigations.

When a Zabbix problem is closed (operator ack, manual close, or
auto-recovery), Zabbix records one or more acknowledgement actions on
the event. This module pulls those acks back into our SQLite store on
the matching ``investigations`` row so future investigations of the
same pattern can lead with the prior resolution.

Public surface:
    - ``capture_resolution_from_zabbix_event(client, memory, *, investigation_id, eventid)``
        Sync one investigation row from one Zabbix event. Returns True
        if the row was updated.
    - ``poll_zabbix_for_resolutions(clients, memory, settings)``
        Single polling pass across every configured Zabbix instance.
        Looks at investigations from the last 7 days that have an
        eventid but no resolution_notes yet, fetches their acks, and
        writes resolution_notes back.
    - ``start_resolution_poller(app, settings, memory, clients)``
        Start the polling loop as an asyncio task attached to
        ``app.state.resolution_poller_task``. Polls every 2 minutes.

The Zabbix acknowledge ``action`` field is a bitmask:
    1=close, 2=ack, 4=add message, 16=change severity,
    32=unack, 64=suppress, 128=unsuppress.
We harvest any ack that carries a message (bit 4) or explicitly closed
the problem (bit 1).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

_log = logging.getLogger(__name__)

# Action bitmask flags (kept readable instead of magic numbers).
_ACTION_CLOSE = 1
_ACTION_ACK = 2
_ACTION_MESSAGE = 4

# How far back to look for investigations needing resolution capture.
_LOOKBACK_DAYS = 7
# Polling cadence — 2 min is the spec's chosen value.
_POLL_INTERVAL_SEC = 120
# Separator between concatenated ack messages, newest-last.
_ACK_SEPARATOR = "\n---\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clock_to_iso(clock: Any) -> str:
    """Convert a Zabbix unix-epoch clock (possibly a string) to ISO UTC."""
    try:
        ts = int(clock)
    except (TypeError, ValueError):
        return _now_iso()
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _has_message(action: Any) -> bool:
    try:
        return (int(action) & _ACTION_MESSAGE) == _ACTION_MESSAGE
    except (TypeError, ValueError):
        return False


def _has_close(action: Any) -> bool:
    try:
        return (int(action) & _ACTION_CLOSE) == _ACTION_CLOSE
    except (TypeError, ValueError):
        return False


def _ack_sort_key(ack: dict) -> int:
    try:
        return int(ack.get("clock", 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Username lookup (small cache per call)
# ---------------------------------------------------------------------------

async def _resolve_username(client: Any, userid: str,
                             cache: dict[str, str]) -> str:
    if not userid:
        return ""
    if userid in cache:
        return cache[userid]
    try:
        rows = await client.call("user.get", {
            "userids": [str(userid)],
            "output": ["username", "name", "surname"],
        })
    except Exception as exc:
        _log.debug("user.get(%s) failed: %s", userid, exc)
        cache[userid] = ""
        return ""
    if not rows:
        cache[userid] = ""
        return ""
    row = rows[0]
    name = row.get("username") or row.get("name") or row.get("surname") or ""
    cache[userid] = name
    return name


# ---------------------------------------------------------------------------
# Core: capture one investigation from one eventid
# ---------------------------------------------------------------------------

async def capture_resolution_from_zabbix_event(
    client: Any, memory: Any, *,
    investigation_id: int, eventid: int,
) -> bool:
    """Sync acknowledgement messages for one Zabbix event onto an
    investigation row.

    Returns True if the row was updated. Returns False if Zabbix has no
    relevant acks yet (so the caller can re-try on the next poll).
    """
    try:
        rows = await client.call("event.get", {
            "eventids": [str(eventid)],
            "output": ["eventid", "name", "clock", "r_clock"],
            "select_acknowledges": ["acknowledgeid", "message", "action",
                                    "userid", "clock"],
        })
    except Exception as exc:
        _log.debug("event.get(eventid=%s) failed: %s", eventid, exc)
        return False
    if not rows:
        return False

    acks = rows[0].get("acknowledges") or []
    # Only keep acks that carry a message or explicitly closed the problem.
    relevant = [
        a for a in acks
        if _has_message(a.get("action")) or _has_close(a.get("action"))
    ]
    if not relevant:
        return False

    relevant.sort(key=_ack_sort_key)  # oldest → newest

    # Resolve usernames once per unique userid.
    user_cache: dict[str, str] = {}
    notes_parts: list[str] = []
    for ack in relevant:
        msg = (ack.get("message") or "").strip()
        if not msg and not _has_close(ack.get("action")):
            continue
        # Collapse runs of whitespace inside the message but preserve
        # paragraph breaks the operator typed.
        msg = "\n".join(line.strip() for line in msg.splitlines()
                         if line.strip()) or ""
        username = await _resolve_username(
            client, str(ack.get("userid") or ""), user_cache,
        )
        prefix_bits: list[str] = []
        if username:
            prefix_bits.append(username)
        if _has_close(ack.get("action")):
            prefix_bits.append("[closed]")
        prefix = " ".join(prefix_bits)
        if prefix and msg:
            notes_parts.append(f"{prefix}: {msg}")
        elif prefix:
            notes_parts.append(prefix)
        elif msg:
            notes_parts.append(msg)

    if not notes_parts:
        return False

    notes = _ACK_SEPARATOR.join(notes_parts)

    latest = relevant[-1]
    latest_user = await _resolve_username(
        client, str(latest.get("userid") or ""), user_cache,
    )
    resolution_at = _clock_to_iso(latest.get("clock"))

    await memory.execute(
        """UPDATE investigations
           SET resolution_notes=?,
               resolution_at=?,
               resolution_by=?,
               resolution_source=?
           WHERE id=?""",
        (notes, resolution_at, latest_user or None,
         "zabbix_ack", investigation_id),
    )
    return True


# ---------------------------------------------------------------------------
# Polling pass
# ---------------------------------------------------------------------------

async def poll_zabbix_for_resolutions(
    clients: dict[str, Any], memory: Any, settings: Any | None = None,
) -> int:
    """Run one polling pass over every Zabbix instance.

    For each (instance, eventid) found in investigations from the last
    ``_LOOKBACK_DAYS`` days that still has ``resolution_notes IS NULL``,
    pull the acks and write resolution metadata back. Returns the count
    of rows updated.
    """
    if not clients:
        return 0

    # Limit lookback: investigations whose started_at is within the
    # window AND that still lack resolution_notes.
    cutoff = datetime.now(UTC).timestamp() - _LOOKBACK_DAYS * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=UTC).isoformat()

    rows = await memory.fetchall(
        """SELECT id, instance, eventid
           FROM investigations
           WHERE resolution_notes IS NULL
             AND eventid IS NOT NULL
             AND started_at >= ?""",
        (cutoff_iso,),
    )

    updated = 0
    for inv_id, instance, eventid in rows:
        if not instance or instance not in clients:
            continue
        client = clients[instance]
        try:
            ok = await capture_resolution_from_zabbix_event(
                client, memory,
                investigation_id=int(inv_id),
                eventid=int(eventid),
            )
            if ok:
                updated += 1
        except Exception as exc:
            _log.warning(
                "resolution capture failed for inv=%s event=%s: %s",
                inv_id, eventid, exc,
            )
    if updated:
        _log.info("resolution_notes: updated %d investigation row(s)",
                  updated)
    return updated


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def _run_poller(clients: dict[str, Any], memory: Any,
                       settings: Any | None,
                       interval: int = _POLL_INTERVAL_SEC) -> None:
    """Long-running task — calls ``poll_zabbix_for_resolutions`` every
    ``interval`` seconds. Survives individual-cycle errors."""
    _log.info("resolution_notes poller started (interval=%ds)", interval)
    while True:
        try:
            await poll_zabbix_for_resolutions(clients, memory, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("resolution_notes poll cycle failed: %s", exc)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(interval)


def start_resolution_poller(
    app: Any, settings: Any, memory: Any,
    clients: dict[str, Any] | None = None,
    *, interval: int = _POLL_INTERVAL_SEC,
) -> asyncio.Task:
    """Spawn the polling task and stash it on ``app.state`` so it isn't
    garbage-collected mid-loop.

    The integrator should call this from ``create_app`` (after
    ``setup_admin``) once Zabbix clients are available. ``clients`` is
    a dict ``{instance_name: ZabbixClient}``; if omitted we look it up
    on ``app.state.zabbix_clients``.

    Returns the spawned task so callers can ``await`` cancellation on
    shutdown if they want to.
    """
    if clients is None:
        clients = getattr(app.state, "zabbix_clients", {}) or {}
    task = asyncio.create_task(
        _run_poller(clients, memory, settings, interval=interval),
        name="resolution_poller",
    )
    app.state.resolution_poller_task = task
    return task
