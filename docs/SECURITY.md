# Security notes — zabbix-rca-AI

## LLM prompt-injection surface (#17)

### What is protected

- **Read-only tool registry** — every tool the AI can call is in a hard-coded
  allowlist (`ALLOWED_DIAG_KEYS`). There is no "run arbitrary command" tool.
- **Parameterised SQL** — all DB queries use `?` placeholders; no string
  interpolation occurs in any query path.
- **No shell exec from Python** — the diag tools call the Zabbix API
  (`script.execute`) which runs the fixed Zabbix global-script commands on the
  agent. The Python process itself never calls `subprocess`.
- **Audit trail** — every tool call (name + sanitised input + output) is
  written to `audit_log`. A full investigation transcript is therefore
  recoverable for forensic review.

### What is NOT protected

The orchestrator builds the user prompt by concatenating untrusted strings
from Zabbix: `problem.name`, `hostname`, host tags, briefing data, etc.
A maliciously crafted hostname or problem description (e.g. set inside
Zabbix by an attacker) **could trick the model** into:

- Producing a misleading or incorrect root-cause summary.
- Mis-routing a diagnostic call to a wrong host (within the set of hosts
  the bot token can see).

Neither of these escapes the read-only tool boundary. The worst outcome
is a bad summary or a wasted `$0.04` API call.

### Risk escalation condition

If write-capable tools are ever added (e.g. "close HostBill ticket",
"restart service", "silence Zabbix alert"), the injection surface becomes
a **High** finding and must be addressed before those tools ship:
- Input sanitisation / reject-list on problem names / hostnames fed into
  the prompt.
- Operator confirmation step before any write action.
- Output parser validation (ensure the model output is a valid action, not
  a free-form instruction string).

### Recommendation

Operators should periodically review the `audit_log` for unexpected tool
calls, especially if the bot interacts with Zabbix instances where
monitoring data is customer-supplied (e.g. cloud VPS platforms).

---

## Token log exposure (#7 / #20)

Investigation tokens travel in query strings (`/investigate?token=…`) and
therefore appear in nginx `access_log` by default.  See `docs/RUNBOOK.md`
for the nginx log-format recommendation to redact query strings from the
`/investigate*` path.

---

## Secrets on disk (#20)

`.env.local` in the working tree (gitignored) may contain real production
secrets.  Anyone with a shell as `leap` can read them.  After this security
review, rotate:
- `ANTHROPIC_API_KEY`
- `ZABBIX_TOKEN_MONITORING`

Use `anthropic.com/account` and the Zabbix admin UI respectively.

---

## Bootstrap admin password (#21)

`BOOTSTRAP_ADMIN_PASSWORD` in `/etc/zabbix-ai/env` is only used on first
startup to create the initial `admin` user.  Once that user exists the
variable is never read again — but it remains on disk.  Remove it:

```bash
sudo sed -i '/^BOOTSTRAP_ADMIN_PASSWORD=/d' /etc/zabbix-ai/env
sudo systemctl restart zabbix-ai
```

The service logs a warning at startup if the variable is still set after
the first admin user is created.

---

## SSRF on admin connection URL fields (#19)

Admin-supplied Zabbix / HostBill URLs are validated against a private-CIDR
deny-list on save.  Blocked ranges:
- `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- `169.254.0.0/16` (link-local / AWS metadata)
- `::1`, `fc00::/7` (IPv6 loopback / ULA)

Only `https://` URLs are accepted (or `http://` with an explicit admin
override — currently blocked entirely to encourage TLS).
