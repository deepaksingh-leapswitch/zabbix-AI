# zabbix-rca-AI — Windows agent enablement script.
# Auto-detects Agent 1 ("Zabbix Agent") vs Agent 2 ("Zabbix Agent 2"),
# resolves the install directory from the service's binary path, and
# configures accordingly. Idempotent.
#
# Run as Administrator on each Windows host you want zabbix-rca-AI to
# diagnose. Assumes the agent is already installed and basic hardening
# (Server= / TLS PSK / firewall) is in place — see docs/AGENT-SETUP.md.
#
# Usage:
#   # In an elevated PowerShell:
#   .\install-windows.ps1
#
# Optional flag: -AgentDir 'D:\Zabbix\' to override install dir.

[CmdletBinding()]
param(
    [string]$AgentDir
)

$ErrorActionPreference = 'Stop'

# ── 0. Must be Administrator ───────────────────────────────────────────────
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error 'Must run as Administrator.'
    exit 1
}

# ── 1. Detect agent variant ────────────────────────────────────────────────
function Find-AgentVariant {
    param([string]$Override)
    # Prefer Agent 2 if both services exist.
    foreach ($cand in @(
        @{ Service='Zabbix Agent 2'; Variant='agent2'; ExeHint='zabbix_agent2.exe'; ConfName='zabbix_agent2.conf'; IncDir='zabbix_agent2.d' },
        @{ Service='Zabbix Agent';   Variant='agent1'; ExeHint='zabbix_agentd.exe';  ConfName='zabbix_agentd.conf';  IncDir='zabbix_agentd.d'  }
    )) {
        try { $svc = Get-Service -Name $cand.Service -ErrorAction Stop }
        catch { continue }

        # Resolve install dir from the service's binary path.
        $wmi = Get-WmiObject Win32_Service -Filter "Name='$($cand.Service)'"
        $bin = $wmi.PathName -replace '^"','' -replace '"\s.*$','' -replace '\s--.*$',''
        $dir = if ($Override) { $Override } else { Split-Path -Parent $bin }
        return [pscustomobject]@{
            Variant     = $cand.Variant
            ServiceName = $cand.Service
            AgentDir    = $dir
            MainConf    = Join-Path $dir $cand.ConfName
            IncludeDir  = Join-Path $dir $cand.IncDir
            NeedsEnableRemote = ($cand.Variant -eq 'agent1')
        }
    }
    return $null
}

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$srcConf = Join-Path $here 'zabbix-ai-diag-windows.conf'
if (-not (Test-Path $srcConf)) { Write-Error "missing source config: $srcConf"; exit 1 }

$agent = Find-AgentVariant -Override $AgentDir
if (-not $agent) {
    Write-Error @"
No Zabbix agent service found ("Zabbix Agent 2" or "Zabbix Agent").
Install one from https://www.zabbix.com/download_agents and re-run.
"@
    exit 1
}

Write-Host "[1/4] detected $($agent.Variant)"
Write-Host "      service:    $($agent.ServiceName)"
Write-Host "      install:    $($agent.AgentDir)"
Write-Host "      main conf:  $($agent.MainConf)"

if (-not (Test-Path $agent.MainConf)) {
    Write-Error "main config not found at $($agent.MainConf) — pass -AgentDir to override."
    exit 1
}

# ── 2. Drop AllowKey include file ──────────────────────────────────────────
$dstConf = Join-Path $agent.IncludeDir 'zabbix-ai-diag.conf'
Write-Host "[2/4] installing $dstConf"
if (-not (Test-Path $agent.IncludeDir)) {
    New-Item -ItemType Directory -Path $agent.IncludeDir | Out-Null
}
Copy-Item -Path $srcConf -Destination $dstConf -Force

# Make sure the main config actually reads from the .d directory.
$mainText = Get-Content -Path $agent.MainConf -Raw
$includePattern = '^\s*Include\s*=\s*' + [regex]::Escape($agent.IncludeDir).Replace('\\','\\\\?')
if (-not ($mainText -match $includePattern)) {
    Write-Host "      adding Include= line to $($agent.MainConf)"
    Add-Content -Path $agent.MainConf -Value "`r`n# Added by zabbix-rca-AI installer`r`nInclude=$($agent.IncludeDir)\*.conf"
} else {
    Write-Host '      Include= line already present — skipping'
}

# ── 3. Agent 1 only: enable system.run ─────────────────────────────────────
if ($agent.NeedsEnableRemote) {
    $mainText = Get-Content -Path $agent.MainConf -Raw
    if ($mainText -match '(?m)^\s*EnableRemoteCommands\s*=\s*\d+') {
        if ($mainText -notmatch '(?m)^\s*EnableRemoteCommands\s*=\s*1') {
            (Get-Content -Path $agent.MainConf) `
                -replace '(?m)^\s*EnableRemoteCommands\s*=.*','EnableRemoteCommands=1' `
                | Set-Content -Path $agent.MainConf
            Write-Host "[3/4] flipped EnableRemoteCommands=1 in $($agent.MainConf) (Agent 1 requirement)"
        } else {
            Write-Host '[3/4] EnableRemoteCommands=1 already set'
        }
    } else {
        Add-Content -Path $agent.MainConf -Value "`r`n# Added by zabbix-rca-AI installer — Agent 1 needs this for system.run`r`nEnableRemoteCommands=1"
        Write-Host "[3/4] added EnableRemoteCommands=1 to $($agent.MainConf) (Agent 1 requirement)"
    }
} else {
    Write-Host '[3/4] EnableRemoteCommands not needed (Agent 2 uses AllowKey only)'
}

# ── 4. Restart ─────────────────────────────────────────────────────────────
Write-Host "[4/4] restarting $($agent.ServiceName)"
Restart-Service $agent.ServiceName
Start-Sleep -Seconds 2
$svc = Get-Service $agent.ServiceName
if ($svc.Status -ne 'Running') {
    Write-Error "service did not come back up: $($svc.Status)"
    exit 1
}
Write-Host "      ✓ $($agent.ServiceName) is running"

Write-Host @"

Done ($($agent.Variant)). Verify from the Zabbix server:
  zabbix_get -s <this-host-ip> -k agent.version
  zabbix_get -s <this-host-ip> -k agent.variant     # 1 = Agent 1, 2 = Agent 2

Then trigger a test investigation from the Zabbix frontend
(right-click the host → Investigate with AI).
"@
