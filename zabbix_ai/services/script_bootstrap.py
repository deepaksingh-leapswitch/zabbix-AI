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

import base64
from dataclasses import dataclass, field

from zabbix_ai.clients.zabbix import ZabbixClient

_MENU_PATH_PREFIX = "zabbix-AI"
_OS_DISPLAY = {"linux": "Linux", "windows": "Windows"}
# Zabbix 7.4 caps script.create timeout at 30s; we reject larger values.
_TIMEOUT = "30s"

# Preamble applied to every Windows PowerShell script. Suppresses the
# CLIXML progress-record noise that appears when PowerShell runs as a
# child of cmd.exe and quietly drops non-fatal errors so the captured
# stdout stays human-readable.
_PS_PREAMBLE = (
    "$ProgressPreference='SilentlyContinue';"
    "$ErrorActionPreference='SilentlyContinue';"
)


def _ps_encoded(script: str) -> str:
    """Wrap a PowerShell script as a cmd.exe-safe `-EncodedCommand` line.

    Quote escaping when nesting PowerShell inside `cmd.exe /c "..."` (which
    is what Zabbix system.run does on Windows) is fragile: long inline
    commands break or time out. PowerShell's `-EncodedCommand` accepts a
    base64-of-UTF-16LE blob and avoids all escaping issues.
    """
    full = _PS_PREAMBLE + script
    encoded = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -OutputFormat Text -EncodedCommand {encoded}"

# Linux mail-queue inspection. Auto-detects Postfix / Exim / sendmail-compat
# mailq. Returns counts + top 20 senders + queue-id/timestamps. No subject
# lines; sender/recipient envelope addresses only (option B per design).
_LINUX_MAIL_QUEUE = """\
if command -v postqueue >/dev/null 2>&1; then \
    echo '=== Postfix queue summary ==='; \
    postqueue -p 2>/dev/null | tail -3; \
    echo; \
    echo '=== top 20 senders ==='; \
    postqueue -p 2>/dev/null | awk '/^[A-F0-9]+[*!]?[[:space:]]/{print $NF}' \
        | sort | uniq -c | sort -rn | head -20; \
    echo; \
    echo '=== oldest 10 messages ==='; \
    postqueue -p 2>/dev/null | awk '/^[A-F0-9]+[*!]?[[:space:]]/{print $3,$4,$5,$6,"  ",$NF}' \
        | head -10; \
elif command -v exim >/dev/null 2>&1; then \
    echo '=== Exim queue ==='; \
    exim -bpc 2>/dev/null; echo; \
    exim -bp 2>/dev/null | awk '/^[0-9a-z]+ [0-9]+/ && NF==4 {print $NF}' \
        | sort | uniq -c | sort -rn | head -20; \
elif command -v mailq >/dev/null 2>&1; then \
    echo '=== mailq output ==='; \
    mailq 2>/dev/null | head -50; \
else \
    echo 'No supported MTA found (looked for postfix postqueue, exim, mailq)'; \
fi"""

# Windows MailEnable queue inspection.
#
# MailEnable's actual delivery queue is `Queues\SF\Outgoing\Messages\` (the
# Store-and-Forward outbound spool with retry logic) — not `SMTP\Outgoing\`,
# which contains submitted-but-not-yet-routed messages and historical stores.
# The Zabbix item that reports the alert "Mail Queue count" maps to SF.
#
# .MAI files in SF\\Outgoing are full RFC822 messages, so we extract the
# `From:` and `To:` headers (envelope addresses are not preserved in this
# folder). No subject lines or message bodies — privacy option B.
# Sample bounded to 200 most-recent files; works on queues of tens of thousands.
_WINDOWS_MAIL_QUEUE_PS = """\
$bases = @(
  'C:\\Program Files (x86)\\Mail Enable\\Queues',
  'C:\\Program Files\\Mail Enable\\Queues')
$base = $bases | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $base) { 'MailEnable queue not found'; exit }
Write-Output "Base: $base"
Write-Output ''
Write-Output '=== counts per top-level subfolder ==='
Get-ChildItem $base -Directory -EA SilentlyContinue | ForEach-Object {
  $sub = (Get-ChildItem $_.FullName -Filter *.MAI -File -Recurse `
          -EA SilentlyContinue).Count
  if ($sub -gt 0) {
    [PSCustomObject]@{ Folder = $_.Name; Total = $sub }
  }
} | Sort Total -Desc | Format-Table -AutoSize | Out-String
# The active outbound delivery queue
$sf = Join-Path $base 'SF\\Outgoing\\Messages'
if (Test-Path $sf) {
  $files = Get-ChildItem $sf -Filter *.MAI -File -EA SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 200
  Write-Output ('=== sampled ' + $files.Count + ' newest SF\\Outgoing\\Messages files ===')
  Write-Output ''
  Write-Output '=== top 20 senders (From:) ==='
  # Grep envelope headers from first ~200 lines (covers DKIM-bloated headers).
  # Regex anchor ^ requires no -SimpleMatch flag.
  $files | ForEach-Object {
    $m = Select-String -Path $_.FullName `
          -Pattern '^From:\\s*(.+)$' -List -EA SilentlyContinue
    if ($m) { $m.Matches[0].Groups[1].Value.Trim() }
  } | Group-Object | Sort Count -Desc | Select -First 20 |
    Format-Table Count, Name -AutoSize | Out-String
  Write-Output '=== top 20 recipients (To:) ==='
  $files | ForEach-Object {
    $m = Select-String -Path $_.FullName `
          -Pattern '^To:\\s*(.+)$' -List -EA SilentlyContinue
    if ($m) { $m.Matches[0].Groups[1].Value.Trim() }
  } | Group-Object | Sort Count -Desc | Select -First 20 |
    Format-Table Count, Name -AutoSize | Out-String
  Write-Output '=== age distribution (same sample) ==='
  $files | ForEach-Object {
    $days = [int]((Get-Date) - $_.LastWriteTime).TotalDays
    if ($days -lt 1) { '<1 day' }
    elseif ($days -lt 7) { '1-7 days' }
    elseif ($days -lt 30) { '7-30 days' }
    elseif ($days -lt 365) { '30-365 days' }
    else { '>1 year' }
  } | Group-Object | Sort Name |
    Format-Table Name, Count -AutoSize | Out-String
  Write-Output '=== sample timestamps (oldest 5, newest 5) ==='
  $files | Sort-Object LastWriteTime |
    Select-Object -First 5 -Property Name, LastWriteTime |
    Format-Table -AutoSize | Out-String
  $files | Sort-Object LastWriteTime -Descending |
    Select-Object -First 5 -Property Name, LastWriteTime |
    Format-Table -AutoSize | Out-String
}
"""

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

# Windows snapshot — multi-line PowerShell, encoded once at bootstrap time.
_WINDOWS_SNAPSHOT_PS = """\
Write-Output '=== uptime ==='
$os = Get-CimInstance Win32_OperatingSystem
$up = (Get-Date) - $os.LastBootUpTime
"Up {0} days {1} hours, since {2}" -f $up.Days, $up.Hours, $os.LastBootUpTime
Write-Output '=== drives ==='
Get-PSDrive -PSProvider FileSystem | Select Name,
  @{n='UsedGB';e={[math]::Round($_.Used/1GB,1)}},
  @{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}} |
  Format-Table -AutoSize | Out-String
Write-Output '=== memory ==='
$mem = Get-CimInstance Win32_OperatingSystem
"Total MB:    {0}" -f [math]::Round($mem.TotalVisibleMemorySize/1024)
"Free MB:     {0}" -f [math]::Round($mem.FreePhysicalMemory/1024)
"Total VM MB: {0}" -f [math]::Round($mem.TotalVirtualMemorySize/1024)
"Free VM MB:  {0}" -f [math]::Round($mem.FreeVirtualMemory/1024)
Write-Output '=== top processes by working set ==='
Get-Process | Sort WS -Desc | Select -First 20 Name, Id,
  @{n='WS_MB';e={[math]::Round($_.WorkingSet/1MB,1)}},
  @{n='CPU_s';e={[math]::Round($_.CPU,1)}} |
  Format-Table -AutoSize | Out-String
Write-Output '=== listening ports (top 20) ==='
Get-NetTCPConnection -State Listen | Select LocalAddress, LocalPort, OwningProcess |
  Sort LocalPort | Select -First 20 | Format-Table -AutoSize | Out-String
Write-Output '=== last 30 system errors/warnings ==='
Get-EventLog System -EntryType Error, Warning -Newest 30 |
  Format-Table TimeGenerated, EntryType, Source, EventID, Message -AutoSize | Out-String
"""
_WINDOWS_SNAPSHOT = _ps_encoded(_WINDOWS_SNAPSHOT_PS)


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
            windows=_ps_encoded(
                "Get-PSDrive -PSProvider FileSystem | Select Name,"
                "@{n='UsedGB';e={[math]::Round($_.Used/1GB,1)}},"
                "@{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}} | "
                "Format-Table -AutoSize | Out-String",
            )),
    DiagDef("diag.free", "Memory usage.",
            linux="free -m",
            windows=_ps_encoded(
                "$mem = Get-CimInstance Win32_OperatingSystem;"
                "'Total MB:    {0}' -f [math]::Round($mem.TotalVisibleMemorySize/1024);"
                "'Free MB:     {0}' -f [math]::Round($mem.FreePhysicalMemory/1024);"
                "'Total VM MB: {0}' -f [math]::Round($mem.TotalVirtualMemorySize/1024);"
                "'Free VM MB:  {0}' -f [math]::Round($mem.FreeVirtualMemory/1024)",
            )),
    DiagDef("diag.uptime", "System uptime and load.",
            linux="uptime",
            windows=_ps_encoded(
                "$os=Get-CimInstance Win32_OperatingSystem;"
                "$up=(Get-Date)-$os.LastBootUpTime;"
                "'Up {0} days {1} hours, since {2}' -f "
                "$up.Days,$up.Hours,$os.LastBootUpTime",
            )),
    DiagDef("diag.top", "Top CPU/memory processes.",
            linux="top -bn1 2>/dev/null | head -30",
            windows=_ps_encoded(
                "Get-Process | Sort WS -Desc | Select -First 30 Name,Id,"
                "@{n='WS_MB';e={[math]::Round($_.WorkingSet/1MB,1)}},"
                "@{n='CPU_s';e={[math]::Round($_.CPU,1)}} | "
                "Format-Table -AutoSize | Out-String",
            )),
    DiagDef("diag.dmesg_tail", "Recent kernel/system events.",
            linux="dmesg -T 2>/dev/null | tail -100",
            windows=_ps_encoded(
                "Get-EventLog System -Newest 100 | "
                "Format-Table TimeGenerated,EntryType,Source,EventID,Message "
                "-AutoSize | Out-String",
            )),
    DiagDef("diag.ss_listen", "Listening sockets / TCP connections.",
            linux="ss -tunap 2>/dev/null",
            windows=_ps_encoded(
                "Get-NetTCPConnection -State Listen | "
                "Select LocalAddress,LocalPort,OwningProcess | "
                "Sort LocalPort | Format-Table -AutoSize | Out-String",
            )),
    DiagDef("diag.ps_aux", "Process list sorted by memory, top 40.",
            linux="ps auxf --sort=-%mem 2>/dev/null | head -40",
            windows=_ps_encoded(
                "Get-Process | Sort WS -Desc | Select -First 40 Name,Id,"
                "@{n='WS_MB';e={[math]::Round($_.WorkingSet/1MB,1)}},Path | "
                "Format-Table -AutoSize | Out-String",
            )),
    DiagDef("diag.iostat",
            "I/O statistics (Linux: iostat; Windows: per-disk perf counters).",
            linux="iostat -xz 1 2 2>/dev/null",
            windows=_ps_encoded(
                "Get-Counter -Counter "
                "'\\PhysicalDisk(*)\\Disk Bytes/sec',"
                "'\\PhysicalDisk(*)\\Avg. Disk Queue Length' "
                "-MaxSamples 2 | Format-List | Out-String",
            )),
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
    DiagDef("diag.mail_queue",
            "Mail queue inspection — counts, top senders, top recipients, "
            "age distribution. Linux: Postfix / Exim / sendmail-compat mailq. "
            "Windows: MailEnable. Envelope addresses only — no subject lines.",
            linux=_LINUX_MAIL_QUEUE,
            windows=_ps_encoded(_WINDOWS_MAIL_QUEUE_PS)),
    # Manualinput-using Windows diags can't use _ps_encoded because Zabbix
    # substitutes {MANUALINPUT} on the raw command string. The PowerShell
    # validator regex constrains the input to safe characters, so the inline
    # form is short enough to dodge escape problems.
    DiagDef("diag.systemctl_status",
            "systemctl status <unit> (Linux) / Get-Service status (Windows).",
            linux="systemctl status {MANUALINPUT} --no-pager 2>/dev/null",
            windows="powershell -NoProfile -Command "
                    "\"$ProgressPreference='SilentlyContinue';"
                    "$ErrorActionPreference='SilentlyContinue';"
                    "Get-Service '{MANUALINPUT}' | "
                    "Format-List Name,DisplayName,Status,StartType | "
                    "Out-String\"",
            manualinput=True,
            manualinput_prompt="service / unit name",
            manualinput_validator=r"^[a-zA-Z0-9._@ -]{1,64}$",
            manualinput_arg_name="unit"),
    DiagDef("diag.journal_tail",
            "Last N lines of journalctl (Linux) / system event log (Windows).",
            linux="journalctl -n {MANUALINPUT} --no-pager 2>/dev/null",
            windows="powershell -NoProfile -Command "
                    "\"$ProgressPreference='SilentlyContinue';"
                    "$ErrorActionPreference='SilentlyContinue';"
                    "Get-EventLog System -Newest {MANUALINPUT} | "
                    "Format-Table TimeGenerated,EntryType,Source,Message "
                    "-AutoSize | Out-String\"",
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
            params = _build_create_params(d, os_kind)
            if sid is None:
                res = await client.call("script.create", params)
                sid = res["scriptids"][0]
            else:
                # Keep the script body in sync with the in-code definition so
                # `command` / `manualinput*` updates propagate without manual
                # cleanup. script.update accepts the same params plus scriptid.
                update_params = {"scriptid": sid, **{
                    k: v for k, v in params.items() if k != "name"
                }}
                await client.call("script.update", update_params)
            index.by_name.setdefault(d.name, {})[os_kind] = sid
    return index
