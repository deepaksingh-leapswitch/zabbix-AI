# zabbix-rca-AI

AI-assisted root-cause analysis for Leapswitch Zabbix monitoring.

On-demand AI investigation with read-only diagnostic tools, multi-instance
Zabbix support, and Claude as the reasoning brain. v0.2 = CLI only;
Slack / Zabbix-UI / HostBill adapters arrive in subsequent plans.

## Architecture

See `docs/superpowers/specs/2026-04-28-zabbix-rca-ai-design.md`.

## Install (development)

```bash
git clone git@github.com:Leapswitch-Networks/zabbix-rca-AI.git
cd zabbix-rca-AI
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Configure

Copy and edit:

```bash
sudo mkdir -p /etc/zabbix-ai
sudo cp config.example.yaml /etc/zabbix-ai/config.yaml
sudo $EDITOR /etc/zabbix-ai/config.yaml
```

Set env vars (or place in `/etc/zabbix-ai/env`):

- `ANTHROPIC_API_KEY` — Claude API key
- `ZABBIX_TOKEN_<NAME>` — one per Zabbix instance, matching `token_env` in yaml

## Slack adapter (optional)

To enable `@zabbix-ai` in Slack:

1. Create a Slack app at https://api.slack.com/apps with these scopes:
   - `app_mentions:read`, `chat:write`, `chat:write.public`, `channels:history`
2. Install to your workspace, copy the bot token (`xoxb-...`) and signing secret
3. Add to `/etc/zabbix-ai/env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_SIGNING_SECRET=...
   ```
4. Add the `slack:` section to `/etc/zabbix-ai/config.yaml` (see `config.example.yaml`)
5. In Slack app settings → Event Subscriptions, set Request URL to
   `https://zabbix-ai.internal/slack/events` and subscribe to `app_mention`
6. Restart: `systemctl restart zabbix-ai`
7. Invite the bot to your alert channel and mention it: `@zabbix-ai why is web-1 slow?`

## Zabbix UI right-click (optional)

To enable an "Investigate with AI" right-click on Zabbix problems:

1. Generate a signing key (32+ bytes random) and add to `/etc/zabbix-ai/env`:
   ```
   URL_SIGNING_KEY=...
   ```
2. Add the `zabbix_ui:` section to `/etc/zabbix-ai/config.yaml`
   (see `config.example.yaml`).
3. Restart: `systemctl restart zabbix-ai`.
4. Wire the Zabbix Frontend Script per
   `deploy/zabbix-frontend-script.md`.

## HostBill ticket lookup (optional)

Once you have a HostBill admin API user with read access to tickets, the
AI can search past closed tickets for similar customer issues.

1. In HostBill admin: **Settings → API access** → create user with
   permissions: `getTickets`, `getTicketDetails`, `getClientDetails`. Copy
   the API ID and API key.
2. Add to `/etc/zabbix-ai/env`:
   ```
   HOSTBILL_API_ID=...
   HOSTBILL_API_KEY=...
   ```
3. Add the `hostbill:` section to `/etc/zabbix-ai/config.yaml`
   (see `config.example.yaml`).
4. Restart: `systemctl restart zabbix-ai`.

When configured, the AI can call `memory.find_resolved_tickets("disk full")`
during investigation. When not configured, that tool returns
"HostBill not configured" and the investigation proceeds without it.

Local memory (own past investigations + learned host facts + pattern
table) works regardless and is filled automatically every time the AI
runs. No CSV import is needed.

## Deploy agent UserParameters

On every host you want diagnosable, copy `deploy/zabbix-agent/diag.conf`
to `/etc/zabbix/zabbix_agentd.d/diag.conf` and restart the agent. This
defines the read-only `diag.*` allowlist that the AI can call.

## Admin UI (optional, v0.7+)

Browser-accessible read-only views: dashboard, investigation history,
audit log, learned patterns, host facts.

1. Generate a session secret: `openssl rand -hex 32`
2. Choose a temporary bootstrap admin password (used only on first start
   to create the `admin` user; once that user enrols TOTP, this env var
   is ignored — clear it for security).
3. Add to `/etc/zabbix-ai/env`:
   ```
   SESSION_SECRET=<32 hex bytes>
   BOOTSTRAP_ADMIN_PASSWORD=<temporary>
   ```
4. Add the `admin:` block to `/etc/zabbix-ai/config.yaml`.
5. Restart the service. Open `https://your-host/admin/login`, sign in as
   `admin`, and enrol TOTP (Google Authenticator, 1Password, etc.).
6. **Clear `BOOTSTRAP_ADMIN_PASSWORD` from the env file** after first
   login so it can't be reused.

### Connection management

Once logged in as admin, visit `/admin/connections` to add/edit:
  - Zabbix instances (multiple, one row each)
  - HostBill API credentials
  - Slack bot token + channel allowlist
  - Anthropic API key
  - Google SSO client config
  - Zabbix UI signing key

Secrets are AES-GCM encrypted with a key derived from `SECRETS_KEY`
(or `SESSION_SECRET` if unset). Edits take effect on the NEXT
investigation — no service restart needed.

User management and pattern editing arrive in v0.7.1.

### Google SSO (optional)

Sign in with a Google Workspace account instead of password+TOTP:

1. In Google Cloud Console → APIs & Services → Credentials → **Create
   OAuth 2.0 Client ID** → Web application.
2. Authorised redirect URI:
   `https://zabbix-ai.lsnw.io/admin/oauth/google/callback`
3. Save the client ID and client secret.
4. Add to `/etc/zabbix-ai/env`:
   ```
   GOOGLE_OAUTH_CLIENT_SECRET=<client secret>
   ```
5. Add to `/etc/zabbix-ai/config.yaml`:
   ```yaml
   oauth_google:
     client_id: 1234567890-abc.apps.googleusercontent.com
     client_secret_env: GOOGLE_OAUTH_CLIENT_SECRET
     allowed_email_domain: leapswitch.com
     default_role: operator
   ```
6. Restart and visit `/admin/login` — the "Sign in with Google" button
   appears alongside the password+TOTP form.

First-time SSO sign-in auto-provisions a user; the username is the email
address. SSO users skip TOTP enrollment (Google already does 2FA).

## Run a CLI investigation

```bash
python -m zabbix_ai investigate --instance monitoring --eventid 998877
python -m zabbix_ai investigate --instance monitoring --hostid 12345 --question "why is it slow?"
```

## Test

```bash
pytest -v
```

## Roadmap

- v0.1+v0.2 (this branch) — CLI, orchestrator, ~15 read-only tools
- v0.3 — Slack adapter ✓ (feat/v0.3-slack)
- v0.4 — Zabbix UI right-click adapter ✓ (feat/v0.4-zabbix-ui)
- v0.5 — Memory + pattern recognition + HostBill live lookup ✓ (feat/v0.5-memory-hostbill)
- v0.7 — Admin UI MVP (auth, dashboard, history, audit, memory) ✓ (feat/v0.7-admin-ui)
- v0.8 — Forecasting + anomaly tools (forecast.linear, anomaly.iqr, anomaly.zscore) ✓
- v1.0 — GA ✓ (deployed at https://zabbix-ai.lsnw.io/)
- v1.1 — HostBill webhook + customer ticket flow
- v1.2 — Optional auto-mode for Disaster severity
- v1.3 — Host briefing pre-fetch (token-efficient correlation across 30-day metrics + history) ✓
- v1.3.1 — Security hardening (CSRF, rate limit, single-use tokens, SSRF deny-list, audit log, TOTP replay-cache) ✓
- v1.4.0 — Admin: user management UI, cost dashboard, system status page. Tools: diag.network, diag.cert_expiry, diag.smart. Agent-side install guide ✓
- v1.4.{1,2,3,4,5} — diag.disk_usage iterations + diag.windows_winsxs (Windows space consumers) ✓
- v1.5.0 — Trust loop: auto-investigate-on-alert webhook, resolution-notes feed-forward, daily ₹ budget cap, outcome inference, HostBill linkage foundation ✓

## Auto ticket + Slack follow-up flow (ticket-flow, optional)

Automates the NOC's manual loop: raise a HostBill ticket, post to Slack, and
chase follow-ups. Disabled by default; builds on the auto-investigate webhook.

**Lifecycle**

1. A qualifying Zabbix problem (severity ≥ `ticket_min_severity`) is
   auto-investigated as usual.
2. Instead of a one-shot summary, the bot posts an **AI-drafted ticket** to the
   auto-investigate Slack channel with **Approve / Discard** buttons. No ticket
   exists yet.
3. On **Approve**, a HostBill ticket is created — a *customer* ticket if the host
   is confidently linked to a HostBill client (`host_hostbill_link`), otherwise
   an *internal* ticket to `internal_department_id`.
4. A background loop posts **escalating reminders** in the Slack thread
   (`nudge_schedule_minutes`) until one of:
   - a human **replies on the ticket** → hand off to the NOC, stop nudging;
   - the **Zabbix problem recovers** → post "resolved" and close the ticket;
   - **3 days** with no reply → go quiet (ticket stays open);
   - **6 days** still active → post an approval-gated prompt to **disable
     monitoring** in Zabbix (ICMP-only host → disable the host; agent trigger →
     disable that trigger; otherwise a maintenance window). Requires a Zabbix
     write token; without one the bot only posts an alert.

**Setup**

1. Give the HostBill API user ticket-write perms (`addTicket`, `addTicketReply`,
   `setTicketStatus`) on top of the read perms.
2. In the Slack app, set **Interactivity → Request URL** to
   `https://<your-host>/slack/interactions`.
3. (For the 6-day disable) create a write-role Zabbix token and set
   `write_token_env` on the instance + export that env var.
4. Add the `ticket_flow:` block to `/etc/zabbix-ai/config.yaml`
   (see `config.example.yaml`). **Start with `dry_run: true`** and a
   `test_slack_channel` to validate the flow end-to-end, then flip to live.
5. Restart: `systemctl restart zabbix-ai`.

State for each problem lives in the `incidents` table (migration 008); every
ticket create and Zabbix write is recorded in `audit_log`.

## Operational constraints (run a single instance)

This service is designed to run as **one process on one node**, and that assumption
is currently load-bearing:

- **Background workers are per-process singletons** with no leader election —
  `resolution_poller`, `outcome_inference`, `hostbill_sync`, and (when ticket-flow
  is enabled) `followup_worker` all start unconditionally in `lifespan`. Running two
  instances would **double-run** them and duplicate their side effects (Slack nudges,
  Zabbix write-backs, ticket follow-ups).
- **The auto-investigate webhook has side effects** (writes back to Zabbix, posts to
  Slack, may create incidents). Two instances behind a load balancer could process the
  same event twice. (The `incidents` table is unique on `(instance, eventid)`, which
  prevents duplicate *tickets*, but not duplicate investigations/Slack posts.)
- **State is SQLite** (`sqlite_path`), single-writer — it cannot be shared across nodes.

**If you ever need HA / horizontal scale:** move state to Postgres and run the workers
as a single leader-elected (or externally-queued) process separate from the web tier.
Until then, keep it to one `zabbix-ai` instance.

Worker startup health is visible at `/admin/status` (the `workers` field) — a worker that
fails to start is logged and recorded there rather than silently swallowed.
