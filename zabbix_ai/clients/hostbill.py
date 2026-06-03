from __future__ import annotations

import logging
from typing import Any

import httpx

_log = logging.getLogger(__name__)


class HostBillError(Exception):
    pass


class HostBillClient:
    """Read-only client for HostBill's admin API.

    HostBill expects form-encoded POSTs with api_id, api_key, call=<method>,
    plus method-specific params. Responses are JSON {"success": 0|1, ...}.

    The "search_*"/"get_*" methods used by the host linker are written to
    degrade gracefully — when the API is unreachable, returns missing creds,
    or returns an error envelope they return ``[]`` / ``None`` rather than
    raising. The original ``search_tickets``/``get_ticket_details``/
    ``get_client_details`` callers (`tools.memory`) still see the legacy
    raising behaviour via ``_call``.
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

    async def _try_call(self, method: str, **params: Any) -> dict[str, Any] | None:
        """Like _call but swallows every error and returns None.

        Used by the host-linker code paths where the HostBill API may not yet
        be configured / reachable — we never want a missing-API to bubble up
        through the orchestrator's enrichment step.
        """
        try:
            return await self._call(method, **params)
        except (httpx.HTTPError, HostBillError, ValueError) as e:
            _log.debug("hostbill._try_call(%s) failed: %s", method, e)
            return None
        except Exception as e:  # defensive: never raise out of this helper
            _log.warning("hostbill._try_call(%s) unexpected: %s", method, e)
            return None

    # ── Legacy, raising API (kept stable for tools.memory + tests) ───────────

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

    # ── Linker-friendly, non-raising API ──────────────────────────────────────

    async def search_services(self, *, ip: str | None = None,
                              domain: str | None = None) -> list[dict]:
        """Find HostBill services matching an IP or a domain.

        Returns an empty list if the API is unreachable, returns no match,
        or returns an error envelope. Never raises.

        The real HostBill endpoint shape for service search is
        ``getAccounts`` (with filter ``ip`` / ``domain``); if that's not
        available on a given deployment, the same payload is returned by
        ``getServices`` with similar filters. We try the documented call
        first and fall back if the API rejects it.
        """
        if ip is None and domain is None:
            return []
        for method in ("getAccounts", "getServices"):
            params: dict[str, Any] = {}
            if ip is not None:
                params["ip"] = ip
            if domain is not None:
                params["domain"] = domain
            data = await self._try_call(method, **params)
            if data is None:
                continue
            # Accept several response shapes — HostBill's API is uneven
            # across versions. First non-empty list wins.
            for key in ("accounts", "services", "results"):
                rows = data.get(key)
                if isinstance(rows, list) and rows:
                    return rows
                if isinstance(rows, dict) and rows:
                    # API sometimes returns id-keyed dict instead of list
                    return list(rows.values())
        return []

    async def get_service(self, service_id: int) -> dict | None:
        """Return the HostBill service dict, or None on any failure."""
        data = await self._try_call("getAccountDetails", id=service_id)
        if data is None:
            data = await self._try_call("getService", id=service_id)
        if data is None:
            return None
        for key in ("account", "service", "details"):
            row = data.get(key)
            if isinstance(row, dict) and row:
                return row
        return None

    async def get_client(self, client_id: int) -> dict | None:
        """Return the HostBill client dict, or None on any failure."""
        data = await self._try_call("getClientDetails", id=client_id)
        if data is None:
            return None
        for key in ("client", "details"):
            row = data.get(key)
            if isinstance(row, dict) and row:
                return row
        return None

    async def get_tickets(self, *, client_id: int,
                          service_id: int | None = None,
                          date_from: str | None = None) -> list[dict]:
        """Return tickets for a client (and optionally a service).

        Returns ``[]`` rather than raising on transport/API errors.
        """
        params: dict[str, Any] = {"client_id": client_id, "limit": 100}
        if service_id is not None:
            params["service_id"] = service_id
        if date_from is not None:
            params["date_from"] = date_from
        data = await self._try_call("getTickets", **params)
        if data is None:
            return []
        rows = data.get("tickets")
        if isinstance(rows, list):
            return rows
        if isinstance(rows, dict):
            return list(rows.values())
        return []

    async def is_reachable(self) -> bool:
        """Cheap reachability probe — returns False on any error."""
        # getTickets with limit=1 is the smallest legal call we know works
        # in the existing /admin/connections/hostbill/test handler.
        data = await self._try_call("getTickets", limit=1)
        return data is not None

    # ── Write API (ticket-flow, migration 008) ───────────────────────────────
    # These RAISE on failure (unlike the linker helpers) so the Slack approve
    # handler can report the error back to the operator instead of silently
    # creating nothing. HostBill API method/param names vary across versions;
    # adjust to match the live billing.leapswitch.com deployment if needed.

    async def add_ticket(self, *, subject: str, message: str,
                         department_id: int | None = None,
                         client_id: int | None = None,
                         priority: str = "High") -> int:
        """Create a HostBill ticket and return the new ticket id.

        Pass ``client_id`` to attach the ticket to a customer (customer
        ticket); omit it for an internal/department-only ticket. Raises
        HostBillError if no id can be parsed from the response.
        """
        data = await self._call(
            "addTicket",
            subject=subject, message=message,
            department_id=department_id, client_id=client_id,
            priority=priority,
        )
        # New id appears under varying keys across HostBill versions.
        for key in ("ticket_id", "ticketid", "id"):
            if data.get(key) is not None:
                return int(data[key])
        info = data.get("info") or data.get("ticket")
        if isinstance(info, dict):
            for key in ("ticket_id", "id"):
                if info.get(key) is not None:
                    return int(info[key])
        raise HostBillError(f"addTicket: no ticket id in response: {data}")

    async def add_ticket_reply(self, *, ticket_id: int, message: str) -> None:
        """Append a reply to an existing ticket. Raises on failure."""
        await self._call("addTicketReply", id=ticket_id, message=message)

    async def set_ticket_status(self, *, ticket_id: int, status: str) -> None:
        """Change a ticket status (e.g. \"Closed\"). Raises on failure."""
        await self._call("setTicketStatus", id=ticket_id, status=status)

    async def get_ticket_reply_count(self, ticket_id: int) -> int:
        """Return the number of replies on a ticket, or 0 on any failure.

        The follow-up worker compares this against the baseline captured at
        creation: a higher count means a human (customer or staff) replied,
        which stops the auto-nudge loop.
        """
        data = await self._try_call("getTicketDetails", id=ticket_id)
        if data is None:
            return 0
        ticket = data.get("ticket")
        if not isinstance(ticket, dict):
            return 0
        replies = ticket.get("replies")
        if isinstance(replies, (list, dict)):
            return len(replies)
        return 0
