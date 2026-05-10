# Changelog

All notable releases. Format follows [keepachangelog.com](https://keepachangelog.com/).

## v1.2.0 — 2026-05-10

- **Connection management UI** at `/admin/connections`. Admin role can
  add/edit Zabbix instances, HostBill, Slack, Anthropic, Google SSO,
  and Zabbix UI signing key from the browser instead of editing
  `/etc/zabbix-ai/{env,config.yaml}` and restarting.
- **Encrypted secret store** in SQLite (AES-GCM-256). Master key derived
  from `SECRETS_KEY` env var (falls back to `SESSION_SECRET`).
- **"Test connection" buttons** on each form probe the real API and
  return live OK/failure inline.
- **Edits take effect on the next investigation** — no service restart.
  `InvestigationRunner.__aenter__` overlays DB-stored connections onto
  the file-loaded `Settings` before building clients.
- Schema migration 004 adds `secrets_kv` and `connections` tables.

## v1.1.0 — 2026-05-10

- **Google SSO** for the admin UI. Login page shows "Sign in with
  Google" alongside the password+TOTP form. First-time SSO sign-in
  auto-provisions a user; SSO users skip TOTP enrolment (Google's 2FA
  is sufficient). Optional `allowed_email_domain` restricts to a
  specific Workspace tenant.
- **TOTP enrolment QR code rendered server-side** as inline SVG via the
  `qrcode` library. The previous Google Charts API URL was deprecated
  and returned a broken image.
- Schema migration 003: makes `users.password_hash` and
  `users.totp_secret` nullable; adds `oauth_provider` + `oauth_subject`
  with a composite unique constraint.

## v1.0.0 — 2026-05-10

First GA release. The system has been validated end-to-end against
real Zabbix 7.4.9 (`monitoring.leapswitch.com`) on three host kinds —
Linux KVM hypervisor (AcronisStor2), Windows Plesk hosting (Plesk1
India-Pune), and a compromised-mailbox spam case — producing actionable
RCAs in 30s–2 min per investigation at ~$0.02 each.

The deployment at `https://zabbix-ai.lsnw.io/` is the reference instance.

### Features

- **CLI** — `python -m zabbix_ai investigate --instance X --eventid N`
  exercises the full orchestrator from the command line.
- **Slack adapter** — `@zabbix-ai` mention in alert threads runs an
  investigation and posts a Block Kit reply in the same thread.
- **Zabbix UI right-click** — HTML page with live SSE-streamed reasoning
  via a signed URL token; opens from a Zabbix Frontend Script.
- **Admin UI** — read-only dashboard, investigation history, audit log,
  pattern memory, host-fact memory, behind TOTP-protected login.
- **Read-only Claude tool registry**, currently 22 tools:
  - `zabbix.*` — get_problem, get_open_problems, get_host, get_history
  - `diag.*` — 14 OS-aware Linux/Windows diagnostics via Zabbix global
    scripts (`zabbix-AI/Linux`, `zabbix-AI/Windows`), plus the special
    `diag.snapshot` and `diag.mail_queue` tools
  - `lookup.*` — host_by_domain, host_by_ip
  - `memory.*` — find_similar_past_investigations, find_pattern,
    find_resolved_tickets (live HostBill query when configured)
  - `forecast.*` / `anomaly.*` — linear extrapolation + IQR/z-score
- **Persistent memory** — every investigation's summary + extracted
  host facts + pattern signature are written to SQLite, surfacing as
  context for future investigations of similar alerts.
- **Pre-deployment infrastructure** — idempotent `deploy/install.sh`
  for Ubuntu 22/24, systemd unit, nginx reverse proxy, certbot.

### Out of scope for v1.0

- HostBill webhook + draft-not-send customer reply (v1.1)
- Auto-mode on Disaster severity (v1.2 — only after trust established)
- Connection management / user management UI (v0.7.1)

### Tests

169 tests pass. Live end-to-end runs on production Zabbix have been
captured in commit history.

## v0.8.0 — 2026-05-10

- `forecast.linear`, `anomaly.iqr`, `anomaly.zscore` tools — pure-Python
  predictive analytics on Zabbix metric history.

## v0.7.0 — 2026-05-10

- Admin UI MVP: dashboard, investigation history, audit log, pattern
  browser, host-facts browser. TOTP login, signed session cookies.

## v0.5.0 → v0.5.1 — 2026-05-09

- Memory write-back from each investigation (patterns + host_facts)
- Three new `memory.*` tools (find_similar, find_pattern,
  find_resolved_tickets)
- Live HostBill API integration for ticket lookup
- Per-OS diag scripts via Zabbix global scripts under `zabbix-AI/{Linux,
  Windows}` menu paths
- `diag.snapshot` consolidated first-look tool
- `diag.mail_queue` MailEnable + Postfix/Exim queue inspection
- Many real-world fixes during live validation: Zabbix 7.4 API compat
  (problem.get → event.get, selectGroups → selectHostGroups, recent
  param dropped), Anthropic tool-name regex (`.` → `__`),
  PowerShell `-EncodedCommand` for Windows command-line safety,
  cmd.exe 8191-char limit awareness, MailEnable SF vs SMTP queue
  folder structure, RFC822 vs envelope file format

## v0.4.0 — 2026-04-29

- Zabbix UI right-click adapter: HMAC-signed URL tokens, streaming
  orchestrator (`investigate_streaming` async generator), Jinja2
  HTML page with EventSource consumer, ops doc for wiring the
  Frontend Script via a PHP signer.

## v0.3.0 — 2026-04-29

- Slack adapter: HMAC-SHA256 signature verification, mention parser,
  Block Kit renderer, `/slack/events` route, channel allowlist.
- `InvestigationRunner` service refactor that hoists orchestrator
  wiring out of the CLI so adapters can share it.

## v0.2.0 — 2026-04-29

- Foundation: FastAPI skeleton, multi-instance Zabbix client, SQLite
  memory + audit log, tool registry with read-only allowlist
  (`zabbix.*`, `diag.*`, `lookup.*`), Claude orchestrator with
  prompt-caching and hard caps (8 tool calls / 50K input tokens),
  CLI adapter.
