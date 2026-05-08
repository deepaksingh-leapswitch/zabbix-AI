from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any
from zabbix_ai.memory import Memory

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class AuditLog:
    def __init__(self, memory: Memory):
        self.memory = memory

    async def log_start(
        self, *, source: str, instance: str | None = None,
        eventid: int | None = None, ticket_id: int | None = None,
        customer_id: int | None = None, hostid: int | None = None,
        hostname: str | None = None, model: str | None = None,
    ) -> int:
        await self.memory.execute(
            """INSERT INTO investigations
               (source, instance, eventid, ticket_id, customer_id,
                hostid, hostname, started_at, model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, instance, eventid, ticket_id, customer_id,
             hostid, hostname, _now(), model),
        )
        row = await self.memory.fetchone("SELECT last_insert_rowid()")
        inv_id = int(row[0]) if row else 0
        await self.memory.execute(
            "INSERT INTO audit_log (ts, investigation_id, event_type, source) "
            "VALUES (?, ?, 'start', ?)",
            (_now(), inv_id, source),
        )
        return inv_id

    async def log_tool(
        self, inv_id: int, tool_name: str, tool_input: dict, tool_output: Any
    ) -> None:
        await self.memory.execute(
            """INSERT INTO audit_log
               (ts, investigation_id, event_type, tool_name, tool_input, tool_output)
               VALUES (?, ?, 'tool_call', ?, ?, ?)""",
            (_now(), inv_id, tool_name,
             json.dumps(tool_input, default=str),
             str(tool_output)[:8000]),
        )

    async def log_error(self, inv_id: int, message: str) -> None:
        await self.memory.execute(
            """INSERT INTO audit_log
               (ts, investigation_id, event_type, tool_output)
               VALUES (?, ?, 'error', ?)""",
            (_now(), inv_id, message[:8000]),
        )

    async def log_end(
        self, inv_id: int, *, summary: str = "", root_cause: str = "",
        suggested_actions: str = "", confidence: str = "",
        pattern_signature: str = "", duration_ms: int = 0,
        tokens_in: int = 0, tokens_out: int = 0,
    ) -> None:
        await self.memory.execute(
            """UPDATE investigations
               SET summary=?, root_cause=?, suggested_actions=?, confidence=?,
                   pattern_signature=?, duration_ms=?, tokens_in=?, tokens_out=?
               WHERE id=?""",
            (summary, root_cause, suggested_actions, confidence,
             pattern_signature, duration_ms, tokens_in, tokens_out, inv_id),
        )
        await self.memory.execute(
            "INSERT INTO audit_log (ts, investigation_id, event_type) "
            "VALUES (?, ?, 'end')",
            (_now(), inv_id),
        )
