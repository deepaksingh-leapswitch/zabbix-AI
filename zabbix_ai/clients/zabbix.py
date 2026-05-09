from __future__ import annotations

import asyncio
from typing import Any

import httpx


class ZabbixError(Exception):
    pass


class ZabbixClient:
    def __init__(self, name: str, url: str, token: str, timeout: float = 60.0):
        self.name = name
        self.url = url.rstrip("/") + "/api_jsonrpc.php"
        self.token = token
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json-rpc",
                     "Authorization": f"Bearer {token}"},
        )
        self._id = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def call(self, method: str, params: dict | list[Any] | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "method": method,
                   "params": params or {}, "id": self._next_id()}
        r = await self._client.post(self.url, json=payload)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            raise ZabbixError(f"{err.get('message')}: {err.get('data')}")
        return data["result"]

    async def get_problem(self, eventid: int) -> dict:
        # Zabbix 7.4 removed selectHosts/selectTags from problem.get; use event.get instead.
        rows = await self.call("event.get", {
            "eventids": [str(eventid)],
            "output": "extend",
            "selectHosts": ["hostid", "host", "name"],
            "selectTags": ["tag", "value"],
        })
        if not rows:
            raise ZabbixError(f"no event found for eventid={eventid}")
        return rows[0]

    async def get_open_problems(self, hostid: int | None = None,
                                hostgroupid: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"output": "extend"}
        if hostid:
            params["hostids"] = [str(hostid)]
        if hostgroupid:
            params["groupids"] = [str(hostgroupid)]
        return await self.call("problem.get", params)

    async def get_host(self, hostid: int) -> dict:
        # Zabbix 7.x renamed selectGroups → selectHostGroups; response key
        # is now `hostgroups`. We normalise back to `groups` so callers and
        # tests don't have to track both naming schemes.
        rows = await self.call("host.get", {
            "hostids": [str(hostid)],
            "output": "extend",
            "selectHostGroups": ["groupid", "name"],
            "selectInterfaces": ["ip", "dns", "type"],
            "selectInventory": "extend",
            "selectTags": ["tag", "value"],
        })
        if not rows:
            raise ZabbixError(f"no host found for hostid={hostid}")
        host = rows[0]
        if "hostgroups" in host and "groups" not in host:
            host["groups"] = host["hostgroups"]
        return host

    async def get_history(self, hostid: int, keys: list[str], range_seconds: int = 3600) -> dict:
        items = await self.call("item.get", {
            "hostids": [str(hostid)],
            "search": {"key_": keys},
            "searchByAny": True,
            "output": ["itemid", "key_", "value_type", "name"],
        })
        if not items:
            return {}
        import time
        time_from = int(time.time()) - range_seconds
        result: dict[str, list] = {}
        for item in items:
            history = await self.call("history.get", {
                "itemids": [item["itemid"]],
                "history": int(item["value_type"]),
                "time_from": time_from,
                "sortfield": "clock",
                "sortorder": "ASC",
                "limit": 200,
            })
            result[item["key_"]] = [{"clock": int(h["clock"]),
                                     "value": h["value"]} for h in history]
        return result

    async def get_item(self, hostid: int, key: str) -> dict | None:
        rows = await self.call("item.get", {
            "hostids": [str(hostid)],
            "filter": {"key_": key},
            "output": ["itemid", "key_", "value_type", "lastvalue", "lastclock"],
        })
        return rows[0] if rows else None

    async def task_create_check_now(self, itemid: str) -> None:
        await self.call("task.create", [{"type": 6,
                                          "request": {"itemid": str(itemid)}}])

    async def wait_for_fresh_value(self, itemid: str, after_clock: int,
                                   timeout: float = 15.0) -> str:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            rows = await self.call("item.get", {
                "itemids": [str(itemid)],
                "output": ["lastvalue", "lastclock"],
            })
            if rows and int(rows[0].get("lastclock") or 0) > after_clock:
                return rows[0]["lastvalue"]
            await asyncio.sleep(1.0)
        raise ZabbixError(f"timeout waiting for fresh value on item {itemid}")
