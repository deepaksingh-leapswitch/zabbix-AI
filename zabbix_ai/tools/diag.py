"""diag.* tools — read-only host diagnostics via Zabbix global scripts.

Each diag is backed by per-OS Zabbix global scripts (see
`services/script_bootstrap.py`). The tool wrapper detects the target
host's OS from Zabbix host inventory or tags, picks the matching
scriptid, and calls `script.execute(scriptid, hostid, manualinput?)`.
The agent runs the fixed command (its `AllowKey=system.run[<cmd>]` rules
enforce the same allowlist defense-in-depth) and returns stdout.

`diag.snapshot` is a single-shot tool that bundles the most-common
diagnostics so a typical first-look triage is one round trip, not
seven. The individual diags remain available for follow-up drilling.
"""
from __future__ import annotations

from zabbix_ai.tools import register

ALLOWED_DIAG_KEYS = {
    "diag.df", "diag.free", "diag.uptime", "diag.top",
    "diag.dmesg_tail", "diag.journal_tail", "diag.systemctl_status",
    "diag.ss_listen", "diag.ps_aux", "diag.iostat",
    "diag.mysql_status", "diag.mysql_processlist", "diag.apache_status",
    "diag.snapshot", "diag.mail_queue",
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
            f"diag scripts not bootstrapped for instance '{instance}'",
        )
    return idx


def _normalise_os(raw: str | None) -> str:
    """Map a free-text OS string to one of {'linux', 'windows'}."""
    if not raw:
        return "linux"
    low = raw.lower()
    if "win" in low:
        return "windows"
    if "linux" in low or "centos" in low or "ubuntu" in low or "debian" in low \
            or "rhel" in low or "rocky" in low or "alma" in low:
        return "linux"
    # Default to linux — most of the fleet is Linux and trying a Linux command
    # on a non-matching agent fails gracefully with the agent's own error
    # message, which is more useful than a hard refusal here.
    return "linux"


async def _detect_os(client, hostid: int, cache: dict) -> str:
    """Return 'linux' or 'windows' for the host. Cached per investigation."""
    if hostid in cache:
        return cache[hostid]
    host = await client.get_host(hostid)
    # Priority 1: a host tag with key 'os'
    for tag in host.get("tags") or []:
        if tag.get("tag", "").lower() == "os":
            kind = _normalise_os(tag.get("value"))
            cache[hostid] = kind
            return kind
    # Priority 2: inventory.os_short, then os, then os_full
    inv = host.get("inventory") or {}
    if isinstance(inv, dict):
        for field in ("os_short", "os", "os_full"):
            v = inv.get(field)
            if v:
                kind = _normalise_os(v)
                cache[hostid] = kind
                return kind
    cache[hostid] = "linux"
    return "linux"


def _os_cache(ctx: dict) -> dict:
    cache = ctx.get("host_os_cache")
    if cache is None:
        cache = {}
        ctx["host_os_cache"] = cache
    return cache


async def _run_diag(ctx: dict, instance: str, hostid: int, name: str,
                     manualinput: str | None = None) -> str:
    if name not in ALLOWED_DIAG_KEYS:
        raise ValueError(f"diagnostic '{name}' not allowed")
    client = _client(ctx, instance)
    index = _index(ctx, instance)
    os_kind = await _detect_os(client, hostid, _os_cache(ctx))
    sid = index.scriptid(name, os_kind)
    if sid is None:
        raise ValueError(
            f"{name} is not available for {os_kind} hosts on instance '{instance}'",
        )
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
        return await _run_diag(_ctx, instance, hostid, name)

    _impl.__name__ = f"_{name.replace('.', '_')}"


def register_tools() -> None:
    _register_simple(
        "diag.snapshot",
        "First-look snapshot of the host: uptime, disk, memory, top processes, "
        "listening ports, recent kernel/system events — one round trip. "
        "Use this BEFORE drilling into individual diag tools.",
    )
    _register_simple("diag.df", "Disk usage on the host.")
    _register_simple("diag.free", "Memory usage.")
    _register_simple("diag.uptime", "System uptime and load average.")
    _register_simple("diag.top", "Top CPU/memory processes (head 30).")
    _register_simple("diag.dmesg_tail", "Recent kernel/system events.")
    _register_simple("diag.ss_listen", "Listening sockets / TCP connections.")
    _register_simple("diag.ps_aux", "Process list sorted by memory, top 40.")
    _register_simple("diag.iostat", "I/O statistics.")
    _register_simple("diag.mysql_status", "MySQL server status (Linux only).")
    _register_simple("diag.mysql_processlist",
                     "MySQL SHOW FULL PROCESSLIST (Linux only).")
    _register_simple("diag.apache_status", "Apache server-status (Linux only).")
    _register_simple(
        "diag.mail_queue",
        "Mail queue inspection: counts per folder, top 20 senders, "
        "top 20 recipients, age distribution. Linux auto-detects "
        "Postfix/Exim/mailq; Windows scans MailEnable. Envelope "
        "addresses only — no message bodies or subject lines.",
    )

    @register(
        "diag.systemctl_status",
        description="Service / systemd unit status (read-only).",
        schema={"type": "object",
                "properties": {
                    "hostid": {"type": "integer"},
                    "instance": {"type": "string"},
                    "unit": {"type": "string"}},
                "required": ["hostid", "instance", "unit"]},
    )
    async def _systemctl(*, hostid: int, instance: str, unit: str,
                         _ctx: dict) -> str:
        # Defense in depth — the Zabbix script has a manualinput regex too.
        # #18: Tightened regex — no space allowed (prevents arg injection).
        import re as _re
        if not _re.fullmatch(r"[a-zA-Z0-9._@-]{1,64}", unit):
            raise ValueError(
                "invalid unit name: must match ^[a-zA-Z0-9._@-]{1,64}$"
            )
        return await _run_diag(
            _ctx, instance, hostid, "diag.systemctl_status", manualinput=unit,
        )

    @register(
        "diag.journal_tail",
        description="Last N lines of journalctl (Linux) / system event log (Windows).",
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
            _ctx, instance, hostid, "diag.journal_tail", manualinput=str(lines),
        )
