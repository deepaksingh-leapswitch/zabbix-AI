"""Idempotent bootstrap of `rca-ai.<diag>.<os>` Zabbix global scripts.

The orchestrator runs read-only diagnostics by calling Zabbix's
`script.execute` against pre-registered global scripts (type=Script,
execute_on=Zabbix agent, scope=Manual host action). Each diag has a
Linux variant and (where applicable) a Windows variant — they're
registered as separate scripts (`rca-ai.diag.df.linux`,
`rca-ai.diag.df.windows`, etc.) and the tool wrapper picks the right
one based on the host's OS at investigation time.

`diag.snapshot` is a special one-shot tool that bundles the most-common
diagnostics into a single script, so a typical investigation can start
with one round trip instead of seven.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from zabbix_ai.clients.zabbix import ZabbixClient

_MENU_PATH_PREFIX = "zabbix-AI"
_OS_DISPLAY = {"linux": "Linux", "windows": "Windows"}
# Zabbix 7.4 caps script.create timeout at 30s; we reject larger values.
_TIMEOUT = "30s"

# Linux multi-command snapshot — runs through /bin/sh on the agent.
_LINUX_SNAPSHOT = (
    "echo '=== uptime ==='; uptime; "
    "echo '=== df -hP ==='; df -hP; "
    "echo '=== free -m ==='; free -m; "
    "echo '=== top (head 20) ==='; top -bn1 2>/dev/null | head -20; "
    "echo '=== ps by mem (head 20) ==='; ps auxf --sort=-%mem 2>/dev/null | head -20; "
    "echo '=== ss listen (head 20) ==='; ss -tlnp 2>/dev/null | head -20; "
    "echo '=== dmesg tail 30 ==='; dmesg -T 2>/dev/null | tail -30"
)

# Windows snapshot — single PowerShell invocation. Agent on Windows runs
# `system.run[]` via cmd.exe; we hand it `powershell -Command "..."`.
_WINDOWS_SNAPSHOT = (
    'powershell -NoProfile -Command "'
    "Write-Output '=== uptime ==='; "
    "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime; "
    "Write-Output '=== drives ==='; "
    "Get-PSDrive -PSProvider FileSystem | "
    "Select Name,@{n='UsedGB';e={[math]::Round($_.Used/1GB,1)}},"
    "@{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}} | Format-Table -AutoSize | Out-String; "
    "Write-Output '=== memory ==='; "
    "Get-CimInstance Win32_OperatingSystem | "
    "Select TotalVisibleMemorySize,FreePhysicalMemory | Format-List | Out-String; "
    "Write-Output '=== top processes by ws ==='; "
    "Get-Process | Sort-Object WS -Desc | Select -First 20 "
    "Name,Id,@{n='WS_MB';e={[math]::Round($_.WorkingSet/1MB,1)}} | "
    "Format-Table -AutoSize | Out-String; "
    "Write-Output '=== listening ports ==='; "
    "Get-NetTCPConnection -State Listen 2>$null | "
    "Select LocalAddress,LocalPort | Sort LocalPort | Select -First 20 | "
    "Format-Table -AutoSize | Out-String; "
    "Write-Output '=== latest 30 system errors ==='; "
    "Get-EventLog System -EntryType Error,Warning -Newest 30 -EA SilentlyContinue | "
    "Format-Table TimeGenerated,Source,Message -AutoSize | Out-String"
    '"'
)


@dataclass(frozen=True)
class DiagDef:
    """One diagnostic command, optionally with a Linux and/or Windows variant."""

    name: str                     # e.g. "diag.df"
    description: str
    linux: str | None = None       # /bin/sh command run on Linux agent
    windows: str | None = None     # cmd.exe / powershell command on Windows agent
    timeout_s: int = 30
    manualinput: bool = False
    manualinput_prompt: str = ""
    manualinput_validator: str = ""
    manualinput_default_value: str = ""
    manualinput_arg_name: str = ""

    def menu_path(self, os_kind: str) -> str:
        return f"{_MENU_PATH_PREFIX}/{_OS_DISPLAY[os_kind]}"

    def command_for(self, os_kind: str) -> str | None:
        return {"linux": self.linux, "windows": self.windows}.get(os_kind)

    @property
    def supported_os(self) -> list[str]:
        out: list[str] = []
        if self.linux:
            out.append("linux")
        if self.windows:
            out.append("windows")
        return out


# ---------------------------------------------------------------------------
# Diag catalog. Each definition lists the per-OS shell command. Set windows=None
# for diags that don't have a sensible Windows equivalent (mysqladmin, apache).
# ---------------------------------------------------------------------------
DIAG_DEFINITIONS: list[DiagDef] = [
    DiagDef("diag.df", "Disk usage on the host.",
            linux="df -hP",
            windows="powershell -NoProfile -Command \"Get-PSDrive -PSProvider FileSystem | "
                    "Select Name,@{n='UsedGB';e={[math]::Round($_.Used/1GB,1)}},"
                    "@{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}} | "
                    "Format-Table -AutoSize | Out-String\""),
    DiagDef("diag.free", "Memory usage.",
            linux="free -m",
            windows="powershell -NoProfile -Command \"Get-CimInstance Win32_OperatingSystem | "
                    "Select TotalVisibleMemorySize,FreePhysicalMemory,TotalVirtualMemorySize,"
                    "FreeVirtualMemory | Format-List | Out-String\""),
    DiagDef("diag.uptime", "System uptime and load.",
            linux="uptime",
            windows="powershell -NoProfile -Command \"$os = Get-CimInstance Win32_OperatingSystem; "
                    "$up = (Get-Date) - $os.LastBootUpTime; "
                    "'Up {0} days {1} hours, since {2}' -f "
                    "$up.Days,$up.Hours,$os.LastBootUpTime\""),
    DiagDef("diag.top", "Top CPU/memory processes.",
            linux="top -bn1 2>/dev/null | head -30",
            windows="powershell -NoProfile -Command \"Get-Process | Sort WS -Desc | "
                    "Select -First 30 Name,Id,"
                    "@{n='WS_MB';e={[math]::Round($_.WorkingSet/1MB,1)}},"
                    "@{n='CPU_s';e={[math]::Round($_.CPU,1)}} | "
                    "Format-Table -AutoSize | Out-String\""),
    DiagDef("diag.dmesg_tail", "Recent kernel/system events.",
            linux="dmesg -T 2>/dev/null | tail -100",
            windows="powershell -NoProfile -Command \"Get-EventLog System -Newest 100 "
                    "-EA SilentlyContinue | Format-Table TimeGenerated,EntryType,Source,Message "
                    "-AutoSize | Out-String\""),
    DiagDef("diag.ss_listen", "Listening sockets / TCP connections.",
            linux="ss -tunap 2>/dev/null",
            windows="powershell -NoProfile -Command \"Get-NetTCPConnection -State Listen "
                    "-EA SilentlyContinue | Select LocalAddress,LocalPort,OwningProcess | "
                    "Sort LocalPort | Format-Table -AutoSize | Out-String\""),
    DiagDef("diag.ps_aux", "Process list sorted by memory, top 40.",
            linux="ps auxf --sort=-%mem 2>/dev/null | head -40",
            windows="powershell -NoProfile -Command \"Get-Process | Sort WS -Desc | "
                    "Select -First 40 Name,Id,@{n='WS_MB';e={[math]::Round($_.WorkingSet/1MB,1)}},"
                    "Path | Format-Table -AutoSize | Out-String\""),
    DiagDef("diag.iostat",
            "I/O statistics (Linux: iostat; Windows: per-disk perf counters).",
            linux="iostat -xz 1 2 2>/dev/null",
            windows="powershell -NoProfile -Command \"Get-Counter -Counter "
                    "'\\PhysicalDisk(*)\\Disk Bytes/sec',"
                    "'\\PhysicalDisk(*)\\Avg. Disk Queue Length' "
                    "-MaxSamples 2 -EA SilentlyContinue | "
                    "Format-List | Out-String\""),
    DiagDef("diag.mysql_status", "MySQL server status (Linux only).",
            linux="mysqladmin --defaults-file=/etc/zabbix/.my.cnf status 2>/dev/null",
            windows=None),
    DiagDef("diag.mysql_processlist", "MySQL SHOW FULL PROCESSLIST (Linux only).",
            linux="mysql --defaults-file=/etc/zabbix/.my.cnf "
                  "-e 'SHOW FULL PROCESSLIST' 2>/dev/null",
            windows=None),
    DiagDef("diag.apache_status", "Apache server-status (Linux only).",
            linux="curl -s http://127.0.0.1/server-status?auto 2>/dev/null",
            windows=None),
    DiagDef("diag.systemctl_status",
            "systemctl status <unit> (Linux) / Get-Service status (Windows).",
            linux="systemctl status {MANUALINPUT} --no-pager 2>/dev/null",
            windows="powershell -NoProfile -Command \"Get-Service '{MANUALINPUT}' "
                    "-EA SilentlyContinue | Format-List Name,DisplayName,Status,StartType | "
                    "Out-String\"",
            manualinput=True,
            manualinput_prompt="service / unit name",
            manualinput_validator=r"^[a-zA-Z0-9._@ -]{1,64}$",
            manualinput_arg_name="unit"),
    DiagDef("diag.journal_tail",
            "Last N lines of journalctl (Linux) / system event log (Windows).",
            linux="journalctl -n {MANUALINPUT} --no-pager 2>/dev/null",
            windows="powershell -NoProfile -Command \"Get-EventLog System -Newest "
                    "{MANUALINPUT} -EA SilentlyContinue | "
                    "Format-Table TimeGenerated,EntryType,Source,Message -AutoSize | Out-String\"",
            manualinput=True,
            manualinput_prompt="line count",
            manualinput_validator=r"^[1-9][0-9]{0,3}$",
            manualinput_default_value="200",
            manualinput_arg_name="lines"),
    # Consolidated first-look snapshot. Bundles uptime, df, free, top, ps,
    # ss, dmesg into one execution so the AI doesn't need 7 round trips for
    # a typical first triage. Individual diags remain available for follow-up.
    DiagDef("diag.snapshot",
            "One-shot snapshot — uptime, df, free, top, ps, ss, dmesg/event log "
            "in a single response. Use this first; drill in with specific tools after.",
            linux=_LINUX_SNAPSHOT,
            windows=_WINDOWS_SNAPSHOT),
]


@dataclass
class ScriptIndex:
    """Maps `(diag_name, os_kind) → scriptid` plus the original DiagDefs."""
    by_name: dict[str, dict[str, str]] = field(default_factory=dict)
    defs_by_name: dict[str, DiagDef] = field(default_factory=dict)

    def scriptid(self, diag_name: str, os_kind: str) -> str | None:
        return self.by_name.get(diag_name, {}).get(os_kind)

    def diagdef(self, diag_name: str) -> DiagDef | None:
        return self.defs_by_name.get(diag_name)


def _build_create_params(d: DiagDef, os_kind: str) -> dict:
    command = d.command_for(os_kind)
    assert command is not None  # caller guards on supported_os
    params: dict = {
        "name": d.name,
        "menu_path": d.menu_path(os_kind),
        "type": 0,                 # 0 = Script
        "scope": 2,                # 2 = Manual host action (script.execute)
        "execute_on": 0,           # 0 = Zabbix agent
        "command": command,
        "host_access": 2,
        "usrgrpid": "0",
        "groupid": "0",
        "timeout": _TIMEOUT,        # Zabbix 7.4 enforces this default
    }
    if d.manualinput:
        params.update({
            "manualinput": "1",
            "manualinput_prompt": d.manualinput_prompt or "value",
            "manualinput_validator_type": "0",
            "manualinput_validator": d.manualinput_validator or ".*",
            "manualinput_default_value": d.manualinput_default_value,
        })
    return params


async def _delete_legacy_scripts(client: ZabbixClient) -> None:
    """Drop the old `rca-ai.diag.*` flat-namespace scripts created by an
    earlier version of this code. Idempotent: noop if none exist."""
    legacy = await client.call("script.get", {
        "output": ["scriptid", "name"],
        "search": {"name": "rca-ai.diag."},
        "startSearch": True,
    })
    if legacy:
        await client.call(
            "script.delete", [s["scriptid"] for s in legacy],
        )


async def ensure_diag_scripts(client: ZabbixClient,
                               defs: list[DiagDef] | None = None) -> ScriptIndex:
    """Create any missing `zabbix-AI/<OS>/<diag>` scripts; return an index."""
    if defs is None:
        defs = DIAG_DEFINITIONS

    await _delete_legacy_scripts(client)

    wanted_paths = [
        f"{_MENU_PATH_PREFIX}/{display}"
        for display in _OS_DISPLAY.values()
    ]
    wanted_names = [d.name for d in defs]

    existing = await client.call("script.get", {
        "filter": {"name": wanted_names, "menu_path": wanted_paths},
        "output": ["scriptid", "name", "menu_path"],
    }) if wanted_names else []
    have: dict[tuple[str, str], str] = {
        (row["name"], row["menu_path"]): row["scriptid"] for row in existing
    }

    index = ScriptIndex()
    for d in defs:
        index.defs_by_name[d.name] = d
        for os_kind in d.supported_os:
            mp = d.menu_path(os_kind)
            sid = have.get((d.name, mp))
            if sid is None:
                res = await client.call(
                    "script.create", _build_create_params(d, os_kind),
                )
                sid = res["scriptids"][0]
            index.by_name.setdefault(d.name, {})[os_kind] = sid
    return index
