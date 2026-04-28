# Zabbix RCA AI — Design Spec

**Date:** 2026-04-28
**Status:** Draft, pending user review
**Repo:** `git@github.com:Leapswitch-Networks/zabbix-rca-AI.git`

## 1. Problem & Goals

Leapswitch operates 3 Zabbix instances (`monitoring.leapswitch.com`,
`dcmonitoring.leapswitch.com`, `monitoring.stradsolutions.com`) covering
1000+ hosts across multiple customer-facing teams (Support, CloudPE, Cloudjiffy,
DC, Network, Monitoring/Acronis). Alerts fire to Slack and escalate via Twilio
calls, but root-cause analysis still happens entirely in human heads. L1/L2
engineers spend 15–25 minutes per incident reading dashboards, SSHing in,
correlating problems, and writing replies.

**Goal:** an AI service that performs the diagnostic legwork on demand, posts a
clear root cause + suggested actions, and stages drafts for customer-facing
replies — without ever mutating production state.

The system covers three use cases driven by the user's original request:

1. **Triage** — explain what's likely going wrong when a problem fires
2. **Correlation** — show related problems, deduplicate noise, link recurring patterns
3. **Prediction** — forecast threshold breaches and flag anomalous metric behaviour

All three are **on-demand only** in v1: the AI never auto-fires or auto-replies.

### Out of scope (v1)
- Autonomous response to alerts (deferred to v1.2)
- Write actions on hosts (deferred indefinitely; would require a separate human-approval queue)
- Auto-sending customer ticket replies
- Multi-region HA

## 2. Users & Entrypoints

| Entrypoint | Who uses it | Trigger |
|---|---|---|
| Slack mention | NOC, team TLs | `@zabbix-ai` in any alert thread or channel |
| Zabbix UI right-click | NOC, L1/L2 | Frontend Script "Investigate with AI" on Problems view |
| HostBill webhook | Support team (customer-driven) | New customer ticket creates webhook |
| CLI | Developers, ops | `python -m zabbix_ai.cli investigate --eventid=…` |
| Admin UI | Admins, operators | `/admin` for config + memory management |

All entrypoints share one orchestrator and one tool registry.

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                         Entrypoints                            │
│  Slack mention   Zabbix UI URL   HostBill webhook   CLI        │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
        ┌─────────────────────────────────────────────────┐
        │       zabbix-ai service (FastAPI, Python 3.12)  │
        │  Orchestrator (Claude tool-use loop)             │
        │  Tool registry (read-only)                       │
        │  Memory layer (SQLite)                           │
        │  Renderers (Slack, HTML/SSE, HostBill draft)     │
        │  Admin UI (Jinja2 + HTMX)                        │
        └──┬──────────────┬──────────────┬─────────────────┘
           ▼              ▼              ▼
     Zabbix API    Claude API      SQLite (state +
     (3 instances) (cached         encrypted secrets)
                    prompts)
                          │
                          ▼
                  Zabbix agent on host
                  (UserParameter diag.*)
```

**Service:** single FastAPI process behind nginx on one Linux VM.
**State:** SQLite (single file). Backed up nightly.
**No HA in v1.** Slack/HostBill retry on failure; Zabbix UI shows error.

## 4. Tool Registry (read-only)

The orchestrator exposes a fixed set of tools to Claude. There are no write tools.

```
zabbix.get_problem(eventid, instance)
zabbix.get_open_problems(hostid|hostgroupid)
zabbix.get_host(hostid)
zabbix.get_history(hostid, keys[], range)
zabbix.get_related_problems(eventid, window="1h")

diag.df, diag.free, diag.uptime, diag.top, diag.iostat,
diag.dmesg_tail, diag.journal_tail(unit, lines),
diag.systemctl_status(unit), diag.ss_listen, diag.ps_aux,
diag.mysql_status, diag.mysql_processlist, diag.apache_status,
diag.curl_headers(url), diag.ssl_cert_check(domain),
diag.dns_resolve(domain), diag.tcp_connect(host, port)

forecast.linear(hostid, key, horizon)
forecast.holt_winters(hostid, key, horizon)
anomaly.iqr(hostid, key, range)
anomaly.zscore(hostid, key, range)

correlate.related_problems(eventid)
correlate.similar_hosts(hostid)

lookup.host_by_domain(domain)
lookup.customer_services(customer_id)
lookup.host_by_ip(ip)

memory.find_similar_past_investigations(hostid|pattern, limit=3)
memory.get_host_facts(hostid)
memory.find_pattern(signature)
memory.find_resolved_tickets(alert_pattern, limit=5)

hostbill.get_ticket(ticket_id)
hostbill.draft_reply(ticket_id, text)   # only "write" — stages a draft, never sends
```

Tools defined at `zabbix_ai/tools/*.py`; registered in
`zabbix_ai/tools/__init__.py:ALLOWED_TOOLS`. Wrappers validate arguments
against allowlists before calling external APIs.

## 5. Diagnostic Mechanism (how the AI reaches hosts)

**The AI never gets a shell.** Diagnostics run via Zabbix agent UserParameters.

### Agent config (deployed via existing config-management to all hosts)

`/etc/zabbix/zabbix_agentd.d/diag.conf`:

```conf
UserParameter=diag.df,df -hP
UserParameter=diag.free,free -m
UserParameter=diag.uptime,uptime
UserParameter=diag.top,top -bn1 | head -30
UserParameter=diag.dmesg_tail,dmesg -T 2>/dev/null | tail -100
UserParameter=diag.journal_tail[*],journalctl -n $1 --no-pager 2>/dev/null
UserParameter=diag.systemctl_status[*],systemctl status $1 --no-pager 2>/dev/null
UserParameter=diag.ss_listen,ss -tunap 2>/dev/null
UserParameter=diag.ps_aux,ps auxf --sort=-%mem | head -40
UserParameter=diag.iostat,iostat -xz 1 2 2>/dev/null
UserParameter=diag.mysql_status,mysqladmin --defaults-file=/etc/zabbix/.my.cnf status 2>/dev/null
UserParameter=diag.mysql_processlist,mysql --defaults-file=/etc/zabbix/.my.cnf -e 'SHOW FULL PROCESSLIST' 2>/dev/null
UserParameter=diag.apache_status,curl -s http://127.0.0.1/server-status?auto 2>/dev/null

AllowKey=diag.*
DenyKey=system.run[*]
```

Agent runs as `zabbix` user (no root). MySQL creds in
`/etc/zabbix/.my.cnf` mode 0400 owned by `zabbix:zabbix`, account
`zabbix-ai-readonly` with grants `SELECT, PROCESS, REPLICATION CLIENT`.

### Trust chain

```
AI service ──API token (read-only role)──► Zabbix server
Zabbix server ──existing PSK/cert──► Zabbix agent
Zabbix agent ──fixed UserParameters as user 'zabbix'──► OS
```

The AI service holds **one** Zabbix credential (per instance): a token bound
to a `zabbix-ai-bot` user with role = read-only (no `*.create/update/delete`,
no UI config, optional API).

### On-demand execution

For each diag tool call:

1. Resolve `hostid` + `key` → Zabbix item (auto-create item with template if missing)
2. Call `task.create` with `type=6` (check now)
3. Poll `history.get` for fresh value (timeout 15s)
4. Return value to Claude

## 6. Safety Rules

1. **Read-only by construction.** Tool registry contains no mutating Zabbix or OS operations.
2. **API token scope.** `zabbix-ai-bot` user has read-only role. Mutations rejected by Zabbix even if attempted.
3. **Agent allowlist.** `AllowKey=diag.*` + `DenyKey=system.run[*]`. Agent cannot run anything else.
4. **Tool input validation.** Every wrapper checks types and enums before calling Zabbix.
5. **Bounded loop.** Max 8 tool calls, max 50k input + 10k output tokens per investigation.
6. **HostBill draft-only.** `hostbill.draft_reply` stages a private draft. Sending requires explicit human click.
7. **Audit log.** Append-only SQLite table; every tool call recorded with inputs/outputs.
8. **Prompt-injection isolation.** Tool dispatch matches against fixed Python registry; unknown tool names are rejected before any external call.

## 7. Token & Cost Strategy

| Technique | Effect |
|---|---|
| Anthropic prompt caching for system prompt + tool defs + host inventory | ~90% cost cut on cached blocks (5-min TTL, 1-h refresh on inventory) |
| Lean tool outputs (truncate to N lines, summary stats) | 5–10× less context |
| Model tiering (Haiku for summarisation/classification, Sonnet for orchestrator, Opus on demand) | 5–15× cost cut on subtasks |
| Investigation cache (same eventid in 30 min) | Zero cost on duplicates |
| Hard caps (8 tool calls, 50k/10k tokens) | Per-investigation ceiling |

Target: **$0.01–0.03** per typical investigation, ≪$0.005 on cache hits.

Daily cost dashboard surfaced in admin UI + posted to ops channel.

## 8. Memory ("more brain")

Per-investigation conversation is stateless. Long-term memory lives in SQLite
across investigations.

### Schema

```sql
CREATE TABLE investigations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL, instance TEXT,
    eventid INTEGER, ticket_id INTEGER, customer_id INTEGER,
    hostid INTEGER, hostname TEXT,
    started_at TIMESTAMP, duration_ms INTEGER,
    tokens_in INTEGER, tokens_out INTEGER, model TEXT,
    summary TEXT, root_cause TEXT, suggested_actions TEXT,
    confidence TEXT, pattern_signature TEXT
);
CREATE TABLE host_facts (
    hostid INTEGER, key TEXT, value TEXT,
    source_investigation_id INTEGER, learned_at TIMESTAMP,
    PRIMARY KEY (hostid, key)
);
CREATE TABLE patterns (
    signature TEXT PRIMARY KEY,
    first_seen TIMESTAMP, last_seen TIMESTAMP, occurrences INTEGER,
    typical_root_cause TEXT, typical_fix TEXT, confidence_score REAL
);
CREATE TABLE ticket_resolutions (
    ticket_id INTEGER PRIMARY KEY,
    alert_pattern TEXT, resolution_text TEXT,
    customer_id INTEGER, hostname TEXT, closed_at TIMESTAMP
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY, ts TIMESTAMP,
    investigation_id INTEGER,
    event_type TEXT, tool_name TEXT,
    tool_input TEXT, tool_output TEXT,
    user TEXT, source TEXT
);
```

### Write-back loop

After each investigation:
1. Cheap Haiku call: "summarise root cause + extract reusable host facts"
2. Insert into `investigations`; upsert `host_facts`
3. Compute pattern signature (stable hash of host group + alert key + key evidence terms); upsert `patterns` (`occurrences++`, refresh `last_seen`)

### Seeding

`monitoring_team_tickets.csv` (~180k rows) ingested once at install: cluster by
alert pattern, populate `ticket_resolutions`. Day-1 memory bootstrap.

### Memory tools (already in registry)

- `memory.find_similar_past_investigations`
- `memory.get_host_facts`
- `memory.find_pattern`
- `memory.find_resolved_tickets`

## 9. Adapters & Renderers

### 9.1 Slack mention

```
@zabbix-ai in alert thread
  → /slack/events (HMAC verify)
  → adapters/slack.py extracts eventid from thread message metadata
  → posts "🔍 Investigating…" placeholder
  → orchestrator.investigate(ctx)
  → renderers/slack.py updates with Block Kit reply
```

### 9.2 Zabbix UI right-click

Configured via Zabbix Configure → Scripts:
- Name: `Investigate with AI`
- Scope: `Manual event action`
- Type: `URL`
- URL: `https://zabbix-ai.internal/investigate?eventid={EVENT.ID}&instance=monitoring&token=<HMAC>`
- Permissions: NOC user groups only

The endpoint serves an HTML page that streams Server-Sent Events
(`tool_call`, `tool_result`, `thinking`, `final`) so the user sees progress
live. A "Post to Slack" button at the end shares the result.

URL `token` is HMAC(`eventid|instance|exp`) with shared secret stored on the
RCA VM and on the Zabbix server (used by a small wrapper script). Alternative
in v1: skip the HMAC and IP-restrict the endpoint to internal subnets.

### 9.3 HostBill webhook (v1.1)

```
HostBill → POST /hostbill/triage (HMAC verify)
  → resolve domain to hostid
  → orchestrator.investigate(ctx)
  → 1. Slack post in #support-ai with [Approve] [Edit] [Escalate]
  → 2. hostbill.draft_reply stages reply (private note, not sent)
  → On Approve in Slack → hostbill.send_draft(ticket_id)
```

### 9.4 CLI

`python -m zabbix_ai.cli investigate --eventid=998877 --instance=monitoring`
Prints reasoning + result; useful for testing and scripted bulk runs.

## 10. Auth, Secrets, Deployment

### 10.1 Secret placement

All secrets live on the **RCA VM**. The frontend (Zabbix UI / browser)
never sees keys.

**Bootstrap secrets (`/etc/zabbix-ai/env`, mode 0600):**
- `AGE_MASTER_KEY` — wraps the SQLite data encryption key
- `URL_SIGNING_KEY` — HMAC for Zabbix UI signed URLs
- `BOOTSTRAP_ADMIN_PASSWORD` — first admin login (changed at first login)

**Encrypted secrets (SQLite, editable via Admin UI):**
- Anthropic API key
- Zabbix API tokens (per instance)
- Slack bot token + signing secret
- HostBill API key + webhook HMAC secret

Envelope encryption: master key (env) → DEK (SQLite) → individual secrets.
Decryption only in-memory at tool-call time.

**Per-host MySQL creds:** `/etc/zabbix/.my.cnf` mode 0400 owned `zabbix:zabbix`,
account `zabbix-ai-readonly`. Already standard for Zabbix MySQL templates.

### 10.2 Identity

- Zabbix: dedicated `zabbix-ai-bot` user per instance, role = Read-only
- Slack: dedicated `zabbix-ai` bot, scopes `app_mentions:read`, `chat:write`,
  `chat:write.public`, `channels:history`
- HostBill: dedicated API user with read tickets + create draft/internal note
  permissions only (no send permission)
- Service runs as Linux user `zabbix-ai`, no sudo, no shell login

### 10.3 Topology

Single internal VM, 2 vCPU / 4 GB RAM / 20 GB disk.

```
systemd unit: zabbix-ai.service
Python 3.12 venv, FastAPI + uvicorn, nginx in front
SQLite at /var/lib/zabbix-ai/state.db (nightly backup)
Config at /etc/zabbix-ai/{config.yaml,env}
Logs in journald + /var/log/zabbix-ai/

Inbound (443, internal network only):
  /slack/events             ← Slack (HMAC verify)
  /hostbill/triage          ← HostBill (HMAC verify)
  /investigate              ← staff browsers (signed URL or IP-allowlist)
  /admin/*                  ← admins (session cookie + TOTP)
  /metrics                  ← Prometheus
  /healthz                  ← internal monitor

Outbound (443):
  api.anthropic.com
  monitoring.leapswitch.com, dcmonitoring.leapswitch.com,
    monitoring.stradsolutions.com
  slack.com
  hostbill domain
```

## 11. Admin UI

- Same FastAPI service, mounted at `/admin`
- Server-rendered Jinja2 + HTMX (no React build)
- Auth: local users + TOTP, session cookies (signed, HttpOnly, Secure)
- Roles: `admin` / `operator` / `viewer`
- Login rate-limited; brute-force lockout

### Pages

| Page | Edit allowed by | Purpose |
|---|---|---|
| Connections / Zabbix | admin | Add/edit/delete instance + token; test-connection; last-success timestamp |
| Connections / Slack | admin | Bot token, signing secret, channel allowlist, channel↔host-group map |
| Connections / HostBill | admin | URL, API key, webhook HMAC; test-connection |
| Connections / Anthropic | admin | API key, default model, per-use-case model override |
| Diagnostics allowlist | admin | View `diag.*` registry (read-only); coverage report (which hosts have UserParameters deployed) |
| Users & access | admin | Create/disable users, role assign, TOTP reset |
| Memory / Patterns | operator | Browse, edit `typical_fix`, adjust `confidence_score`, delete bad patterns |
| Memory / Host facts | operator | Browse + edit AI-learned host facts |
| Investigations | viewer | History, filter by source/host/customer, cost, re-render |
| Audit log | viewer | All tool calls + config changes |
| Health | viewer | Token validity, agent UserParameter coverage, daily cost, error rate |

Every config change writes a row to `audit_log`
(`event_type=config_change`, secrets redacted, user, timestamp).

## 12. Error Handling & Observability

| Failure | Response |
|---|---|
| Zabbix API timeout | Retry once, return tool error to Claude (it adapts) |
| Diagnostic stale (agent unreachable) | Return "agent timeout" to Claude |
| Claude API 5xx / overloaded | Backoff + retry; on hard fail, post error with investigation_id |
| Tool budget exhausted | Stop loop; ask Claude to summarise with what it has |
| Token budget exhausted | Stop early at hard cap |
| HostBill draft failure | Still post Slack analysis; flag draft as "manual paste needed" |

### Metrics (Prometheus on `/metrics`)

- `investigations_total{source,outcome}`
- `investigation_duration_seconds{source}`
- `tool_calls_total{name,outcome}`
- `claude_tokens_total{kind=in|out|cached}`
- `claude_cost_usd_total`

Daily digest in `#zabbix-ai-ops`: investigation count, top patterns,
total cost, error rate.

### Rate limits

- Per-user (Slack): 10 investigations / 5 min
- Per-ticket: 1 investigation, cached 30 min
- Global: 60 concurrent investigations (uvicorn worker bound)

## 13. Testing Strategy

### Unit
- Tool wrappers — mocked Zabbix client, allowlist enforcement, argument validation
- Memory layer — in-memory SQLite fixture, schema migrations, idempotent upserts
- Renderers — snapshot tests for Slack Block Kit JSON, HTML, HostBill draft
- Admin UI — auth, RBAC, secret round-trip encryption

### Integration (real Claude API, mocked Zabbix/Slack/HostBill)
- Happy path — canned alert → expected tool sequence → expected output schema
- Tool error recovery — inject Zabbix timeout on call #2, verify Claude continues
- Budget exhaustion — slow tools, verify hard cap honoured
- Prompt injection — malicious alert text, verify no out-of-registry calls

### Pre-prod soak
- 1-week dry run on `dcmonitoring` Slack-only with `--dry-run` (logs Claude
  output, never posts)
- Compare RCA against actual L1 resolution from `monitoring_team_tickets.csv`
  for accuracy baseline
- Then enable Slack posting in `#zabbix-ai-test` for 1 week before opening
  to teams

## 14. Phased Rollout

| Phase | Scope | Duration | Gate |
|---|---|---|---|
| v0.1 skeleton | FastAPI service, config loader, Zabbix client (multi-instance), audit log, SQLite schema, 3 tools, no Claude | 1 week | unit tests pass; CLI dry-run works |
| v0.2 Claude loop | Full orchestrator with tool-use loop, prompt cache, ~15 tools, CLI adapter | 1 week | E2E CLI investigation produces sensible RCA on 5 historical incidents |
| v0.3 Slack adapter | Events handler, mention recognition, Block Kit renderer (#zabbix-ai-test only) | 1 week | 50 successful investigations, no escapes, cost < $30 |
| v0.4 Zabbix UI adapter | Signed URL endpoint, SSE streaming, HTML renderer, Frontend Script wired | 1 week | NOC uses on 20+ real problems |
| v0.5 Memory + patterns | Investigation summary write-back, pattern recognition, ticket history seed | 1 week | Pattern table reaches 50+ entries with demonstrable hit rate |
| v0.6 Forecasting/correlation | `forecast.holt_winters`, `anomaly.iqr`, `correlate.related_problems` | 1 week | 5 metrics: predictions match observed within 10% |
| v0.7 Admin UI | Auth + TOTP, connection pages, encrypted secret store, health, audit viewer | 1 week | All config changeable through UI; secrets round-trip safely |
| v1.0 GA NOC | Open to all teams; daily digest; Prometheus dashboards; runbook | — | — |
| v1.1 HostBill | Webhook adapter, customer flow, draft-not-send, support team rollout | 2 weeks | 50 customer tickets handled, L1 approval rate > 70% |
| v1.2 Auto-mode (optional) | Mediatype webhook on Disaster severity → auto-investigate, post to alert thread | 1+ week | Only if v1.0/1.1 trust established |

Each phase is independently deployable and produces value on its own.

## 15. Repo Layout

```
zabbix-rca-AI/
├── pyproject.toml
├── README.md
├── docs/
│   └── superpowers/specs/2026-04-28-zabbix-rca-ai-design.md
├── zabbix_ai/
│   ├── __main__.py            # uvicorn entry
│   ├── app.py                  # FastAPI app, mounts adapters
│   ├── orchestrator.py         # Claude tool-use loop
│   ├── prompts.py              # system prompt, cached blocks
│   ├── memory.py               # SQLite read/write helpers
│   ├── config.py               # config loader + validation
│   ├── audit.py                # append-only audit log
│   ├── crypto.py               # envelope encryption
│   ├── tools/                  # tool registry + wrappers
│   ├── clients/                # zabbix, claude, slack, hostbill
│   ├── adapters/               # slack, zabbix_ui, hostbill, cli
│   ├── renderers/              # slack, html, hostbill
│   └── admin/                  # FastAPI router, templates, static
├── deploy/
│   ├── zabbix-agent/diag.conf
│   └── systemd/zabbix-ai.service
├── migrations/                 # SQLite schema migrations
└── tests/
    ├── unit/
    └── integration/
```

## 16. Open Questions

- Final list of `diag.*` UserParameters (12 listed; we may add `nginx_status`,
  `redis_info` based on which services live on which hosts; coordinate with
  the existing setup docs in `/home/deepak/zabbix/docs/`)
- Per-instance API token rotation cadence (proposed: 90 days, surfaced in admin UI)
- Whether to expose forecasting tools in v0.2 or wait for v0.6 (current plan: v0.6)
- HostBill: do we need a "Don't auto-investigate this customer" allowlist? (proposed: yes, store in admin UI)
