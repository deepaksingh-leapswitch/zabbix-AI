# zabbix-rca-AI — Agent enablement

Drop-in config + one-line installers to enable zabbix-rca-AI diag scripts
on a Zabbix agent. See `docs/AGENT-SETUP.md` for full background, threat
model, and the wrapper-script alternative.

## Linux (zabbix-agent2)

```bash
sudo ./install-linux.sh
```

Installs the AllowKey block, `smartmontools`, `sysstat`, the smartctl
sudoers entry, and restarts the agent.

## Windows (Zabbix Agent 2)

In an **elevated** PowerShell:

```powershell
.\install-windows.ps1
```

If the agent isn't at `C:\Program Files\Zabbix Agent 2\`, pass the
location: `.\install-windows.ps1 -AgentDir 'D:\Zabbix\'`.

## Files

| File | Purpose |
|---|---|
| `zabbix-ai-diag.conf` | Linux AllowKey block — drops into `/etc/zabbix/zabbix_agent2.d/` |
| `zabbix-ai-diag-windows.conf` | Windows AllowKey block — drops into `C:\Program Files\Zabbix Agent 2\zabbix_agent2.d\` |
| `install-linux.sh` | Idempotent Linux installer (run as root) |
| `install-windows.ps1` | Idempotent Windows installer (run as Administrator) |

## Prerequisites

Both installers assume basic agent hardening is already in place:

- `Server=<single Zabbix server IP>` — no CIDR, no hostname
- TLS PSK enabled (`TLSConnect=psk` + `TLSAccept=psk`)
- Host firewall: TCP/10050 inbound only from the Zabbix server

If those aren't set yet, do that first — see `docs/AGENT-SETUP.md` §2.
The AllowKey-only model relies on the network being a trusted boundary.
