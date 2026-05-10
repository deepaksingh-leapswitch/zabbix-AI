# zabbix-ai operational runbook

How to keep the service healthy, upgrade it, debug it, and recover from
common incidents. Assumes the deploy-doc setup at `/opt/zabbix-ai`.

---

## Common operations

### Status / start / stop / restart

```bash
sudo systemctl status zabbix-ai --no-pager
sudo systemctl restart zabbix-ai
sudo systemctl stop zabbix-ai
sudo systemctl start zabbix-ai
```

### Logs

App logs go to journald via uvicorn:

```bash
journalctl -u zabbix-ai --since '30 min ago'
journalctl -u zabbix-ai -f                    # tail
journalctl -u zabbix-ai --since '1 hour ago' | grep ERROR
```

The audit log of every Claude tool call is in SQLite at
`/var/lib/zabbix-ai/state.db`, table `audit_log`. View via the admin UI
at `/admin/audit` or with `sqlite3`:

```bash
sudo -u zabbix-ai sqlite3 /var/lib/zabbix-ai/state.db \
  "SELECT ts,event_type,tool_name,substr(tool_input,1,80) FROM audit_log ORDER BY id DESC LIMIT 20"
```

### Health check

```bash
curl -fsSL https://zabbix-ai.lsnw.io/healthz
# {"ok":true,"version":"0.8.0"}
```

### Smoke-test an investigation from CLI

```bash
sudo -u zabbix-ai /opt/zabbix-ai/.venv/bin/python -m zabbix_ai \
    --config /etc/zabbix-ai/config.yaml list-instances

# Pick an open eventid from the Zabbix UI, then:
sudo -u zabbix-ai env $(grep -v '^#' /etc/zabbix-ai/env | xargs) \
    /opt/zabbix-ai/.venv/bin/python -m zabbix_ai \
    --config /etc/zabbix-ai/config.yaml \
    investigate --instance monitoring --eventid <ID>
```

---

## Upgrading

```bash
# from your laptop:
rsync -az --exclude .venv --exclude .git --exclude data \
    --exclude __pycache__ --exclude '*.sqlite*' --exclude .env.local \
    --exclude config.local.yaml /home/leap/rca/ \
    root@VM:/opt/zabbix-ai/

# on the VM:
sudo chown -R zabbix-ai:zabbix-ai /opt/zabbix-ai
sudo -u zabbix-ai /opt/zabbix-ai/.venv/bin/pip install -e /opt/zabbix-ai --quiet
sudo systemctl restart zabbix-ai
sudo systemctl status zabbix-ai --no-pager
```

Schema migrations apply automatically at startup.

## Rolling back

If a new release misbehaves:

```bash
# on the VM:
sudo systemctl stop zabbix-ai
cd /opt/zabbix-ai
sudo -u zabbix-ai git checkout v0.7.0  # or any earlier tag (if you used git-clone deploy)

# OR — if you deployed via rsync, rsync from a known-good source tree:
# from your laptop:
git -C /home/leap/rca worktree add /tmp/rca-rollback v0.7.0
rsync -az --exclude .venv --exclude .git ... /tmp/rca-rollback/ root@VM:/opt/zabbix-ai/

# then:
sudo systemctl start zabbix-ai
```

If the new release ran a forward migration that you can't tolerate, restore
the SQLite file from backup:

```bash
sudo systemctl stop zabbix-ai
sudo -u zabbix-ai cp /var/lib/zabbix-ai/state.db.bak /var/lib/zabbix-ai/state.db
sudo systemctl start zabbix-ai
```

## Backups

SQLite is the only persistent state. Snapshot it nightly:

```bash
# add to /etc/cron.daily/zabbix-ai-backup:
#!/bin/sh
test -f /var/lib/zabbix-ai/state.db || exit 0
sudo -u zabbix-ai sqlite3 /var/lib/zabbix-ai/state.db \
    ".backup '/var/lib/zabbix-ai/state.db.$(date +%Y%m%d)'"
find /var/lib/zabbix-ai -name 'state.db.20*' -mtime +14 -delete
```

`chmod +x /etc/cron.daily/zabbix-ai-backup`.

## TLS certificate

Certbot auto-renews via systemd timer. Verify:

```bash
sudo systemctl list-timers | grep certbot
sudo certbot certificates
sudo certbot renew --dry-run
```

If the cert ever expires (auto-renew failed), reissue:

```bash
sudo certbot --nginx -d zabbix-ai.lsnw.io --redirect
```

---

## Common issues

### "Repository not found" / token expired

- **Zabbix API token expired** → `Application error.: API token expired.`
  Regenerate the token in Zabbix UI → Users → API tokens. Update
  `/etc/zabbix-ai/env` and restart.
- **Anthropic API key invalid** → Claude calls fail with 401. Rotate the
  key at console.anthropic.com, update env, restart.

### "diag scripts not bootstrapped for instance"

Bootstrap is best-effort. If it failed at startup (Zabbix unreachable,
API token bad), the AI will say this when calling a `diag.*` tool. Fix:

1. Verify the token: `curl -X POST https://monitoring.leapswitch.com/api_jsonrpc.php
   -H 'Content-Type: application/json-rpc' -H 'Authorization: Bearer $TOKEN'
   -d '{"jsonrpc":"2.0","method":"host.get","params":{"limit":1},"id":1}'`
2. Restart the service — bootstrap runs on every start.

### Slack adapter not responding

- Check the bot token + signing secret in `/etc/zabbix-ai/env` are valid.
- Slack Events Request URL must match your VM's HTTPS endpoint exactly.
- The bot must be invited to the channel where you mention `@zabbix-ai`.
- Check logs: `journalctl -u zabbix-ai --since '5 min ago' | grep slack`.

### Admin UI: "session expired" loop

- Cookie clock skew. Verify the VM's clock is right: `timedatectl`.
- Session secret was rotated → all sessions invalid; users re-login.

### `script.execute` timeouts

- The Zabbix server-to-agent path may be slow. Default Zabbix script
  timeout is 30s, our HTTP client timeout is 60s. If a host is overloaded
  the agent itself can't keep up.
- Check the host's CPU / memory: maybe the host needs attention before
  diagnostics can run.

### "command line is too long" on Windows

- A Windows diag's encoded PowerShell exceeded 8191 chars. Trim the
  PS source, encode via base64-of-UTF-16LE, redeploy. See
  `services/script_bootstrap.py` for examples.

---

## Capacity guidance

- **One investigation costs $0.01–0.04** in Anthropic tokens (Sonnet 4.6
  + cached system prompt).
- **Per-investigation latency:** 30s–2 min depending on tool count.
- **Concurrent investigations:** uvicorn defaults to 1 worker; that
  serializes investigations. To scale: bump uvicorn workers in the
  systemd unit, OR increase httpx pool size, OR move to gunicorn+uvicorn
  workers if you need parallelism. SQLite write contention only matters
  at >50 concurrent investigations.
- **Disk:** SQLite grows ~1 KB per investigation (audit + summary).
  10K investigations ≈ 10 MB.

---

## Who to call

- **Service down** (`/healthz` doesn't return) → Deepak Singh (Monitoring TL)
- **TLS expired** → Akar Periwal (Network TL)
- **Customer ticket urgent** → Suresh Thaware / Ajinkya Lawand (Support TLs)
- **Anthropic API issue** → check status.anthropic.com first, then escalate
