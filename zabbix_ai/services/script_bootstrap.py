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
# The Zabbix item `mailqueue.outgoing` counts files directly inside
# `Queues\SMTP\Outgoing\` (NOT recursive into Messages, NOT SF\Outgoing).
# These are tiny (~500 byte) envelope files in MailEnable's key=value
# format with Sender, Recipients, CommandType (NDR/DELIVER), StatusCode,
# Retries, Subject and Status — much more useful than RFC822 headers.
#
# Example envelope file content:
#   DomainName=foo.com
#   CommandType=NDR
#   StatusCode=4
#   Recipients=[SMTP:bob@foo.com]
#   Sender=[SMTP:alice@foo.com]
#   Retries=3
#   Status=Unsent
#
# Files are tiny so we scan all of them. On busy hosts with >5K queued
# the 30s Zabbix script timeout may bite; AI sees timeout as queue-bloat
# evidence. No subject-line / body output (privacy option B).
_WINDOWS_MAIL_QUEUE_PS = (
    "$base=@('C:\\Program Files (x86)\\Mail Enable\\Queues',"
    "'C:\\Program Files\\Mail Enable\\Queues')|?{Test-Path $_}|select -First 1;"
    "if(-not $base){'MailEnable queue not found';exit};"
    "\"Base: $base\";'';"
    "'=== counts per subfolder (recursive) ===';"
    "gci $base -Directory -EA 0|%{"
    "$n=(gci $_.FullName -Filter *.MAI -File -Recurse -EA 0).Count;"
    "if($n -gt 0){[pscustomobject]@{Folder=$_.Name;Total=$n}}"
    "}|sort Total -Desc|ft -A|Out-String;"
    "$out=Join-Path $base 'SMTP\\Outgoing';"
    "if(Test-Path $out){"
    "$all=gci $out -Filter *.MAI -File -EA 0;"
    "$total=$all.Count;"
    "$f=$all|sort LastWriteTime -Desc|select -First 2000;"
    "if($total -gt $f.Count){"
    "\"=== queue total $total envelope files; scanning $($f.Count) most-recent for breakdown ===\""
    "}else{\"=== scanning all $total envelope files ===\"};"
    "'';"
    "$now=Get-Date;"
    # Use [IO.File]::ReadAllText (much faster than Get-Content per file) and
    # one regex per field rather than line-by-line iteration.
    "$r=$f|%{"
    "$d=[int]($now-$_.LastWriteTime).TotalDays;"
    "$b=if($d -lt 1){'today_lt1d'}elseif($d -lt 7){'recent_1-7d'}"
    "elseif($d -lt 30){'mid_7-30d'}elseif($d -lt 365){'old_30-365d'}"
    "else{'stale_gt1y'};"
    "$c=[IO.File]::ReadAllText($_.FullName);"
    "$sn=$null;$rc=$null;$dm=$null;$ct=$null;$st=$null;$sc=$null;$rt=$null;"
    "if($c -match '(?m)^Sender=(.*)$'){$sn=$Matches[1].Trim()};"
    "if($c -match '(?m)^Recipients=(.*)$'){$rc=$Matches[1].Trim()};"
    "if($c -match '(?m)^DomainName=(.*)$'){$dm=$Matches[1].Trim()};"
    "if($c -match '(?m)^CommandType=(.*)$'){$ct=$Matches[1].Trim()};"
    "if($c -match '(?m)^Status=(.*)$'){$st=$Matches[1].Trim()};"
    "if($c -match '(?m)^StatusCode=(.*)$'){$sc=$Matches[1].Trim()};"
    "if($c -match '(?m)^Retries=(.*)$'){$rt=$Matches[1].Trim()};"
    "[pscustomobject]@{B=$b;D=$d;Sender=$sn;Recipients=$rc;Domain=$dm;"
    "CommandType=$ct;Status=$st;StatusCode=$sc;Retries=$rt}"
    "};"
    "'=== age distribution ===';"
    "$r|group B|sort Name|ft Name,Count -A|Out-String;"
    "'=== command type (NDR vs DELIVER) ===';"
    "$r|group CommandType|sort Count -Desc|ft Count,Name -A|Out-String;"
    "'=== status code distribution ===';"
    "$r|group StatusCode|sort Count -Desc|ft Count,Name -A|Out-String;"
    "'=== retry count distribution ===';"
    "$r|group Retries|sort Name|ft Name,Count -A|Out-String;"
    "'=== top 20 destination domains ===';"
    "$r|?{$_.Domain}|group Domain|sort Count -Desc|select -First 20|"
    "ft Count,Name -A|Out-String;"
    "'=== top 20 senders (envelope) ===';"
    "$r|?{$_.Sender}|group Sender|sort Count -Desc|select -First 20|"
    "ft Count,Name -A|Out-String;"
    "'=== top 20 recipients (envelope) ===';"
    "$r|?{$_.Recipients}|group Recipients|sort Count -Desc|select -First 20|"
    "ft Count,Name -A|Out-String;"
    "foreach($k in @('today_lt1d','recent_1-7d','mid_7-30d','old_30-365d','stale_gt1y')){"
    "$br=$r|?{$_.B -eq $k};"
    "if($br){"
    "\"=== $k - $(@($br).Count) messages — top 5 destinations ===\";"
    "$br|?{$_.Domain}|group Domain|sort Count -Desc|select -First 5|"
    "ft Count,Name -A|Out-String"
    "}};"
    "'=== oldest 5 ==='; $f|sort LastWriteTime|"
    "select -First 5 Name,LastWriteTime|ft -A|Out-String;"
    "'=== newest 5 ==='; $f|sort LastWriteTime -Desc|"
    "select -First 5 Name,LastWriteTime|ft -A|Out-String"
    "}"
)

# Linux network state — interfaces, routes, DNS config + resolve test, default
# gateway reachability. Used as an early triage step when symptoms look like
# "site down" / "API unreachable" before drilling into apps.
_LINUX_NETWORK = """\
echo '=== ip a (brief) ==='; ip -brief a 2>/dev/null
echo '=== ip route ==='; ip route 2>/dev/null
echo '=== resolv.conf ==='; cat /etc/resolv.conf 2>/dev/null
echo '=== DNS resolve test ==='
for d in $(awk '/^nameserver/ {print $2}' /etc/resolv.conf 2>/dev/null); do
  echo "[$d]"; timeout 2 nslookup google.com $d 2>&1 | tail -3
done
echo '=== default gw reachability ==='
gw=$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}')
[ -n "$gw" ] && timeout 3 ping -c 2 -W 1 $gw 2>&1 | tail -2
"""

# Windows network state PowerShell — encoded once at bootstrap. Built up as
# concatenated chunks to stay within the project's 100-char line limit.
_WINDOWS_NETWORK_PS = (
    "Get-NetIPAddress -AddressFamily IPv4 | "
    "Select-Object InterfaceAlias,IPAddress,PrefixLength | "
    "Format-Table | Out-String;"
    "Get-NetRoute -DestinationPrefix 0.0.0.0/0 | "
    "Select-Object NextHop,InterfaceAlias,RouteMetric | "
    "Format-Table | Out-String;"
    "Get-DnsClientServerAddress -AddressFamily IPv4 | "
    "Where-Object {$_.ServerAddresses} | "
    "Format-Table InterfaceAlias,ServerAddresses | Out-String;"
    "try { Resolve-DnsName google.com -QuickTimeout -ErrorAction Stop | "
    "Select-Object -First 3 | Format-Table | Out-String } "
    "catch { \"DNS resolve failed: $_\" };"
    "$gw=(Get-NetRoute -DestinationPrefix 0.0.0.0/0 | "
    "Select-Object -First 1).NextHop;"
    "if ($gw) { \"ping $gw: \" + (Test-Connection $gw -Count 2 -Quiet) }"
)

# Linux TLS cert-expiry probe. Iterates the operator-supplied host:port list
# (`{MANUALINPUT}` — Zabbix substitutes literally; the tool wrapper validates).
_LINUX_CERT_EXPIRY = (
    "IFS=',' read -ra eps <<< \"{MANUALINPUT}\"\n"
    "for ep in \"${eps[@]}\"; do\n"
    "  echo \"=== $ep ===\"\n"
    "  timeout 5 bash -c \"echo | openssl s_client -servername ${ep%:*} "
    "-connect $ep 2>/dev/null | openssl x509 -noout -dates -subject 2>&1\""
    " || echo \"  (failed or timed out)\"\n"
    "done"
)

# Windows TLS cert-expiry — manualinput-using diags can't be `_ps_encoded`
# because Zabbix substitutes `{MANUALINPUT}` on the literal command string
# (encoding happens once at bootstrap time, before substitution). Keep the
# script inline; the tool-side regex constrains input characters tightly.
_WINDOWS_CERT_EXPIRY = (
    "powershell -NoProfile -Command "
    "\"$ProgressPreference='SilentlyContinue';"
    "$ErrorActionPreference='SilentlyContinue';"
    "$eps='{MANUALINPUT}' -split ',';"
    "foreach ($ep in $eps) {"
    "$h,$p=$ep -split ':';"
    "\\\"=== $ep ===\\\";"
    "try {"
    "$tcp=New-Object Net.Sockets.TcpClient;"
    "$tcp.Connect($h,[int]$p);"
    "$ssl=New-Object Net.Security.SslStream($tcp.GetStream(),$false,({$true}));"
    "$ssl.AuthenticateAsClient($h);"
    "$cert=$ssl.RemoteCertificate;"
    "$cert2=[Security.Cryptography.X509Certificates.X509Certificate2]::new($cert);"
    "'Subject : ' + $cert2.Subject;"
    "'Issuer  : ' + $cert2.Issuer;"
    "'NotBefore: ' + $cert2.NotBefore;"
    "'NotAfter : ' + $cert2.NotAfter;"
    "$ssl.Dispose(); $tcp.Dispose();"
    "} catch { '  error: ' + $_ }"
    "}\""
)

# Linux SMART health probe. Gracefully no-ops when smartmontools isn't
# installed (printing an install hint) rather than failing the whole call.
_LINUX_SMART = (
    "if ! command -v smartctl >/dev/null 2>&1; then\n"
    "  echo 'smartmontools not installed — run: apt install smartmontools "
    "(or yum install smartmontools)'; exit 0\n"
    "fi\n"
    "smartctl --scan 2>/dev/null | awk '{print $1}' | while read -r d; do\n"
    "  echo \"=== $d ===\"\n"
    "  sudo -n smartctl -H -i -A \"$d\" 2>&1 | "
    "grep -E 'Device Model|Model Number|User Capacity|Power_On_Hours|"
    "Reallocated_Sector|Current_Pending|SMART overall|test result' || true\n"
    "done"
)

# Windows SMART/storage-health PowerShell — uses Get-PhysicalDisk +
# Get-StorageReliabilityCounter (NVMe/SATA both supported).
_WINDOWS_SMART_PS = (
    "Get-PhysicalDisk | "
    "Select-Object FriendlyName,HealthStatus,OperationalStatus,MediaType,"
    "@{N='Size_GB';E={[math]::Round($_.Size/1GB,1)}},SerialNumber | "
    "Format-Table | Out-String;"
    "Get-PhysicalDisk | ForEach-Object {"
    "\"=== $($_.FriendlyName) ===\";"
    "$_ | Get-StorageReliabilityCounter 2>$null | "
    "Format-List PowerOnHours,Wear,ReadErrorsTotal,WriteErrorsTotal,"
    "Temperature | Out-String"
    "}"
)

# Linux disk usage: which folders are eating the drive?
# `du -hxd 3 /` stays on one filesystem, walks 3 levels deep, prints
# sizes; sort+head returns the 40 biggest paths. Wrapped in `timeout`
# because deep du on a large drive can pin the agent. AllowKey must
# match exactly — see deploy/zabbix-agent/zabbix-ai-diag.conf.
_LINUX_DISK_USAGE = (
    "echo '=== filesystems ==='; df -hP 2>/dev/null; echo; "
    "echo '=== top 40 dirs by size (depth 3, max 30s) ==='; "
    "timeout 30 du -hxd 3 / 2>/dev/null | sort -hr | head -40"
)

# Windows disk usage: per-drive used/free + top 15 folders by recursive
# size on each fixed drive. Capped at depth-3 to bound walk time on
# large NTFS volumes — Get-ChildItem -Recurse on a 200 GB drive can
# take minutes otherwise. Errors swallowed so a locked subdir doesn't
# blank the whole report.
_WINDOWS_DISK_USAGE_PS = (
    # FSO-only, fast. NO robocopy fallback — that pushed us past the 30 s
    # Zabbix script-timeout ceiling. Folders FSO refuses (silent 0 from
    # access-denied subdirs) are labelled "n/a — call diag.windows_winsxs"
    # so the AI knows to chain. A trailing 'unaccounted' summary tells
    # the model how much disk is hiding in those folders.
    "$ProgressPreference='SilentlyContinue';"
    "$ErrorActionPreference='SilentlyContinue';"
    "Write-Output '=== drive summary ===';"
    "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
    "Select-Object @{N='Drive';E={$_.DeviceID}},"
    "  @{N='UsedGB';E={[math]::Round(($_.Size-$_.FreeSpace)/1GB,1)}},"
    "  @{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,1)}},"
    "  @{N='TotalGB';E={[math]::Round($_.Size/1GB,1)}},"
    "  @{N='PctUsed';E={"
    "    if($_.Size){[math]::Round(100*($_.Size-$_.FreeSpace)/$_.Size,1)}"
    "    else{0}}} | "
    "Format-Table -AutoSize | Out-String;"
    "$fso = New-Object -ComObject Scripting.FileSystemObject;"
    "$disks = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3';"
    "foreach ($disk in $disks) {"
    "  $d = $disk.DeviceID;"
    "  $usedGB = ($disk.Size - $disk.FreeSpace)/1GB;"
    "  Write-Output \"=== $d top folders (FSO size, ~10s budget) ===\";"
    "  $sw = [Diagnostics.Stopwatch]::StartNew();"
    "  $rows = New-Object Collections.ArrayList;"
    "  $skipped = 0; $measuredBytes = 0;"
    "  Get-ChildItem -LiteralPath \"$d\\\\\" -Directory -Force "
    "    -ErrorAction SilentlyContinue | "
    "  ForEach-Object {"
    "    if ($sw.ElapsedMilliseconds -gt 18000) { $skipped++; return }"
    "    $size = $null; $status = 'fso';"
    "    try { $size = $fso.GetFolder($_.FullName).Size } "
    "    catch { $status = 'n/a (access denied)' }"
    "    if (-not $size -or $size -eq 0) {"
    "      $status = 'n/a (call diag.windows_winsxs)'"
    "    }"
    "    if ($size -and $size -gt 0) { $measuredBytes += $size }"
    "    [void]$rows.Add([PSCustomObject]@{"
    "      Path = $_.FullName;"
    "      SizeGB = if ($size) {[math]::Round($size/1GB,2)} else {0};"
    "      Notes = $status"
    "    })"
    "  };"
    "  $rows | Sort-Object SizeGB -Descending | Select-Object -First 15 | "
    "    Format-Table -AutoSize | Out-String;"
    "  $unaccGB = [math]::Round($usedGB - ($measuredBytes/1GB), 1);"
    "  $usedR = [math]::Round($usedGB,1);"
    "  $measR = [math]::Round($measuredBytes/1GB,1);"
    "  Write-Output (\"  Drive $d total used: $usedR GB, \" + "
    "    \"measured: $measR GB, unaccounted: $unaccGB GB\");"
    "  if ($unaccGB -gt 5) {"
    "    Write-Output (\"  WARN $unaccGB GB unaccounted for - the bulk \" + "
    "      \"is almost certainly hiding in C:\\Windows or C:\\Users \" + "
    "      \"(FSO can't measure those due to access-denied subdirs). \" + "
    "      \"Call diag.windows_winsxs NEXT to size WinSxS / Software\" + "
    "      \"Distribution / Installer / pagefile etc.\")"
    "  }"
    "  if ($skipped) { Write-Output \"  ($skipped folder(s) skipped — budget exhausted)\" }"
    "}"
)

# Windows-only: deep look at the largest space consumers under C:\Windows
# (WinSxS, SoftwareDistribution, system32\config, etc.) plus pagefile /
# hibernation. Use this when diag.disk_usage reports a large 'unaccounted'
# delta — the bulk of Windows space is in places FSO refuses to size.
_WINDOWS_WINSXS_PS = (
    # Tight body to stay well under cmd.exe's 8191-char encoded-command
    # limit. Recovery commands (cleanmgr / DISM / powercfg) live in the
    # tool DESCRIPTION instead — the AI reads them from its tool list,
    # not from script output. Each path runs in its own Start-Job
    # bounded by Wait-Job -Timeout 6; FSO size only, no robocopy
    # fallback (would explode the script size and re-trip 30s).
    "$b={param($p)"
    "if(!(Test-Path -LiteralPath $p)){return @{s=0;t='absent'}}"
    "$i=Get-Item -LiteralPath $p -Force -EA 0;"
    "if(!$i){return @{s=0;t='inaccessible'}}"
    "if(!$i.PSIsContainer){return @{s=$i.Length;t='file'}}"
    "$f=New-Object -ComObject Scripting.FileSystemObject;"
    "$s=0;try{$s=$f.GetFolder($p).Size}catch{}"
    "return @{s=$s;t=if($s){'fso'}else{'n/a (FSO denied)'}}};"
    "Write-Output '=== Windows space consumers (6s/target budget) ===';"
    "$T=\"$env:SystemRoot\",\"$env:SystemDrive\";"
    "$paths=@("
    "  $T[0]+'\\WinSxS', $T[0]+'\\SoftwareDistribution\\Download',"
    "  $T[0]+'\\Temp', $T[0]+'\\Logs', $T[0]+'\\Installer',"
    "  $T[0]+'\\System32\\config', $T[0]+'\\Panther',"
    "  $T[0]+'\\System32\\winevt\\Logs',"
    "  $T[1]+'\\hiberfil.sys', $T[1]+'\\pagefile.sys',"
    "  $T[1]+'\\swapfile.sys');"
    "$rows=New-Object Collections.ArrayList;$total=0;"
    "foreach($p in $paths){"
    "  $j=Start-Job $b -ArgumentList $p;"
    "  if(Wait-Job $j -Timeout 6){"
    "    $r=Receive-Job $j;Remove-Job $j -Force;"
    "    $sz=if($r.s){$r.s}else{0};if($sz){$total+=$sz};"
    "    [void]$rows.Add([PSCustomObject]@{"
    "      Path=$p;"
    "      SizeGB=if($sz){[math]::Round($sz/1GB,2)}else{0};"
    "      Source=$r.t})"
    "  }else{"
    "    Stop-Job $j -EA 0;Remove-Job $j -Force -EA 0;"
    "    [void]$rows.Add([PSCustomObject]@{"
    "      Path=$p;SizeGB=0;Source='(timed out 6s)'})"
    "  }};"
    "$rows | Format-Table -AutoSize | Out-String;"
    "\"Total measured: $([math]::Round($total/1GB,2)) GB.\""
)

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
    DiagDef("diag.network",
            "Network state: interfaces, routes, DNS config, DNS resolve test, "
            "default gateway reachability. Use early when symptoms are "
            "'site down' or 'API unreachable' before drilling into apps.",
            linux=_LINUX_NETWORK,
            windows=_ps_encoded(_WINDOWS_NETWORK_PS)),
    DiagDef("diag.cert_expiry",
            "TLS certificate expiry for one or more endpoints (host:port). "
            "Returns subject + NotBefore/NotAfter dates. Use when a service "
            "may be failing due to expired certs.",
            linux=_LINUX_CERT_EXPIRY,
            windows=_WINDOWS_CERT_EXPIRY,
            manualinput=True,
            manualinput_prompt="comma-separated host:port (max 10)",
            manualinput_validator=
                r"^[A-Za-z0-9.\-]+:[0-9]{1,5}(,[A-Za-z0-9.\-]+:[0-9]{1,5}){0,9}$",
            manualinput_arg_name="endpoints"),
    DiagDef("diag.smart",
            "SMART health and wear indicators for all physical disks. Use "
            "proactively when investigating I/O errors, slow performance, or "
            "before disk-intensive operations.",
            linux=_LINUX_SMART,
            windows=_ps_encoded(_WINDOWS_SMART_PS)),
    DiagDef("diag.disk_usage",
            "Top folders / directories by size — answers 'what's filling "
            "the disk?'. Linux: `du -hxd 3 /` for top 40 paths. Windows: "
            "per-fixed-drive used/free plus top 15 folders (depth 3). "
            "Call this on any disk-space alert before suggesting RDP/SSH.",
            linux=_LINUX_DISK_USAGE,
            windows=_ps_encoded(_WINDOWS_DISK_USAGE_PS)),
    DiagDef("diag.windows_winsxs",
            "Windows-only follow-up to diag.disk_usage when the bulk of "
            "C: usage is unaccounted-for (i.e. hiding in C:\\Windows). "
            "Sizes WinSxS component store, SoftwareDistribution\\Download, "
            "Installer cache, System32\\config, event logs, plus "
            "hiberfil.sys / pagefile.sys / swapfile.sys. Reports pending "
            "Windows updates and prints (does NOT execute) the cleanmgr / "
            "DISM / powercfg recovery commands an operator would run.",
            windows=_ps_encoded(_WINDOWS_WINSXS_PS)),
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
