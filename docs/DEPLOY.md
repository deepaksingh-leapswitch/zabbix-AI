# Deploying zabbix-ai

End-to-end deployment for Ubuntu 22.04/24.04. Single VM, behind nginx
with Let's Encrypt TLS. Idempotent — re-run safely.

## Prerequisites

- VM with public IP and DNS A record pointing at it
- Ports 22, 80, 443 open inbound
- Outbound 443 reachable to `api.anthropic.com`, your Zabbix instance(s),
  optionally `slack.com` and your HostBill domain
- Root SSH access

## One-time install

```bash
ssh root@your-vm
git clone git@github.com:deepaksingh-leapswitch/zabbix-AI.git /opt/zabbix-ai
sudo bash /opt/zabbix-ai/deploy/install.sh zabbix-ai.lsnw.io
```

The install script creates:

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/opt/zabbix-ai/` | zabbix-ai:zabbix-ai | 0755 | code + venv |
| `/etc/zabbix-ai/config.yaml` | zabbix-ai:zabbix-ai | 0640 | yaml settings |
| `/etc/zabbix-ai/env` | zabbix-ai:zabbix-ai | 0640 | secrets (env vars) |
| `/var/lib/zabbix-ai/` | zabbix-ai:zabbix-ai | 0750 | SQLite, logs |
| `/var/log/zabbix-ai/` | zabbix-ai:zabbix-ai | 0750 | application logs |
| `/etc/systemd/system/zabbix-ai.service` | root:root | 0644 | systemd unit |
| `/etc/nginx/sites-enabled/zabbix-ai` | root:root | 0644 | reverse proxy |

…and runs `certbot --nginx` to issue + auto-renew the cert.

## Fill in secrets

Edit `/etc/zabbix-ai/env`. Required:

```
# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Zabbix instances (one var per instance, name matches token_env in yaml)
ZABBIX_TOKEN_MONITORING=...
# ZABBIX_TOKEN_DCMONITORING=...

# Admin UI (v0.7+)
SESSION_SECRET=<openssl rand -hex 32>
BOOTSTRAP_ADMIN_PASSWORD=<temporary, clear after first login>

# Zabbix UI right-click adapter (v0.4+)
URL_SIGNING_KEY=<openssl rand -hex 32>

# Slack (only if enabling /slack/events; v0.3+)
# SLACK_BOT_TOKEN=xoxb-...
# SLACK_SIGNING_SECRET=...

# HostBill (only if enabling memory.find_resolved_tickets; v0.5+)
# HOSTBILL_API_ID=...
# HOSTBILL_API_KEY=...
```

Edit `/etc/zabbix-ai/config.yaml`. Adjust `zabbix_instances:` and uncomment
the optional sections (`slack:`, `zabbix_ui:`, `hostbill:`, `admin:`) that
match your enabled features.

Both files are mode 0640 owned by `zabbix-ai:zabbix-ai` — readable by the
service and root only.

## Start the service

```bash
sudo systemctl enable --now zabbix-ai
sudo systemctl status zabbix-ai --no-pager
```

Smoke tests:

```bash
curl -fsSL https://zabbix-ai.lsnw.io/healthz
# {"ok":true,"version":"0.7.0"}

# Admin UI (browser): https://zabbix-ai.lsnw.io/admin/login
#   sign in with admin / <BOOTSTRAP_ADMIN_PASSWORD>
#   enrol TOTP, then **clear BOOTSTRAP_ADMIN_PASSWORD from /etc/zabbix-ai/env**

# CLI investigation (only inside the VM, as the zabbix-ai user):
sudo -u zabbix-ai /opt/zabbix-ai/.venv/bin/python -m zabbix_ai \
    --config /etc/zabbix-ai/config.yaml list-instances
```

## Wire up Zabbix UI right-click

After the service is healthy, register a Zabbix Frontend Script of type
URL pointing at the AI service. See `deploy/zabbix-frontend-script.md`
for the PHP wrapper that signs the URL token, or have your Zabbix server
call the AI's `/sign` helper (planned).

## Wire up Slack

Create a Slack app at <https://api.slack.com/apps>. Bot scopes:
`app_mentions:read`, `chat:write`, `chat:write.public`, `channels:history`.
Put the bot token + signing secret in `/etc/zabbix-ai/env`, restart the
service, and set the Events Request URL in Slack to
`https://zabbix-ai.lsnw.io/slack/events`. Invite the bot to alert
channels and `@zabbix-ai` to investigate.

## Upgrades

```bash
cd /opt/zabbix-ai
sudo -u zabbix-ai git pull
sudo -u zabbix-ai .venv/bin/pip install -e . --quiet
sudo systemctl restart zabbix-ai
```

Migrations run automatically at startup.

## Rollback

```bash
cd /opt/zabbix-ai
sudo -u zabbix-ai git checkout v0.6.0   # or whichever previous tag
sudo -u zabbix-ai .venv/bin/pip install -e . --quiet
sudo systemctl restart zabbix-ai
```

SQLite migrations are forward-only; rolling back code that wrote new
schema may leave the DB at a higher schema_version than the code expects.
For schema rollback, restore the SQLite file from backup
(`/var/lib/zabbix-ai/state.db.bak.*`).

## Logs

```bash
journalctl -u zabbix-ai --since '1 hour ago'
```

App writes structured JSON to journald. The audit log of every Claude
tool call lives inside SQLite (`audit_log` table) — view via the admin UI
at `/admin/audit`.
