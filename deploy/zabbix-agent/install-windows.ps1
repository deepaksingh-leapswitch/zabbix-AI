# zabbix-rca-AI — Windows agent enablement script.
# Idempotent: re-running is safe.
#
# Run as Administrator on each Windows host you want zabbix-rca-AI to
# diagnose. Assumes Zabbix Agent 2 is already installed and the basic
# Server= / TLS PSK / firewall hardening is in place
# (see docs/AGENT-SETUP.md §2 and §9).
#
# Usage:
#   # In an elevated PowerShell:
#   .\install-windows.ps1
#
# What it does:
#   1. Drops zabbix-ai-diag.conf into the agent's zabbix_agent2.d folder.
#   2. Restarts the "Zabbix Agent 2" service.
#   3. Prints a quick verify command.

[CmdletBinding()]
param(
    [string]$AgentDir = 'C:\Program Files\Zabbix Agent 2'
)

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error 'Must run as Administrator.'
    exit 1
}

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$srcConf = Join-Path $here 'zabbix-ai-diag-windows.conf'
$dstDir  = Join-Path $AgentDir 'zabbix_agent2.d'
$dstConf = Join-Path $dstDir  'zabbix-ai-diag.conf'

if (-not (Test-Path $srcConf)) {
    Write-Error "missing source config: $srcConf"
    exit 1
}
if (-not (Test-Path $AgentDir)) {
    Write-Error "Zabbix Agent 2 not found at $AgentDir. Pass -AgentDir <path> if installed elsewhere."
    exit 1
}

Write-Host "[1/3] installing $dstConf"
if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Path $dstDir | Out-Null }
Copy-Item -Path $srcConf -Destination $dstConf -Force

# Confirm main config includes the .d directory. Agent 2 *does* read
# zabbix_agent2.d/*.conf by default in 6.x+, but older installs may need
# 'Include=' added manually.
$mainConf = Join-Path $AgentDir 'zabbix_agent2.conf'
if (Test-Path $mainConf) {
    $hasInclude = Select-String -Path $mainConf -Pattern '^\s*Include\s*=.*zabbix_agent2\.d' -Quiet
    if (-not $hasInclude) {
        Write-Host "[2/3] adding Include= line to $mainConf"
        Add-Content -Path $mainConf -Value "`r`n# Added by zabbix-rca-AI installer`r`nInclude=$dstDir\*.conf"
    } else {
        Write-Host '[2/3] Include= line already present — skipping'
    }
}

Write-Host '[3/3] restarting Zabbix Agent 2'
Restart-Service 'Zabbix Agent 2'
Start-Sleep -Seconds 2
$svc = Get-Service 'Zabbix Agent 2'
if ($svc.Status -ne 'Running') {
    Write-Error "service did not come back up: $($svc.Status)"
    exit 1
}
Write-Host '  ✓ agent is running'

Write-Host @'

Done. Verify from the Zabbix server:
  zabbix_get -s <this-host-ip> -k agent.version
  zabbix_get -s <this-host-ip> -k agent.variant     # 2 = Agent 2

Then trigger a test investigation from the Zabbix frontend
(right-click the host → Investigate with AI).
'@
