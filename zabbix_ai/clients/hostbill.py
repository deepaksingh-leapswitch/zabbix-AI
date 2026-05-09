from __future__ import annotations

from typing import Any

import httpx


class HostBillError(Exception):
    pass


class HostBillClient:
    """Read-only client for HostBill's admin API.

    HostBill expects form-encoded POSTs with api_id, api_key, call=<method>,
    plus method-specific params. Responses are JSON {"success": 0|1, ...}.
    """

    def __init__(self, *, api_url: str, api_id: str, api_key: str,
                 timeout: float = 10.0):
        self._api_url = api_url
        self._api_id = api_id
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        form = {"api_id": self._api_id, "api_key": self._api_key,
                "call": method, **{k: str(v) for k, v in params.items()
                                    if v is not None}}
        r = await self._client.post(self._api_url, data=form)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise HostBillError(f"{method}: {data.get('error', 'unknown')}")
        return data

    async def search_tickets(self, *, query: str = "", status: str = "Closed",
                             limit: int = 20,
                             department_id: int | None = None) -> list[dict]:
        data = await self._call(
            "getTickets",
            search=query, status=status,
            limit=limit, department_id=department_id,
        )
        return data.get("tickets", []) or []

    async def get_ticket_details(self, ticket_id: int) -> dict[str, Any]:
        data = await self._call("getTicketDetails", id=ticket_id)
        return data.get("ticket", {}) or {}

    async def get_client_details(self, client_id: int) -> dict[str, Any]:
        data = await self._call("getClientDetails", id=client_id)
        return data.get("client", {}) or {}
