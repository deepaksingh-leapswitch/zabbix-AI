from __future__ import annotations

from zabbix_ai.memory import (
    find_pattern as _find_pattern,
)
from zabbix_ai.memory import (
    find_similar_past_investigations as _find_similar,
)
from zabbix_ai.tools import register


def _memory(ctx: dict):
    mem = ctx.get("memory")
    if mem is None:
        raise ValueError("memory not in context — orchestrator misconfigured")
    return mem


def register_tools() -> None:
    @register(
        "memory.find_similar_past_investigations",
        description=(
            "Find past investigations on the same host or matching the same "
            "pattern signature. Use when an alert looks familiar."
        ),
        schema={"type": "object",
                "properties": {
                    "hostid": {"type": "integer"},
                    "pattern_signature": {"type": "string"},
                    "limit": {"type": "integer", "default": 5}},
                "required": []},
    )
    async def _similar(*, hostid: int | None = None,
                       pattern_signature: str | None = None,
                       limit: int = 5, _ctx: dict) -> list[dict]:
        return await _find_similar(_memory(_ctx),
                                    hostid=hostid,
                                    pattern_signature=pattern_signature,
                                    limit=limit)

    @register(
        "memory.find_pattern",
        description=(
            "Look up an alert pattern by its stable signature. Returns typical "
            "root cause and fix from past investigations of the same pattern."
        ),
        schema={"type": "object",
                "properties": {"signature": {"type": "string"}},
                "required": ["signature"]},
    )
    async def _pattern(*, signature: str, _ctx: dict) -> dict | None:
        return await _find_pattern(_memory(_ctx), signature=signature)

    @register(
        "memory.find_resolved_tickets",
        description=(
            "Search HostBill for closed customer tickets that match the given "
            "alert pattern. Use to surface past resolutions for similar issues."
        ),
        schema={"type": "object",
                "properties": {
                    "alert_pattern": {"type": "string"},
                    "limit": {"type": "integer", "default": 5}},
                "required": ["alert_pattern"]},
    )
    async def _tickets(*, alert_pattern: str, limit: int = 5,
                       _ctx: dict) -> list[dict] | str:
        client = _ctx.get("hostbill_client")
        if client is None:
            return "HostBill not configured — pattern lookup unavailable"
        rows = await client.search_tickets(query=alert_pattern,
                                            status="Closed", limit=limit)
        # Return only the fields Claude needs
        keys = ("id", "subject", "status", "client_id", "lastreply")
        return [{k: r.get(k) for k in keys} for r in rows]
