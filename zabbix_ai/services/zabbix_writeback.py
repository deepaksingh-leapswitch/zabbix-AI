"""Shared helper: write a finished investigation summary back to a Zabbix
event as an acknowledgment comment (`event.acknowledge` action=4).

Called from both the auto-investigate webhook (post-investigation) and
the manual right-click adapter (after `investigate_streaming` completes).
Best-effort — failures are logged but never raised, so a flaky Zabbix
write-back can't blow up the investigation flow.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

# Zabbix event ack messages are capped at 2048 chars in the default schema.
# Leave headroom for the prefix.
_ACK_BODY_LIMIT = 2000


def format_ack_message(summary: str, *, source: str = "manual") -> str:
    """Render the event.acknowledge message body.

    `source` is `"manual"` (right-click) or `"auto"` (webhook) — it
    appears in the prefix so an operator reading the Zabbix UI can
    tell whether a human triggered the investigation.
    """
    tag = "manual" if source == "manual" else "auto-investigation"
    prefix = f"[zabbix-rca-AI {tag}]\n"
    body = summary or "(no summary produced)"
    if len(body) > _ACK_BODY_LIMIT:
        body = body[: _ACK_BODY_LIMIT - 1] + "…"
    return prefix + body


async def post_summary_to_event(
    zabbix_client: Any,
    *,
    eventid: int | str,
    summary: str,
    source: str = "manual",
) -> bool:
    """Add the investigation summary as a Zabbix event comment.

    Returns True on success, False on any failure (logged). Never raises.
    """
    if not eventid or zabbix_client is None:
        return False
    try:
        await zabbix_client.call("event.acknowledge", {
            "eventids": [str(eventid)],
            "action": 4,  # 4 = add message
            "message": format_ack_message(summary, source=source),
        })
        return True
    except Exception as exc:  # pragma: no cover - logged, never raised
        _log.warning(
            "event.acknowledge writeback failed for eventid %s: %s",
            eventid, exc,
        )
        return False


async def post_summary_to_host_open_problems(
    zabbix_client: Any,
    *,
    hostid: int | str,
    summary: str,
    source: str = "manual",
    max_targets: int = 3,
) -> int:
    """Host-mode fallback when no specific eventid is available.

    Resolves the host's currently-open problems (severity DESC) and writes
    the same summary as a comment against the top ``max_targets``. This is
    what makes the right-click "Investigate with AI" from a *host page*
    (vs from a *problem*) still leave a paper trail in the Zabbix UI.

    Returns the number of events successfully commented on. Never raises.
    """
    if not hostid or zabbix_client is None:
        return 0
    try:
        problems = await zabbix_client.call("problem.get", {
            "hostids": [str(hostid)],
            "output": ["eventid", "severity", "name"],
            "recent": False,
            "sortfield": ["severity", "eventid"],
            "sortorder": "DESC",
            "limit": max_targets,
        })
    except Exception as exc:
        _log.warning("problem.get for host %s failed: %s", hostid, exc)
        return 0
    if not problems:
        return 0
    eventids = [str(p["eventid"]) for p in problems
                if p.get("eventid")][:max_targets]
    if not eventids:
        return 0
    try:
        await zabbix_client.call("event.acknowledge", {
            "eventids": eventids,
            "action": 4,
            "message": format_ack_message(summary, source=source),
        })
        return len(eventids)
    except Exception as exc:  # pragma: no cover - logged, never raised
        _log.warning(
            "event.acknowledge host-mode writeback failed for host %s: %s",
            hostid, exc,
        )
        return 0
