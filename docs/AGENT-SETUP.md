# Zabbix Agent setup for zabbix-rca-AI

How to install and configure the Zabbix agent on a monitored host so the
`rca-ai.diag.*` global scripts shipped by zabbix-rca-AI can actually run.
zabbix-rca-AI registers the scripts on the Zabbix **server** automatically
at startup (`services/script_bootstrap.py`); each **agent** must then
explicitly allow the matching `system.run[...]` keys or the call comes
back as `Unsupported item key`. The setup below is defense-in-depth:
server-side regex validation on every `manualinput` value (see
`zabbix_ai/tools/diag.py`) plus per-command `AllowKey` on the agent.

---

## 1. Required agent version

- **Zabbix Agent 2 ≥ 6.0** — tested against 7.x. Some keys
  (`agent.variant`, structured `system.run` plugin) need Agent 2.
- Zabbix Agent 1 (`zabbix_agentd`) works for the simple diags but does not
  ship the Agent-2 plugin keys and is not recommended.
- Config paths differ:
  - Agent 2 → `/etc/zabbix/zabbix_agent2.conf`
  - Agent 1 → `/etc/zabbix/zabbix_agentd.conf`

All examples below target Agent 2.

---

## 2. Network requirements

- Agent listens on TCP/10050. Only the Zabbix server needs to reach it.
- Pin the source — **do not** use CIDR ranges or `0.0.0.0` in `Server=`.
- Encrypt the link with PSK (or certificate) TLS.

```ini
# /etc/zabbix/zabbix_agent2.conf
Server=43.242.224.94          # single IP; the Zabbix server
ServerActive=43.242.224.94    # only if you also push active checks
ListenPort=10050
Hostname=<as-registered-in-Zabbix>

# TLS — PSK in this example. Generate with:
#   openssl rand -hex 32 > /etc/zabbix/agent.psk
#   chmod 600 /etc/zabbix/agent.psk
#   chown zabbix:zabbix /etc/zabbix/agent.psk
TLSConnect=psk
TLSAccept=psk
TLSPSKIdentity=host-<short-name>
TLSPSKFile=/etc/zabbix/agent.psk
```

The same `TLSPSKIdentity` + key must be registered on the host's Encryption
tab in the Zabbix frontend.

Firewall (ufw example, replace `43.242.224.94` with your Zabbix server):

```bash
sudo ufw allow from 43.242.224.94 to any port 10050 proto tcp
```

---

## 3. Linux agent setup

### Install

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install zabbix-agent2

# RHEL / Rocky / Alma
sudo dnf install zabbix-agent2
```

### AllowKey discipline

The diag scripts are registered as Zabbix global scripts with
`execute_on=0` (agent). On the wire each maps to a `system.run[<body>]`
call where `<body>` is the literal command string from `DIAG_DEFINITIONS`
in `script_bootstrap.py`. `AllowKey` / `DenyKey` are evaluated **top-to-bottom**:
the first match wins, so put `DenyKey=system.run[*]` last.

For the short single-command diags the AllowKey value is the bare command
body. The long multi-line bodies (`diag.snapshot`, `diag.mail_queue`,
`diag.network`, `diag.cert_expiry`, `diag.smart`) are too unwieldy — and
contain newlines / quoting — to embed safely in `AllowKey`. For those,
use the **wrapper-script pattern** documented in §3c.

#### 3a. Minimal AllowKey block (short commands only)

Append to `/etc/zabbix/zabbix_agent2.conf`. Each entry matches one diag
exactly as `script_bootstrap.py` registers it.

```ini
# zabbix-rca-AI diag commands — defense in depth.
# Order matters: AllowKey / DenyKey are evaluated top-to-bottom.

# diag.df
AllowKey=system.run[df -hP]
# diag.free
AllowKey=system.run[free -m]
# diag.uptime
AllowKey=system.run[uptime]
# diag.top
AllowKey=system.run[top -bn1 2>/dev/null | head -30]
# diag.dmesg_tail
AllowKey=system.run[dmesg -T 2>/dev/null | tail -100]
# diag.ss_listen
AllowKey=system.run[ss -tunap 2>/dev/null]
# diag.ps_aux
AllowKey=system.run[ps auxf --sort=-%mem 2>/dev/null | head -40]
# diag.iostat
AllowKey=system.run[iostat -xz 1 2 2>/dev/null]
# diag.mysql_status
AllowKey=system.run[mysqladmin --defaults-file=/etc/zabbix/.my.cnf status 2>/dev/null]
# diag.mysql_processlist
AllowKey=system.run[mysql --defaults-file=/etc/zabbix/.my.cnf -e 'SHOW FULL PROCESSLIST' 2>/dev/null]
# diag.apache_status
AllowKey=system.run[curl -s http://127.0.0.1/server-status?auto 2>/dev/null]

# Parameterised — {MANUALINPUT} is substituted by the server before the
# call leaves Zabbix, so the agent sees a concrete unit / line count.
# A wildcard AllowKey is the practical pattern for these two:
# diag.systemctl_status (server-side regex limits unit name)
AllowKey=system.run[systemctl status *]
# diag.journal_tail (server-side regex limits N to 1..9999)
AllowKey=system.run[journalctl -n *]

# Everything else is denied.
DenyKey=system.run[*]
```

#### 3b. Confirming a script body

If you need to verify what string the agent will receive for a given
diag, dump it from a Python REPL on the AI host:

```bash
sudo -u zabbix-ai /opt/zabbix-ai/.venv/bin/python -c \
  "from zabbix_ai.services.script_bootstrap import DIAG_DEFINITIONS; \
   import json; \
   print(json.dumps({d.name: d.linux for d in DIAG_DEFINITIONS}, indent=2))"
```

The value printed for each diag is exactly the body the agent receives
inside `system.run[...]`.

#### 3c. Wrapper-script pattern (recommended for the long diags)

For `diag.snapshot`, `diag.mail_queue`, `diag.network`, `diag.cert_expiry`
and `diag.smart` the command body spans many shell lines and (in the
case of `diag.cert_expiry`) contains a `{MANUALINPUT}` placeholder. The
cleanest agent-side hardening for these is to drop a small wrapper
script per diag and only allow the wrapper.

> **Note.** Out of the box zabbix-rca-AI registers the **inline** bodies
> on the server. To use wrapper scripts end-to-end you must either
> (a) override the `DIAG_DEFINITIONS` body so the server's global script
> calls your wrapper, or (b) keep the inline bodies on the server and
> set permissive AllowKey for these few diags (see §3a). Document
> whichever approach your site uses.

Example wrapper layout:

```bash
sudo install -d -o root -g zabbix -m 0750 /etc/zabbix/scripts
sudo tee /etc/zabbix/scripts/diag-snapshot.sh > /dev/null <<'EOF'
#!/bin/sh
set -eu
echo '=== uptime ==='; uptime
echo '=== df -hP ==='; df -hP
echo '=== free -m ==='; free -m
echo '=== top (head 20) ==='; top -bn1 2>/dev/null | head -20
echo '=== ps by mem (head 20) ==='; ps auxf --sort=-%mem 2>/dev/null | head -20
echo '=== ss listen (head 20) ==='; ss -tlnp 2>/dev/null | head -20
echo '=== dmesg tail 30 ==='; dmesg -T 2>/dev/null | tail -30
EOF
sudo chmod 0750 /etc/zabbix/scripts/diag-snapshot.sh
sudo chown root:zabbix /etc/zabbix/scripts/diag-snapshot.sh
```

Then:

```ini
AllowKey=system.run[/etc/zabbix/scripts/diag-snapshot.sh]
```

### Extra packages required by v1.4 diags

| Diag | Package | Install |
|---|---|---|
| `diag.smart` | smartmontools | `apt install smartmontools` / `dnf install smartmontools` |
| `diag.cert_expiry` | openssl | usually preinstalled — verify with `openssl version` |
| `diag.mail_queue` | postfix / exim / mailx | the script auto-detects `postqueue`, `exim` or `mailq` |
| `diag.iostat` | sysstat | `apt install sysstat` / `dnf install sysstat` |
| `diag.mysql_status`, `diag.mysql_processlist` | mysql client + `/etc/zabbix/.my.cnf` | `[client]` block with user/password; `chmod 0640`, `chown zabbix:zabbix` |

### Sudoers for diag.smart

`smartctl` needs raw device access. Allow only the binary, no password:

```
# /etc/sudoers.d/zabbix-smartctl
zabbix ALL=(root) NOPASSWD: /usr/sbin/smartctl
```

```bash
sudo visudo -cf /etc/sudoers.d/zabbix-smartctl   # validate
sudo chmod 0440 /etc/sudoers.d/zabbix-smartctl
```

### Timeout

Some diags can take 20+ seconds (`diag.iostat` samples twice, the mail
queue script scans up to 2000 envelope files). Bump the agent timeout to
match the server-side 30s limit:

```ini
Timeout=30
```

### Restart + verify

```bash
sudo systemctl restart zabbix-agent2
sudo systemctl status zabbix-agent2 --no-pager

# from the Zabbix server:
zabbix_get -s <agent-ip> -k agent.version
zabbix_get -s <agent-ip> -k agent.variant      # 2 == Agent 2
```

---

## 4. Windows agent setup

### Install

Download the MSI from <https://www.zabbix.com/download_agents>, install
Agent 2 to `C:\Program Files\Zabbix Agent 2\`. Config file:
`C:\Program Files\Zabbix Agent 2\zabbix_agent2.conf`.

### The Windows-specific problem

Every Windows diag (except the manualinput ones) is wrapped by
`_ps_encoded()` in `script_bootstrap.py`, producing a line like:

```
powershell -NoProfile -OutputFormat Text -EncodedCommand <600+ char base64 blob>
```

The blob is unique per diag and changes whenever the upstream
PowerShell body changes. Putting a literal `AllowKey=system.run[...]`
line per diag is impractical and brittle.

Two workable patterns:

#### 4a. Wrapper-script pattern (recommended)

Drop one `.ps1` per diag under `C:\Program Files\Zabbix Agent 2\scripts\`,
override the corresponding entry in `DIAG_DEFINITIONS` (or carry a local
patch) so the server's global-script body invokes the wrapper, and
allow only those wrappers:

```ini
# zabbix_agent2.conf
AllowKey=system.run[powershell -NoProfile -File "C:\Program Files\Zabbix Agent 2\scripts\diag-snapshot.ps1"]
AllowKey=system.run[powershell -NoProfile -File "C:\Program Files\Zabbix Agent 2\scripts\diag-network.ps1"]
AllowKey=system.run[powershell -NoProfile -File "C:\Program Files\Zabbix Agent 2\scripts\diag-smart.ps1"]
# ... one line per diag ...
DenyKey=system.run[*]
```

The `.ps1` content is the matching `_WINDOWS_*_PS` string from
`script_bootstrap.py`. This keeps the agent strictly allowlisted at the
cost of one extra deployment step per host.

#### 4b. Permissive AllowKey, PSK-locked (simpler)

If the agent is reachable **only** from the Zabbix server IP and the
link is PSK-authenticated, allowing all `powershell` invocations is an
acceptable trade-off. zabbix-rca-AI still pins what gets run on the
server side (only the registered global scripts execute, and only via
`script.execute` from the orchestrator):

```ini
# zabbix_agent2.conf — requires §2 PSK + firewall in place.
AllowKey=system.run[powershell*]
DenyKey=system.run[*]
```

Use 4a for security-sensitive estates (shared hosts, multi-tenant). Use
4b for self-hosted homogeneous fleets where the network and PSK keys
are the primary boundary.

### Service user

By default the agent runs as `LocalSystem`, which is fine for
`diag.smart` (uses built-in `Get-PhysicalDisk` /
`Get-StorageReliabilityCounter` cmdlets) and `diag.network`
(`Get-NetIPAddress`, `Resolve-DnsName`, etc.).

`diag.cert_expiry` opens outbound TCP from the agent host to each
endpoint being probed — make sure Windows Firewall and any egress ACL
allows it.

### Restart + verify

```powershell
Restart-Service "Zabbix Agent 2"
Get-Service "Zabbix Agent 2"

# from the Zabbix server:
# zabbix_get -s <agent-ip> -k agent.version
# zabbix_get -s <agent-ip> -k agent.variant     # 2 == Agent 2
```

---

## 5. Server-side: registering global scripts

You normally do **not** need to register scripts manually. On startup,
zabbix-rca-AI calls `ensure_diag_scripts()`
(`zabbix_ai/services/script_bootstrap.py`) and creates / updates the
`zabbix-AI/Linux/diag.*` and `zabbix-AI/Windows/diag.*` global scripts
in every configured Zabbix instance. Provide Zabbix admin credentials
with `script.create` / `script.update` permission and the bootstrap is
idempotent across restarts. If you don't, expect a startup log line
saying script bootstrap is skipped.

---

## 6. Verifying it works

`system.run[...]` is not normally reachable via `zabbix_get` (the call
returns the result of the literal key as configured on the agent, but
the server doesn't actually probe it from `zabbix_get` for global
scripts). The authoritative end-to-end check is to invoke the global
script via the Zabbix frontend:

1. Open the Zabbix UI → Configuration → Hosts → pick the host.
2. From the row menu, **Scripts → zabbix-AI → Linux → diag.uptime**
   (or Windows / diag.df / etc.).
3. The script should return output within ~2 seconds. If you see
   `Unsupported item key` you're missing an `AllowKey`. Add it, restart
   the agent, retry.

Quick smoke list (run each from the Zabbix frontend):

- `diag.uptime` — sanity check, returns one line.
- `diag.df` — confirms simple AllowKey.
- `diag.snapshot` — confirms the wrapper / long-body path works end-to-end.
- `diag.smart` — confirms smartctl + sudoers.
- `diag.cert_expiry` with `manualinput=zabbix-ai.lsnw.io:443` — confirms
  the `manualinput` substitution path.

From the CLI, a basic agent liveness check still works:

```bash
zabbix_get -s <agent-ip> -k agent.ping       # → 1
zabbix_get -s <agent-ip> -k agent.version    # → 7.x.x
```

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ZBX_NOTSUPPORTED: Unsupported item key.` | No matching `AllowKey` or it's after a broader `DenyKey` | Add the exact body in `AllowKey`, ensure `DenyKey=system.run[*]` is last, restart agent |
| `Cannot open file: [2] No such file or directory` | Wrapper-script path wrong (Windows) | Verify path; confirm Agent 2 is the running variant via `zabbix_get -k agent.variant` |
| `script timed out` after 30s | `Timeout=` too low or the diag really is slow | Raise to `Timeout=30` in `zabbix_agent2.conf` and restart; for genuinely-slow hosts raise both this and the Zabbix server-side script timeout |
| `smartctl: command not found` | smartmontools not installed | `apt install smartmontools` / `dnf install smartmontools` |
| `Permission denied` running smartctl | Missing sudoers entry | Add `/etc/sudoers.d/zabbix-smartctl` from §3, validate with `visudo -cf` |
| `openssl: command not found` | openssl missing on Linux | `apt install openssl` |
| `diag.mail_queue` says "No supported MTA found" | Postfix/Exim/mailq not present | Install one, or remove `diag.mail_queue` from the host's expected toolset |
| Agent silently doesn't respond | PSK mismatch | Server host's Encryption tab must match `TLSPSKIdentity` and the key in `TLSPSKFile`; check `/var/log/zabbix/zabbix_agent2.log` |
| `Get-PhysicalDisk` empty on Windows | Running under a service account without storage rights | Leave service as `LocalSystem` |

Agent log for any failed key — Linux:

```bash
sudo tail -f /var/log/zabbix/zabbix_agent2.log
```

Windows:

```
C:\ProgramData\zabbix\zabbix_agent2.log
```

---

## 8. Hardening checklist

- [ ] `Server=` lists a single IP, not a CIDR or wildcard.
- [ ] `TLSConnect=psk` and `TLSAccept=psk` (or `cert`); `TLSPSKFile` is
      `0600`, owned by `zabbix:zabbix`.
- [ ] Each diag has an explicit `AllowKey` (or a wrapper-script
      `AllowKey`); `DenyKey=system.run[*]` is the **last** line.
- [ ] Host firewall allows TCP/10050 inbound only from the Zabbix
      server IP.
- [ ] Agent runs as the `zabbix` user (Linux) — not root. Verify with
      `ps -ef | grep zabbix_agent2`.
- [ ] `127.0.0.1` is **not** in `Server=` unless you specifically need
      local probing.
- [ ] `/etc/sudoers.d/zabbix-smartctl` is `0440` and `visudo -cf`-clean.
- [ ] `/etc/zabbix/.my.cnf` (if used) is `0640`, owned by
      `zabbix:zabbix`, contains no shell-injectable fields.
- [ ] Wrapper scripts under `/etc/zabbix/scripts/` are `0750`,
      `root:zabbix`, and not writable by the `zabbix` user.
