from __future__ import annotations

import contextlib
from typing import Any

from anthropic import AsyncAnthropic

from zabbix_ai.services.connection_health import record_health


class ClaudeClient:
    def __init__(self, api_key: str, memory: Any = None):
        self._client = AsyncAnthropic(api_key=api_key)
        # Optional memory handle for /admin/status health tracking.
        self._memory = memory

    async def create(self, *, model: str, system: list[dict[str, Any]],
                     tools: list[dict[str, Any]], messages: list[dict[str, Any]],
                     max_tokens: int = 2048) -> Any:
        try:
            resp = await self._client.messages.create(
                model=model, system=system, tools=tools,
                messages=messages, max_tokens=max_tokens,
            )
        except Exception as e:
            with contextlib.suppress(Exception):
                await record_health(self._memory, kind="anthropic",
                                    name="primary", ok=False, error=str(e))
            raise
        with contextlib.suppress(Exception):
            await record_health(self._memory, kind="anthropic",
                                name="primary", ok=True)
        return resp
