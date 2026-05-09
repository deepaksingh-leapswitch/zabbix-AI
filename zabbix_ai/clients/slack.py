from __future__ import annotations

from typing import Any

import httpx


class SlackError(Exception):
    pass

_BASE = "https://slack.com/api"

class SlackClient:
    def __init__(self, bot_token: str, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {bot_token}",
                     "Content-Type": "application/json; charset=utf-8"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(f"{_BASE}/{method}", json=payload)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown')}")
        return data

    async def _get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.get(f"{_BASE}/{method}", params=params)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown')}")
        return data

    async def post_message(self, *, channel: str, text: str = "",
                           blocks: list[dict[str, Any]] | None = None,
                           thread_ts: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._post("chat.postMessage", payload)

    async def update_message(self, *, channel: str, ts: str, text: str = "",
                             blocks: list[dict[str, Any]] | None = None,
                             ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
        return await self._post("chat.update", payload)

    async def replies(self, *, channel: str, thread_ts: str,
                      limit: int = 50) -> list[dict[str, Any]]:
        data = await self._get("conversations.replies",
                                {"channel": channel, "ts": thread_ts,
                                 "limit": str(limit)})
        return data.get("messages", [])
