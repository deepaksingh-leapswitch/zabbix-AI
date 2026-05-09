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
- v0.6 — Forecasting / anomaly detection
- v0.7 — Admin UI (auth, encrypted secret store)
- v1.0 — GA
- v1.1 — HostBill webhook + customer ticket flow
- v1.2 — Optional auto-mode for Disaster severity
