from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic


class ClaudeClient:
    def __init__(self, api_key: str):
        self._client = AsyncAnthropic(api_key=api_key)

    async def create(self, *, model: str, system: list[dict[str, Any]],
                     tools: list[dict[str, Any]], messages: list[dict[str, Any]],
                     max_tokens: int = 2048) -> Any:
        return await self._client.messages.create(
            model=model, system=system, tools=tools,
            messages=messages, max_tokens=max_tokens,
        )
