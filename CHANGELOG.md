# Changelog

All notable releases. Format follows [keepachangelog.com](https://keepachangelog.com/).

## v1.4.0 — 2026-05-11

**Admin tooling + 3 new diag tools.**

### Admin panel — three new pages

- **`/admin/users`** — user management (admin role only): create, change role,
  set password, reset TOTP, lock/unlock, delete. Server-side safety gates
  prevent self-lockout and last-admin demotion. Every change writes to
  `admin_audit_log`.
- **`/admin/cost`** — Anthropic spend dashboard. Today/month totals in ₹,
  30-day daily bar chart, model split (Sonnet vs Haiku), top-10 most
  expensive investigations / hosts / sources. Pricing in
  `services/pricing.py`; CSV export at `/admin/cost/export.csv`.
- **`/admin/status`** — system health page. App version + uptime, DB stats,
  per-Zabbix-instance health, Slack/Anthropic last-success timestamps,
  secret counts, running investigation count, memory-table row counts,
  background-task liveness. JSON variant at `/admin/status.json`.

### Diagnostic tools

- **`diag.network`** — interfaces, routes, DNS config + resolve test,
  default-gateway reachability. Linux + Windows.
- **`diag.cert_expiry`** — TLS cert dates for one or more `host:port`
  endpoints (comma-separated, max 10). Strict server-side regex validator.
  Linux uses `openssl s_client`; Windows uses `Net.Sockets.TcpClient` +
  `SslStream`.
- **`diag.smart`** — SMART health for all physical disks. Linux requires
  `smartmontools` + a sudoers NOPASSWD entry for `smartctl`; Windows uses
  `Get-PhysicalDisk` and `Get-StorageReliabilityCounter`.

### Infrastructure

- **`migrations/006_connection_health.sql`** — `connection_health(kind,
  name, last_success_at, last_error_at, last_error)` table. Updated by
  Zabbix / Anthropic / Slack clients via `services/connection_health.py`
  (UPSERT, best-effort, never breaks the calling API call).
- **`docs/AGENT-SETUP.md`** — full agent-side install + hardening guide:
  AllowKey patterns (Linux literal-body + wildcards for manualinput diags,
  Windows wrapper-script pattern), TLS PSK, smartmontools sudoers, troubleshooting.

### Tests

- 280+ tests, 0 failures, 0 lint errors.
- New: `test_admin_users_mgmt` (12), `test_admin_cost` (6),
  `test_admin_status` (6), `test_connection_health` (7),
  `test_pricing` (8), `test_tools_diag` +9, `test_script_bootstrap` +5.

## v1.3.1 — 2026-05-10

**Security hardening release.** Addresses all 21 findings from the
2026-05-10 security review (3 High, 6 Medium, 7 Low, 5 Informational).
See `docs/SECURITY-FIXES-2026-05-10.md` for the full status table.

### Highlights

- **CSRF protection** (`admin/csrf.py`) — double-submit cookie middleware on
  all admin POST routes. Forms render `{{ csrf_token }}`; logout is now POST.
- **Single-use URL tokens** — investigation links signed with a `jti`,
  consumed via the new `used_tokens` table (migration 005). Replaying a
  link returns 401.
- **Rate limiting** (`admin/rate_limit.py`) — slowapi `Limiter` on
  `/admin/login`, `/admin/zabbix-link`, OAuth callback. `_real_ip` honours
  `X-Forwarded-For` only from localhost.
- **Operator role on `/admin/zabbix-link`** — viewers can no longer mint
  investigation tokens (cost-amplification fix).
- **Security headers middleware** — HSTS, CSP, X-Frame-Options,
  X-Content-Type-Options, Referrer-Policy on every admin response.
- **Admin audit log** (`admin_audit_log` table) — login/logout, conn
  upserts, secret rotations, token issuance.
- **TOTP replay-cache** — last code+timestamp stored per user; same code
  in the same 30-second window is rejected.
- **SSRF deny-list** on `/admin/connections/*` URL fields — rejects
  loopback, RFC1918, link-local, and IPv6 ULA/link-local destinations.
- **Self-hosted htmx** under `/static/htmx.min.js` (replaces unpkg CDN
  dependency).
- **Tightened `diag.systemctl_status` regex** — no whitespace allowed.
- **Bootstrap-password retention warning** — startup log + 5-minute
  background reminder if `BOOTSTRAP_ADMIN_PASSWORD` env var is still set
  after admin users exist.

### Migration

- `migrations/005_security_hardening.sql` — adds `used_tokens`,
  `admin_audit_log`, and `users.last_totp_code` / `users.last_totp_at`
  columns. Idempotent on first run, gated by `schema_version`.

### Tests

- 234 tests, 0 failures, 0 lint errors.
- New `tests/conftest.py` resets the slowapi limiter between tests.

## v1.3.0 — 2026-05-10

**Host briefing pre-fetch** — structured Markdown block injected into the first
user message when a `hostid` is known, eliminating redundant discovery tool calls.

### What ships

- **`zabbix_ai/services/host_briefing.py`** — new `build_host_briefing` async
  function. Parallel Zabbix API fetches (host, open problems, 30-day event
  history, per-metric item lookup + history). Renders up to 7 sections:
  host header, open problems, 30-day problem history (deduped against open
  problems), metric trends table (CPU/mem/disk/load), 90-day forecast hits,
  past investigations from memory, and matching pattern signatures.
- **Token-efficient metric detection** — tries Linux or Windows key candidates
  in order; inverts "free %" keys (`pavailable`, `used,pfree`) so every column
  reads "% used". Warns with ⚠ if metric ≥ 85% or growing > 1%/day.
- **Soft token cap** — drops sections in priority order (forecast → past
  investigations → patterns → history) until the briefing fits within
  `host_briefing_max_tokens` (default 2000).
- **`HostBriefingSettings`** Pydantic model added to `config.py`; plumbed
  through `Settings.host_briefing`.
- **Admin UI** — three new fields on the Models & limits form: enabled
  checkbox, history days, and max tokens. Saved to `system/defaults` DB
  row; `config_overlay.py` applies them on each investigation.
- **Orchestrator** — accepts optional `host_briefing_config` dict; calls
  `build_host_briefing` after existing enrichment; stores result in
  `ctx.briefing_md`; `_render_user_prompt` prepends it.
- **`InvestigationContext`** gains `briefing_md: str = ""` field.
- **System prompt** updated to instruct the model to use the briefing first
  and skip redundant `zabbix.get_host` / `zabbix.get_open_problems` calls;
  also tightened the "be terse" instruction.
- **Tests** — `tests/unit/test_host_briefing.py` (7 cases) and three new
  overlay tests in `test_config_overlay.py`.

### Estimated token saving

Typical host with 3 open problems, 47 events in 30 days, 3 matching metrics:
- Briefing ≈ 1 400 tokens (pre-fetched once)
- Equivalent tool calls avoided: `get_host` (~800), `get_open_problems` (~600),
  `event.get` 30d scan (~3 000), `item.get × 4` (~800), `history.get × 4` (~1 600)
- **Net saving ≈ 6 300 input tokens** on the first turn; cache hit on system
  blocks saves a further ~1 200 tokens on re-investigations of the same host
  within the 5-minute cache TTL.

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
