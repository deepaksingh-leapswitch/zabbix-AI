from __future__ import annotations

from zabbix_ai.tools import register


def _client(ctx: dict, instance: str):
    clients = ctx.get("clients") or {}
    if instance not in clients:
        raise ValueError(f"unknown instance '{instance}'")
    return clients[instance]

def register_tools() -> None:
    @register("lookup.host_by_domain",
              description="Find a Zabbix host by served domain (uses host tags).",
              schema={"type": "object",
                      "properties": {
                          "domain": {"type": "string"},
                          "instance": {"type": "string"}},
                      "required": ["domain", "instance"]})
    async def _by_domain(*, domain: str, instance: str, _ctx: dict) -> dict | None:
        client = _client(_ctx, instance)
        rows = await client.call("host.get", {
            "output": ["hostid", "host", "name"],
            "tags": [{"tag": "domain", "value": domain, "operator": "0"}],
        })
        return rows[0] if rows else None

    @register("lookup.host_by_ip",
              description="Find a Zabbix host by primary interface IP.",
              schema={"type": "object",
                      "properties": {
                          "ip": {"type": "string"},
                          "instance": {"type": "string"}},
                      "required": ["ip", "instance"]})
    async def _by_ip(*, ip: str, instance: str, _ctx: dict) -> dict | None:
        client = _client(_ctx, instance)
        rows = await client.call("host.get", {
            "output": ["hostid", "host", "name"],
            "filter": {"ip": ip},
        })
        return rows[0] if rows else None
