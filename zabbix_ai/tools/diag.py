from __future__ import annotations
from zabbix_ai.tools import register

ALLOWED_DIAG_KEYS = {
    "diag.df", "diag.free", "diag.uptime", "diag.top",
    "diag.dmesg_tail", "diag.journal_tail", "diag.systemctl_status",
    "diag.ss_listen", "diag.ps_aux", "diag.iostat",
    "diag.mysql_status", "diag.mysql_processlist", "diag.apache_status",
}

def _client(ctx: dict, instance: str):
    clients = ctx.get("clients") or {}
    if instance not in clients:
        raise ValueError(f"unknown instance '{instance}'")
    return clients[instance]

async def _run_diag(client, hostid: int, key: str) -> str:
    if key not in ALLOWED_DIAG_KEYS:
        raise ValueError(f"diagnostic '{key}' not allowed")
    item = await client.get_item(hostid, key)
    if not item:
        raise ValueError(f"agent on host {hostid} does not expose {key}")
    after = int(item.get("lastclock") or 0)
    await client.task_create_check_now(item["itemid"])
    return await client.wait_for_fresh_value(item["itemid"], after_clock=after, timeout=15)

_HOST_INST_SCHEMA = {
    "type": "object",
    "properties": {"hostid": {"type": "integer"},
                   "instance": {"type": "string"}},
    "required": ["hostid", "instance"],
}

def _register_simple(name: str, key: str, description: str) -> None:
    @register(name, description=description, schema=_HOST_INST_SCHEMA)
    async def _impl(*, hostid: int, instance: str, _ctx: dict) -> str:
        return await _run_diag(_client(_ctx, instance), hostid, key)

def register_tools() -> None:
    _register_simple("diag.df", "diag.df", "Disk usage on the host (df -hP).")
    _register_simple("diag.free", "diag.free", "Memory usage (free -m).")
    _register_simple("diag.uptime", "diag.uptime", "System uptime and load.")
    _register_simple("diag.top", "diag.top", "Top CPU/memory processes (top -bn1).")
    _register_simple("diag.dmesg_tail", "diag.dmesg_tail", "Last 100 lines of kernel ring buffer.")
    _register_simple("diag.ss_listen", "diag.ss_listen", "Listening sockets (ss -tunap).")
    _register_simple("diag.ps_aux", "diag.ps_aux", "Process list sorted by memory.")
    _register_simple("diag.iostat", "diag.iostat", "I/O statistics (iostat -xz 1 2).")
    _register_simple("diag.mysql_status", "diag.mysql_status", "MySQL server status summary.")
    _register_simple("diag.mysql_processlist", "diag.mysql_processlist",
                     "MySQL SHOW FULL PROCESSLIST.")
    _register_simple("diag.apache_status", "diag.apache_status", "Apache server-status output.")

    @register("diag.systemctl_status",
              description="systemctl status <unit> output (read-only).",
              schema={"type": "object",
                      "properties": {
                          "hostid": {"type": "integer"},
                          "instance": {"type": "string"},
                          "unit": {"type": "string"}},
                      "required": ["hostid", "instance", "unit"]})
    async def _systemctl(*, hostid: int, instance: str, unit: str, _ctx: dict) -> str:
        if not unit.replace("-", "").replace(".", "").replace("_", "").replace("@", "").isalnum():
            raise ValueError("invalid unit name")
        client = _client(_ctx, instance)
        item = await client.get_item(hostid, f"diag.systemctl_status[{unit}]")
        if not item:
            raise ValueError(f"agent does not expose diag.systemctl_status[{unit}]")
        after = int(item.get("lastclock") or 0)
        await client.task_create_check_now(item["itemid"])
        return await client.wait_for_fresh_value(item["itemid"], after_clock=after, timeout=15)

    @register("diag.journal_tail",
              description="Last N lines of journalctl for a unit.",
              schema={"type": "object",
                      "properties": {
                          "hostid": {"type": "integer"},
                          "instance": {"type": "string"},
                          "lines": {"type": "integer", "default": 200}},
                      "required": ["hostid", "instance"]})
    async def _journal(*, hostid: int, instance: str, lines: int = 200, _ctx: dict) -> str:
        if not 1 <= lines <= 1000:
            raise ValueError("lines must be between 1 and 1000")
        client = _client(_ctx, instance)
        item = await client.get_item(hostid, f"diag.journal_tail[{lines}]")
        if not item:
            raise ValueError(f"agent does not expose diag.journal_tail[{lines}]")
        after = int(item.get("lastclock") or 0)
        await client.task_create_check_now(item["itemid"])
        return await client.wait_for_fresh_value(item["itemid"], after_clock=after, timeout=15)
