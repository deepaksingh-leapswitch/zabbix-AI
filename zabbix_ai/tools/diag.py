"""diag.* tools — read-only host diagnostics via Zabbix global scripts.

Each diag is backed by a pre-registered Zabbix global script (see
`services/script_bootstrap.py`). The tool wrapper looks up the scriptid
from a per-instance `ScriptIndex` in the dispatch context and calls
`script.execute(scriptid, hostid, manualinput?)`. The agent runs the
fixed command (its `AllowKey=system.run[<cmd>]` rules enforce the same
allowlist defense-in-depth) and returns stdout.
"""
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


def _index(ctx: dict, instance: str):
    indices = ctx.get("scripts") or {}
    idx = indices.get(instance)
    if idx is None:
        raise ValueError(
            f"diag scripts not bootstrapped for instance '{instance}'"
        )
    return idx


async def _run_diag(client, index, hostid: int, name: str,
                     manualinput: str | None = None) -> str:
    if name not in ALLOWED_DIAG_KEYS:
        raise ValueError(f"diagnostic '{name}' not allowed")
    sid = index.scriptid(name)
    if sid is None:
        raise ValueError(f"no Zabbix script registered for {name}")
    params: dict = {"scriptid": sid, "hostid": str(hostid)}
    if manualinput is not None:
        params["manualinput"] = manualinput
    res = await client.call("script.execute", params)
    if res.get("response") != "success":
        raise ValueError(f"{name} execution failed: {res}")
    return res.get("value", "")


_HOST_INST_SCHEMA = {
    "type": "object",
    "properties": {
        "hostid": {"type": "integer"},
        "instance": {"type": "string"},
    },
    "required": ["hostid", "instance"],
}


def _register_simple(name: str, description: str) -> None:
    @register(name, description=description, schema=_HOST_INST_SCHEMA)
    async def _impl(*, hostid: int, instance: str, _ctx: dict) -> str:
        return await _run_diag(
            _client(_ctx, instance), _index(_ctx, instance), hostid, name,
        )

    _impl.__name__ = f"_{name.replace('.', '_')}"


def register_tools() -> None:
    _register_simple("diag.df", "Disk usage on the host (df -hP).")
    _register_simple("diag.free", "Memory usage (free -m).")
    _register_simple("diag.uptime", "System uptime and load average.")
    _register_simple("diag.top", "Top CPU/memory processes (top -bn1, head -30).")
    _register_simple("diag.dmesg_tail", "Last 100 lines of kernel ring buffer.")
    _register_simple("diag.ss_listen", "Sockets and listening ports (ss -tunap).")
    _register_simple("diag.ps_aux", "Process list sorted by memory, top 40 rows.")
    _register_simple("diag.iostat", "I/O statistics (iostat -xz 1 2).")
    _register_simple("diag.mysql_status", "MySQL server status summary.")
    _register_simple("diag.mysql_processlist", "MySQL SHOW FULL PROCESSLIST.")
    _register_simple("diag.apache_status", "Apache server-status output.")

    @register(
        "diag.systemctl_status",
        description="systemctl status <unit> output (read-only).",
        schema={"type": "object",
                "properties": {
                    "hostid": {"type": "integer"},
                    "instance": {"type": "string"},
                    "unit": {"type": "string"}},
                "required": ["hostid", "instance", "unit"]},
    )
    async def _systemctl(*, hostid: int, instance: str, unit: str,
                         _ctx: dict) -> str:
        # Defense in depth — Zabbix's manualinput regex already filters,
        # but we re-check the kwargs here too.
        if not unit.replace("-", "").replace(".", "").replace("_", "").replace("@", "").isalnum():
            raise ValueError("invalid unit name")
        return await _run_diag(
            _client(_ctx, instance), _index(_ctx, instance), hostid,
            "diag.systemctl_status", manualinput=unit,
        )

    @register(
        "diag.journal_tail",
        description="Last N lines of journalctl.",
        schema={"type": "object",
                "properties": {
                    "hostid": {"type": "integer"},
                    "instance": {"type": "string"},
                    "lines": {"type": "integer", "default": 200}},
                "required": ["hostid", "instance"]},
    )
    async def _journal(*, hostid: int, instance: str, lines: int = 200,
                       _ctx: dict) -> str:
        if not 1 <= lines <= 1000:
            raise ValueError("lines must be between 1 and 1000")
        return await _run_diag(
            _client(_ctx, instance), _index(_ctx, instance), hostid,
            "diag.journal_tail", manualinput=str(lines),
        )
