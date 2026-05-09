"""Idempotent bootstrap of `rca-ai.diag.*` Zabbix global scripts.

The orchestrator runs read-only diagnostics by calling Zabbix's
`script.execute` against pre-registered global scripts (type=Script,
execute_on=Zabbix agent, scope=Manual host action). This module makes
sure those scripts exist on each Zabbix instance the service talks to.

We name our scripts `rca-ai.diag.<name>` so they don't collide with
ops-defined scripts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from zabbix_ai.clients.zabbix import ZabbixClient

_SCRIPT_PREFIX = "rca-ai."


@dataclass(frozen=True)
class DiagDef:
    """One diagnostic command to be backed by a Zabbix global script."""

    name: str                       # e.g. "diag.df" — what Claude sees
    description: str                # for the Claude tool registry
    command: str                    # shell run on the agent (may use {MANUALINPUT})
    timeout_s: int = 30
    # Parameterised diags (e.g. systemctl status <unit>) configure these:
    manualinput: bool = False
    manualinput_prompt: str = ""
    manualinput_validator: str = ""        # regex
    manualinput_default_value: str = ""
    manualinput_arg_name: str = ""         # name of the kwarg in the tool wrapper

    @property
    def script_name(self) -> str:
        return _SCRIPT_PREFIX + self.name


DIAG_DEFINITIONS: list[DiagDef] = [
    DiagDef("diag.df", "Disk usage on the host (df -hP).", "df -hP"),
    DiagDef("diag.free", "Memory usage (free -m).", "free -m"),
    DiagDef("diag.uptime", "System uptime and load.", "uptime"),
    DiagDef("diag.top", "Top CPU/memory processes (top -bn1, head -30).",
            "top -bn1 | head -30"),
    DiagDef("diag.dmesg_tail", "Last 100 lines of kernel ring buffer.",
            "dmesg -T 2>/dev/null | tail -100"),
    DiagDef("diag.ss_listen", "Sockets and listening ports (ss -tunap).",
            "ss -tunap 2>/dev/null"),
    DiagDef("diag.ps_aux",
            "Process list sorted by memory, top 40 entries.",
            "ps auxf --sort=-%mem | head -40"),
    DiagDef("diag.iostat", "I/O statistics (iostat -xz 1 2).",
            "iostat -xz 1 2 2>/dev/null"),
    DiagDef("diag.mysql_status", "MySQL server status summary.",
            "mysqladmin --defaults-file=/etc/zabbix/.my.cnf status 2>/dev/null"),
    DiagDef("diag.mysql_processlist", "MySQL SHOW FULL PROCESSLIST.",
            "mysql --defaults-file=/etc/zabbix/.my.cnf "
            "-e 'SHOW FULL PROCESSLIST' 2>/dev/null"),
    DiagDef("diag.apache_status", "Apache server-status output.",
            "curl -s http://127.0.0.1/server-status?auto 2>/dev/null"),
    DiagDef(
        "diag.systemctl_status",
        "systemctl status <unit> output (read-only).",
        "systemctl status {MANUALINPUT} --no-pager 2>/dev/null",
        manualinput=True,
        manualinput_prompt="systemd unit name",
        manualinput_validator=r"^[a-zA-Z0-9._@-]{1,64}$",
        manualinput_arg_name="unit",
    ),
    DiagDef(
        "diag.journal_tail",
        "Last N lines of journalctl.",
        "journalctl -n {MANUALINPUT} --no-pager 2>/dev/null",
        manualinput=True,
        manualinput_prompt="line count",
        manualinput_validator=r"^[1-9][0-9]{0,3}$",
        manualinput_default_value="200",
        manualinput_arg_name="lines",
    ),
]


@dataclass
class ScriptIndex:
    """Maps internal diag name → Zabbix scriptid + the original DiagDef."""
    by_name: dict[str, str] = field(default_factory=dict)
    defs_by_name: dict[str, DiagDef] = field(default_factory=dict)

    def scriptid(self, diag_name: str) -> str | None:
        return self.by_name.get(diag_name)

    def diagdef(self, diag_name: str) -> DiagDef | None:
        return self.defs_by_name.get(diag_name)


def _build_create_params(d: DiagDef) -> dict:
    params: dict = {
        "name": d.script_name,
        "type": 0,                 # 0 = Script
        "scope": 2,                # 2 = Manual host action (required by script.execute)
        "execute_on": 0,           # 0 = Zabbix agent
        "command": d.command,
        "host_access": 2,          # read access
        "usrgrpid": "0",           # 0 = available to all user groups (subject to host perms)
        "groupid": "0",            # 0 = applies to all host groups
        "timeout": f"{d.timeout_s}s",
    }
    if d.manualinput:
        params.update({
            "manualinput": "1",
            "manualinput_prompt": d.manualinput_prompt or "value",
            "manualinput_validator_type": "0",       # regex
            "manualinput_validator": d.manualinput_validator or ".*",
            "manualinput_default_value": d.manualinput_default_value,
        })
    return params


async def ensure_diag_scripts(client: ZabbixClient,
                               defs: list[DiagDef] | None = None) -> ScriptIndex:
    """Look up each `rca-ai.diag.*` script; create any that are missing.

    Returns an index of diag-name → scriptid that the orchestrator threads
    into the tool dispatch context.
    """
    if defs is None:
        defs = DIAG_DEFINITIONS

    names = [d.script_name for d in defs]
    existing = await client.call("script.get", {
        "filter": {"name": names},
        "output": ["scriptid", "name"],
    })
    have: dict[str, str] = {row["name"]: row["scriptid"] for row in existing}

    index = ScriptIndex()
    for d in defs:
        sid = have.get(d.script_name)
        if sid is None:
            res = await client.call("script.create", _build_create_params(d))
            sid = res["scriptids"][0]
        index.by_name[d.name] = sid
        index.defs_by_name[d.name] = d
    return index
