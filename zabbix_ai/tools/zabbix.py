from __future__ import annotations
from typing import Any
from zabbix_ai.tools import register


def _client(ctx: dict, instance: str):
    clients = ctx.get("clients") or {}
    if instance not in clients:
        raise ValueError(f"unknown instance '{instance}'")
    return clients[instance]


def register_tools() -> None:
    @register("zabbix.get_problem", description="Get a Zabbix problem by event id.",
              schema={"type": "object",
                      "properties": {
                          "eventid": {"type": "integer"},
                          "instance": {"type": "string"}},
                      "required": ["eventid", "instance"]})
    async def _get_problem(*, eventid: int, instance: str, _ctx: dict) -> dict:
        return await _client(_ctx, instance).get_problem(eventid)

    @register("zabbix.get_open_problems",
              description="List currently open problems for a host or hostgroup.",
              schema={"type": "object",
                      "properties": {
                          "instance": {"type": "string"},
                          "hostid": {"type": "integer"},
                          "hostgroupid": {"type": "integer"}},
                      "required": ["instance"]})
    async def _open(*, instance: str, hostid: int | None = None,
                    hostgroupid: int | None = None, _ctx: dict) -> list[dict]:
        return await _client(_ctx, instance).get_open_problems(hostid, hostgroupid)

    @register("zabbix.get_host", description="Get full host info including groups, interfaces, inventory.",
              schema={"type": "object",
                      "properties": {
                          "hostid": {"type": "integer"},
                          "instance": {"type": "string"}},
                      "required": ["hostid", "instance"]})
    async def _host(*, hostid: int, instance: str, _ctx: dict) -> dict:
        return await _client(_ctx, instance).get_host(hostid)

    @register("zabbix.get_history",
              description="Get historical metric values for given item keys on a host.",
              schema={"type": "object",
                      "properties": {
                          "hostid": {"type": "integer"},
                          "instance": {"type": "string"},
                          "keys": {"type": "array", "items": {"type": "string"}},
                          "range_seconds": {"type": "integer", "default": 3600}},
                      "required": ["hostid", "instance", "keys"]})
    async def _history(*, hostid: int, instance: str, keys: list[str],
                       range_seconds: int = 3600, _ctx: dict) -> dict:
        return await _client(_ctx, instance).get_history(hostid, keys, range_seconds)
