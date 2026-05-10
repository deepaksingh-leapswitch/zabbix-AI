"""Admin-side audit logging (#8).

Writes to the `admin_audit_log` table (migration 005).  All values are
safe to log — secret *values* must never be passed to these helpers,
only key names / connection names.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from zabbix_ai.memory import Memory


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def log_admin_event(
    memory: Memory,
    *,
    event_type: str,
    by_user: str | None = None,
    target: str | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write one row to admin_audit_log.

    ``details`` may contain arbitrary JSON-serialisable data, but
    MUST NOT contain secret values — only key names, usernames, etc.
    """
    await memory.execute(
        """INSERT INTO admin_audit_log
           (ts, event_type, by_user, target, ip, details)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            _now(),
            event_type,
            by_user,
            target,
            ip,
            json.dumps(details, default=str) if details else None,
        ),
    )
