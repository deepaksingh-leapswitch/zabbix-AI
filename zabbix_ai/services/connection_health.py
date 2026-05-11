"""Track last-success / last-error per external connection.

Called from Zabbix / Slack / Anthropic clients on every call. Single row
per (kind, name) — UPSERT, no append. Failures are non-fatal: if the
table doesn't exist yet (older DB) we swallow the exception so we don't
break the actual API call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def record_health(memory: Any, *, kind: str, name: str,
                        ok: bool, error: str = "") -> None:
    if memory is None:
        return
    ts = _now_iso()
    try:
        if ok:
            await memory.execute(
                "INSERT INTO connection_health (kind, name, last_success_at) "
                "VALUES (?,?,?) "
                "ON CONFLICT(kind, name) DO UPDATE SET "
                "  last_success_at=excluded.last_success_at",
                (kind, name, ts),
            )
        else:
            await memory.execute(
                "INSERT INTO connection_health (kind, name, last_error_at, last_error) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(kind, name) DO UPDATE SET "
                "  last_error_at=excluded.last_error_at, "
                "  last_error=excluded.last_error",
                (kind, name, ts, (error or "")[:200]),
            )
    except Exception:
        # Best-effort. Never break the calling code on a logging failure.
        pass


async def get_health(memory: Any) -> dict[tuple[str, str], dict]:
    """Return {(kind, name): {last_success_at, last_error_at, last_error}}."""
    if memory is None:
        return {}
    try:
        rows = await memory.fetchall(
            "SELECT kind, name, last_success_at, last_error_at, last_error "
            "FROM connection_health"
        )
    except Exception:
        return {}
    out: dict[tuple[str, str], dict] = {}
    for kind, name, succ, err_at, err in rows:
        out[(kind, name)] = {
            "last_success_at": succ,
            "last_error_at": err_at,
            "last_error": err or "",
        }
    return out
