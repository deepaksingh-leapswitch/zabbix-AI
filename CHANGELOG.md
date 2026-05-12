# Changelog

All notable releases. Format follows [keepachangelog.com](https://keepachangelog.com/).

## v1.5.3 — 2026-05-12

**Closing the gap surfaced by inv 14/15 on monitoring.leapswitch.com.**
The AI saw the symptoms (MariaDB CPU, disk 96%, housekeeper backlog)
but couldn't see the root *config*: `innodb_buffer_pool_size=6G`,
`tmp_table_size=16M`, `ibdata1=91GB`, uncompressed log files. Five new
tools and a system-prompt directive that together let the AI deliver
the same "raise buffer pool to 10G, raise tmp_table_size to 256M,
compress logs" answer I had to type by hand.

### New diag tools (Linux only)

| Tool | What it surfaces |
|---|---|
| `diag.mysql_config` | ~30 MariaDB tuning variables (`innodb_buffer_pool_*`, `tmp_table_size`, `max_heap_table_size`, log/binlog, query cache, etc.) |
| `diag.mysql_tables` | Top 25 tables by `DATA_LENGTH` + `INDEX_LENGTH` plus `ls -lh /var/lib/mysql/ib*` to expose `ibdata1` bloat |
| `diag.mysql_stats` | Computed hit ratios — buffer pool hit %, in-memory tmp-table %, dirty-page %, slow-queries/hour |
| `diag.disk_largest_files` | Top 30 individual files by size (`find / -xdev -size +500M`) — catches the single-file culprits `diag.disk_usage` hides at the folder level |
| `diag.read_config` | Read any file under an `/etc/` allowlist (zabbix, my.cnf*, logrotate.d, nginx, apache2, httpd, mysql, mariadb, systemd, sysctl.d, fail2ban, postfix, exim, dovecot). Server-side regex + Zabbix manualinput validator both block traversal. |

### System prompt directives

- **Capacity-vs-config classifier.** Before finalising `suggested_actions`,
  the AI must classify the root cause as `config` / `capacity` /
  `config-then-capacity` and ensure capacity issues land a dedicated
  "Scale-out" section (proxy / vertical scale / scope reduction).
- **MariaDB tool-chaining hint.** When DB symptoms are present, the AI
  is instructed to call `diag.mysql_config`, `diag.mysql_stats`, and
  `diag.mysql_tables` together, not just `diag.mysql_status`.
- **Disk-fill follow-up.** `diag.disk_usage` showing a near-full folder
  triggers a `diag.disk_largest_files` follow-up. Config suspicion
  triggers `diag.read_config` on the relevant file.

### AllowKey config

`deploy/zabbix-agent/zabbix-ai-diag.conf` gains literal `system.run[…]`
lines for all four new non-parameterised bodies (byte-for-byte match
with the script bodies registered server-side) plus
`AllowKey=system.run[head -200 *]` for `diag.read_config` (path
constrained by the server-side regex; `head` is read-only).

### Tests

389 tests pass. New: 12 unit tests for the v1.5.3 tools, including
`diag.read_config` allowlist (6 accepted paths + 6 rejected including
`/etc/passwd`, `/etc/shadow`, `/root/.ssh/id_rsa`, `..` traversal).

## v1.5.2 — 2026-05-12

**Host-mode write-back.** Inv #15 was a right-click from the host
page (not a problem) so `{EVENT.ID}` didn't resolve; v1.5.1's
write-back required a specific eventid and silently no-op'd. Added
`post_summary_to_host_open_problems()` — when no eventid is supplied
but a hostid is, resolves the host's top-3-by-severity open problems
via `problem.get` and writes the same comment against each. Both
adapters (manual right-click and auto-investigate webhook) prefer
eventid when present, fall back to host-mode otherwise.

## v1.5.1 — 2026-05-12

Two fixes driven by inv #14 (Zabbix server itself):

1. **Recurring problems get top-billing in the briefing + report.** The
   30-day past-events list was already in the briefing but only as a
   rolled-up count buried in evidence. Now any trigger that fired 3+
   times in 30 days lands in a dedicated "🔁 Recurring problems"
   section with the latest 3 occurrence timestamps. The system prompt
   explicitly requires the AI to surface these as a "Recent incidents"
   section at the *top* of its report. The on-call engineer no longer
   has to dig.

2. **Manual right-click investigations now write back to the Zabbix
   problem.** Auto-investigate (v1.5.0) already wrote summaries back
   as `event.acknowledge` comments; the manual right-click flow
   didn't, leaving operators with no trail in the Zabbix UI. Extracted
   the write-back into `services/zabbix_writeback.py` and wired it
   into both paths. Source tag distinguishes `[zabbix-rca-AI manual]`
   vs `[zabbix-rca-AI auto-investigation]` in the comment prefix.

## v1.5.0 — 2026-05-11

**Trust-loop release — cuts L1/L2 time on routine alerts.**

The headline change: every Zabbix alert can now be **auto-investigated**
without a human click, and the resolution narrative that L1/L2 type
when they close the problem feeds forward so the *next* time the same
alert fires, the AI starts with *"last time, deepak fixed it with
DISM cleanup."*

### Auto-investigate-on-alert

- New endpoint `POST /zabbix/auto-investigate` (HMAC-signed body,
  replay-protected via timestamp window).
- Zabbix action helper: `POST /admin/connections/system/register-
  zabbix-action` creates the matching Zabbix Action so the wire is
  there with one click.
- Per-hostgroup allowlist + severity gate + budget gate before the
  investigation kicks off.
- After completion, summary is written back to the Zabbix problem as
  an `event.acknowledge` comment AND posted to a configured Slack
  channel.
- Rate-limited at 60 invocations/minute.

### Resolution-notes feed-forward

- `migrations/007_v1_5_schema.sql` adds `resolution_notes`,
  `resolution_at`, `resolution_by`, `resolution_source` columns on
  `investigations`.
- Background poller (2 min interval) mines Zabbix `event.acknowledge`
  messages on closed problems and writes them into the matching
  investigation row, source=`zabbix_ack`. Resolves usernames via
  `user.get` and ack actions via the action bitmask (1=close,
  4=message).
- Operator can also type/edit notes manually via the new
  `POST /admin/investigations/{id}/resolution` route (operator+ role,
  audit-logged).
- `memory.find_similar_past_investigations` returns the notes;
  system prompt requires the AI to LEAD its report with
  *"Last time this fired (date, by user): <notes>"* when present.

### Daily Anthropic budget cap

- `services/budget.py` — daily ₹ cap with three over-budget actions:
  `haiku_only` (downgrade), `pause` (refuse new investigations),
  `warn` (log + proceed). Reset hour configurable.
- Enforced inside the orchestrator before each Claude call.
- New `budget_audit` table records every gated decision.
- Cost dashboard headline: `Today: ₹X / ₹Y · Z% remaining · status:
  ok | haiku-fallback | paused`.

### Outcome inference

- `services/outcome_inference.py` — 10-min poller compares the host's
  relevant metric (disk %, memory free, CPU util) before vs after
  `resolution_at`. If the metric moves in the recovery direction, a
  JSON blob with the delta is stored on `investigations.outcome_inferred`
  and surfaced on the investigation detail page.
- Conservative threshold (10% absolute delta) to avoid false
  "AI fix worked!" claims.

### HostBill linkage foundation

- New `host_hostbill_link` table caches per-host links
  (Zabbix-instance + hostid → HostBill service/client).
- Auto-matcher: Zabbix tag `hostbill_service_id` → IP match →
  hostname/domain match. First hit wins; confidence labelled
  `high`/`medium`/`low`.
- Daily background sync refreshes the cache.
- Admin UI at `/admin/connections/hostbill/links` shows all linked
  hosts; filter to "Needs attention" surfaces unlinked +
  low-confidence rows for manual override.
- HostBill API client extended with `search_services`, `get_service`,
  `get_client`, `get_tickets`, `is_reachable`. All methods degrade
  gracefully when the HostBill API is unreachable — code lights up
  the moment credentials arrive.

### Tests

366 tests, 0 failures, 0 lint errors. New: webhook signature (13),
auto-investigate route (9), budget (12), outcome inference (13),
resolution notes (16 unit + 4 integration), HostBill linker (7 unit
+ 5 integration).

## v1.4.5 — 2026-05-11

Two fixes for inv #12 on deepak-vm:

- `diag.windows_winsxs` was returning `The command line is too long`.
  The Start-Job machinery + recovery-commands strings pushed the
  `-EncodedCommand` base64 to **9821 chars**, well past cmd.exe's
  8191-char limit. Stripped the body to **3801 chars** (61% smaller):
  removed the recovery-commands `Write-Output` block, the Win32_Page
  FileUsage query, and the Microsoft.Update.Session COM query.
  All deleted content is now in the tool **description** — the AI
  reads it once when the tool list loads, no per-call cost. Same
  measurement coverage (WinSxS, SoftwareDistribution, Installer,
  pagefile, hiberfil, etc.).
- The cleanup commands the AI should suggest (`cleanmgr /sagerun:1`,
  `DISM /Online /Cleanup-Image /StartComponentCleanup`,
  `powercfg /h off`, SoftwareDistribution\Download purge) are
  embedded verbatim in the diag.windows_winsxs description so model
  recommendations stay specific without script-output round-trips.

## v1.4.4 — 2026-05-11

Fixes the regression v1.4.3 introduced: the robocopy fallback inside
`diag.disk_usage` could itself take 30+ seconds on `C:\Windows`,
pushing the script past Zabbix 7.4's 30 s hard-cap for global-script
timeouts. Inv #11 on deepak-vm hit it (`diag.disk_usage` and
`diag.windows_winsxs` both timed out) even though the host was idle.

Changes:

- **`diag.disk_usage` (Windows): drop robocopy fallback entirely.**
  Stays FSO-only, ~10 s budget. Folders FSO can't measure get
  `n/a (call diag.windows_winsxs)` in the Notes column. Faster than
  v1.4.3 and matches Zabbix's 30 s limit comfortably.
- **`diag.windows_winsxs`: per-target `Start-Job` + `Wait-Job -Timeout 6`.**
  Each of the 11 paths gets its own 6-second budget. A slow WinSxS
  shows `(timed out after 6s)` for just that row while the other ten
  paths still report correctly.
- Removed `$env:SystemRoot` itself from the target list (was redundant
  with the specific subpaths and too broad to ever finish in budget).

The bootstrap's `script.update` overwrites the v1.4.3 bodies on the
next investigation.

## v1.4.3 — 2026-05-11

**Two improvements driven by inv #9 on deepak-vm.**

1. **`diag.disk_usage` (Windows) — robocopy fallback + unaccounted-bytes
   summary.** FSO silently returns 0 GB on folders with access-denied
   subdirs (e.g. `C:\Windows`, `C:\Users`), which made inv #9 conflate
   "couldn't measure" with "empty." Now:
   - Each row carries a `Notes` column showing how the size was
     obtained (`fso`, `robocopy fallback`, `n/a (access denied)`).
   - When FSO returns 0 we try `robocopy /L /E /BYTES` as a fallback
     (raw-byte total, gracefully tolerates per-file access denials).
   - A trailing summary line per drive reports `total used / measured
     / unaccounted GB`; if the unaccounted delta exceeds 5 GB, the
     output explicitly tells the AI to follow up with
     `diag.windows_winsxs`.

2. **New tool `diag.windows_winsxs`.** Windows-only follow-up that
   measures the usual suspects under `C:\Windows` (WinSxS,
   SoftwareDistribution\Download, Installer, System32\config,
   event-log dir, Panther) plus `hiberfil.sys` / `pagefile.sys` /
   `swapfile.sys`. Each uses FSO with robocopy fallback. Also reports
   the pending Windows update count and prints (does **not** execute)
   the cleanmgr / DISM `/StartComponentCleanup` / `powercfg /h off` /
   `SoftwareDistribution` purge commands an operator would run.

Tool descriptions instruct the model to call `diag.windows_winsxs`
automatically when `diag.disk_usage` reports a large unaccounted
delta — so the next investigation on deepak-vm will chain both calls
and produce a complete picture of `C:\Windows` usage.

## v1.4.2 — 2026-05-11

**Fix `diag.disk_usage` timeout on Windows.** The v1.4.1 implementation
used `Get-ChildItem -Recurse -Depth 3`, which on a 200 GB NTFS volume
took several minutes and tripped the 30s script timeout. Investigation
#8 on deepak-vm came back "Timeout while executing" instead of folder
sizes.

Replaced with `Scripting.FileSystemObject.GetFolder().Size` (native COM,
reads NTFS metadata) on top-level folders only. Wrapped in a 22-second
per-drive stopwatch budget — if the budget runs out it emits what's
been measured plus "N folders skipped" rather than failing outright.
Typical scan time is now ~5-15 s per fixed drive.

Linux variant unchanged.

The bootstrap's `script.update` path will overwrite the v1.4.1 script
body on the next investigation, so no manual cleanup is needed.

## v1.4.1 — 2026-05-11

**New diag tool — `diag.disk_usage`.** Closes the gap observed on
investigation #7 (deepak-vm, C: 92.6% full) where the AI had a process
list but no folder-level view, so it recommended RDP instead of being
concrete. With this tool the AI can now say "`C:\Users\…\AppData\Local\…`
is 47 GB" directly from the investigation.

- **Linux**: `df -hP` + `du -hxd 3 / | sort -hr | head -40` wrapped in
  `timeout 30`. Stays on one filesystem.
- **Windows**: per-fixed-drive used/free/percent, plus top 15 folders
  by recursive size on each (depth-3 cap to bound walk time on big
  NTFS volumes).
- Tool description tells the model: **ALWAYS** call this on a
  disk-space alert before suggesting RDP/SSH.
- `deploy/zabbix-agent/zabbix-ai-diag.conf` gains a matching AllowKey
  line for the Linux body. Windows hosts using
  `system.run[powershell*]` need no change.
- New tests: `test_v14_tools_in_allowlist` extended; new
  `test_diag_disk_usage_dispatches` and
  `test_diag_disk_usage_defined_for_both_os`.

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
