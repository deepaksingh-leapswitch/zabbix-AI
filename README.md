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
- v0.3 — Slack adapter
- v0.4 — Zabbix UI right-click adapter
- v0.5 — Memory + pattern recognition + ticket history seeding
- v0.6 — Forecasting / anomaly detection
- v0.7 — Admin UI (auth, encrypted secret store)
- v1.0 — GA
- v1.1 — HostBill webhook + customer ticket flow
- v1.2 — Optional auto-mode for Disaster severity
