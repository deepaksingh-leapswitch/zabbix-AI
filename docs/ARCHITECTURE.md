# Zabbix RCA AI — Architecture

End-to-end picture of how a Zabbix alert (or a question, or a customer
ticket) becomes an AI-produced root-cause analysis. Read alongside the
design spec at `docs/superpowers/specs/2026-04-28-zabbix-rca-ai-design.md`.

---

## High-level system

```
┌─────────────────── EVENT SOURCES ───────────────────┐    ┌──── REASONING ────┐
│                                                     │    │                   │
│  ┌──────────────┐   ┌──────────────┐                │    │  Anthropic Claude │
│  │ Zabbix UI    │   │ Slack        │                │    │   (Sonnet 4.6     │
│  │ right-click  │   │ @zabbix-ai   │                │    │    + Haiku 4.5)   │
│  │ "Investigate"│   │ in any thread│                │    │                   │
│  └──────┬───────┘   └──────┬───────┘                │    └─────────▲─────────┘
│         │                  │                        │              │
│         │  signed URL      │  events API + HMAC     │  prompt-cached
│         │  (HMAC, TTL)     │  signature             │  system prompt
│         ▼                  ▼                        │  + tool defs
│  ┌─────────────────────────────────────────┐        │              │
│  │              FastAPI service             │◀──────┘              │
│  │            (zabbix-ai, one VM)            │                      │
│  │                                           │                      │
│  │  ┌─────────────────────────────────────┐  │   tool-use loop     │
│  │  │   Orchestrator                       │──┼──────────────────────┘
│  │  │   (Claude tool-use loop, capped)     │  │
│  │  │   • investigate()        — final     │  │
│  │  │   • investigate_streaming() — SSE    │  │
│  │  │   • _write_back() at end (Haiku)     │  │
│  │  └──────────┬──────────────────────────┬┘  │
│  │             │                          │   │
│  │             ▼                          ▼   │
│  │  ┌──────────────────┐      ┌──────────────┐│
│  │  │  Tool registry    │      │  Memory      ││
│  │  │  (read-only)      │      │  (SQLite)    ││
│  │  │                   │      │              ││
│  │  │  zabbix.* (4)     │      │ investigations│
│  │  │  diag.*   (13)    │      │ patterns     ││
│  │  │  lookup.* (2)     │      │ host_facts   ││
│  │  │  memory.* (3)     │      │ audit_log    ││
│  │  └─────────┬─────────┘      └──────────────┘│
│  └────────────┼──────────────────────────────────┘
│               │
│               │  3 outbound channels (per tool)
│               │
│  ┌────────────┴────────────┬──────────────────┐
│  ▼                         ▼                  ▼
│ Zabbix API           Zabbix agent       HostBill admin API
│ (3 instances:        (UserParameters    (live ticket lookup,
│  monitoring,          diag.* allowlist,  optional — graceful
│  dcmonitoring,        AllowKey=diag.*)   "not configured"
│  strads)                                  if not set up)
│
│                                                     │
│   HostBill webhook (v1.1)                           │
│   →  draft-not-send for customer tickets            │
└─────────────────────────────────────────────────────┘
```

---

## Trust & data boundaries

```
┌───────────────────────────────────────────────────────────────────┐
│                       READ-ONLY FORTRESS                           │
│                                                                    │
│  Claude is given a fixed Python tool registry. It cannot call      │
│  anything outside it. Each layer below independently enforces      │
│  read-only:                                                        │
│                                                                    │
│   1. Tool registry         no write functions exist                │
│   2. Wrapper validation    arg types/enums checked before call     │
│   3. Zabbix API token      bound to a Read-only role user          │
│   4. Zabbix agent config   AllowKey=diag.*, DenyKey=system.run[*]  │
│   5. OS permissions        zabbix-agent runs as user `zabbix`,     │
│                            no sudo, no shell                       │
│                                                                    │
│  Defeating one layer leaves the other four in place.               │
└───────────────────────────────────────────────────────────────────┘
```

---

## End-to-end flow: NOC investigates an alert in Slack

```
T+0       Disk-low alert fires  ──►  Slack channel #cloudpe_dc3_alerts
T+0:30s   NOC engineer @-mentions the bot in the alert thread
          ──►  Slack Events API  ──HMAC SHA256──►  POST /slack/events
T+0:31s   adapters/slack.py
            ├─ verify_slack_signature() — 5-min window, hmac.compare_digest
            ├─ check channel allowlist → ok
            └─ parse_mention() pulls eventid + instance from message text
                 (or falls back to default_instance from config)
T+0:32s   post placeholder Block Kit message: "🔎 Investigating…"
T+0:32s   InvestigationRunner __aenter__:
            ├─ load all Zabbix instance clients (token from env)
            ├─ register tools (zabbix.*, diag.*, lookup.*, memory.*)
            ├─ open SQLite, run migrations
            └─ build Orchestrator (claude=ClaudeClient, memory, hostbill?)
T+0:33s   Orchestrator.investigate(ctx) starts the tool-use loop
            │
            │  Each iteration:
            │   1. Send prompt to Claude (system blocks are CACHED)
            │   2. If Claude returns tool_use blocks → execute via dispatch()
            │   3. Append tool_result blocks → feed back to Claude
            │   4. Stop when Claude returns end_turn OR
            │      tool_calls reached max_tool_calls cap
            │
            │  Concretely for "disk low on /var":
            │   • zabbix.get_problem(eventid)   → problem details + tags
            │   • zabbix.get_host(hostid)        → groups, interfaces
            │   • zabbix.get_history(...)        → 6h CPU/mem/disk history
            │   • diag.df(hostid, instance)      → df -hP via agent
            │   • diag.dmesg_tail(hostid, …)     → dmesg | tail -100
            │   • memory.find_pattern(signature) → "seen 4 times before"
            │   • memory.find_resolved_tickets(…) → HostBill, optional
            │
T+0:50s   Claude returns end_turn with:
            "Likely root cause: /var fills with mysql-bin logs.
             Confidence: high.  Suggested actions:
              1. Verify with `mysql -e 'SHOW BINARY LOGS'`
              2. PURGE BINARY LOGS BEFORE NOW() - INTERVAL 3 DAY
              3. Adjust expire_logs_days in my.cnf"
T+0:50s   _write_back():
            ├─ cheap Haiku call  →  {root_cause_short, fix_short, host_facts}
            ├─ compute_pattern_signature("disk low on /var",
            │                            "Managed cPanel VPS")
            ├─ upsert patterns table  (occurrences ++ )
            └─ upsert host_facts      (hostid, role/role)
T+0:52s   audit.log_end() updates the investigations row + audit_log row
T+0:52s   renderers/slack.py builds Block Kit
T+0:53s   chat.update on the placeholder → engineer sees the analysis
                                            in the SAME thread
```

---

## End-to-end flow: Zabbix UI right-click → live SSE

```
NOC engineer right-clicks a problem in Zabbix UI → "Investigate with AI"
   │
   │  Zabbix Frontend Script of type URL fires a tiny PHP wrapper
   │  (deploy/zabbix-frontend-script.md) which:
   │    1. Reads URL_SIGNING_KEY from its environment
   │    2. Builds a token: b64(payload).b64(exp).b64(hmac_sha256)
   │    3. 302 → https://zabbix-ai.internal/investigate?token=…
   │
   ▼
adapters/zabbix_ui.py
   ├─ verify_url_token() — TTL + signature checked
   └─ render_investigate_page(eventid, instance, sse_path)  →  HTML

Browser loads HTML
   ├─ tiny EventSource consumer in <script>
   └─ GET /investigate/stream?token=…    (SSE — text/event-stream)

adapters/zabbix_ui.py:/stream
   ├─ verify_url_token() again
   └─ async for ev in runner.investigate_streaming(ctx):
        yield {"event": ev.event, "data": json.dumps(ev.data)}

orchestrator.investigate_streaming(ctx)
   yields:  started → tool_call → tool_result → thinking → … → final

Browser appends each event to the DOM as it arrives — engineer sees
Claude's reasoning live, not just a final summary.
```

---

## Data flow per request

```
┌─────────────┐       ┌───────────┐       ┌───────────────┐
│ Slack /     │  HTTP │ FastAPI    │ tool   │ Zabbix API    │
│ Zabbix UI / │──────▶│ adapter    │ call   │ (token-auth)  │
│ HostBill    │       │ + verify   │ ──────▶│               │
└─────────────┘       └─────┬──────┘        └──────┬────────┘
                            │                       │
                            ▼                       ▼ task.create
                     ┌──────────────┐        ┌─────────────────┐
                     │ Orchestrator │        │ Zabbix agent    │
                     │ + Claude API │        │ on target host  │
                     └─────┬────────┘        │ (UserParameter  │
                           │  audit          │  diag.*)        │
                           ▼                  └─────────────────┘
                     ┌──────────────┐                ▲
                     │ SQLite (state│                │ result
                     │  + memory +  │                │
                     │  audit log)  │                │
                     └──────┬───────┘                │
                            │  patterns/host_facts   │
                            ▼                         │
                     ┌──────────────┐                │
                     │ Claude API   │ optional       │
                     │ (Haiku       │ HostBill       │
                     │  write-back) │ search         │
                     └──────────────┘ ──────────────▶│
                                                    │
                                              ┌──────────────┐
                                              │ HostBill     │
                                              │ admin API    │
                                              │ (optional)   │
                                              └──────────────┘
```

---

## Components — what lives where

| Component | File / Module | Layer |
|---|---|---|
| Slack adapter | `zabbix_ai/adapters/slack.py` | Entry |
| Zabbix UI adapter | `zabbix_ai/adapters/zabbix_ui.py` | Entry |
| CLI adapter | `zabbix_ai/adapters/cli.py` | Entry |
| Investigation runner | `zabbix_ai/services/investigation_runner.py` | Wiring |
| Orchestrator (tool-use loop) | `zabbix_ai/orchestrator.py` | Reasoning |
| Tool registry | `zabbix_ai/tools/__init__.py` | Reasoning |
| zabbix.* tools | `zabbix_ai/tools/zabbix.py` | Reasoning |
| diag.* tools | `zabbix_ai/tools/diag.py` | Reasoning |
| lookup.* tools | `zabbix_ai/tools/lookup.py` | Reasoning |
| memory.* tools | `zabbix_ai/tools/memory.py` | Reasoning |
| Memory + helpers | `zabbix_ai/memory.py` | Storage |
| Audit log | `zabbix_ai/audit.py` | Storage |
| URL signing | `zabbix_ai/url_signing.py` | Crypto |
| Slack signature | `zabbix_ai/adapters/slack.py` | Crypto |
| Zabbix client | `zabbix_ai/clients/zabbix.py` | I/O |
| Slack client | `zabbix_ai/clients/slack.py` | I/O |
| HostBill client | `zabbix_ai/clients/hostbill.py` | I/O |
| Claude client | `zabbix_ai/clients/claude.py` | I/O |
| Slack renderer | `zabbix_ai/renderers/slack.py` | View |
| HTML renderer | `zabbix_ai/renderers/html.py` | View |
| Text renderer (CLI) | `zabbix_ai/renderers/text.py` | View |

---

## Cost & performance — what each tool call costs

| Tool family | Cost per call | Why |
|---|---|---|
| zabbix.get_problem / get_host | <50ms, ~0.5k tokens | Single Zabbix API call, lean output |
| zabbix.get_history | 200–500ms, 1–3k tokens | Multiple item.get + history.get round trips, capped at 200 points each |
| diag.* | 1–15s, ~0.5–2k tokens | Real-time UserParameter execution; agent runs the command, we wait for fresh value |
| memory.find_* | <10ms, ~0.5k tokens | Local SQLite query |
| memory.find_resolved_tickets | 100–300ms (HostBill) | Live HostBill admin API call when configured, otherwise instant "not configured" |
| Claude reasoning | $0.01–0.03 per investigation | Sonnet 4.6 with prompt-cached system blocks (5-min TTL) — cache hit rate ~90% on repeated investigations |
| Write-back (Haiku) | $0.001 per investigation | One small JSON-extraction call |

Hard caps in the orchestrator: **8 tool calls per investigation**,
**50k input + 10k output tokens**, abort early on either.

---

## Security checklist

- [x] Read-only Claude tool registry — no write tools defined anywhere
- [x] Read-only Zabbix API user (`zabbix-ai-bot`) per instance, role lacks `*.create / *.update / *.delete`
- [x] Zabbix agent `AllowKey=diag.*` + `DenyKey=system.run[*]` — no shell escape
- [x] Agent runs as `zabbix` user, not root, no sudo
- [x] Slack webhook verified by HMAC SHA-256 + 5-minute timestamp window
- [x] Zabbix UI right-click URL signed by HMAC + TTL (default 300s)
- [x] HostBill API credentials never sent to Claude — only the search
      results pass through
- [x] All secrets in env (`/etc/zabbix-ai/env`, mode 0600). Admin UI in
      v0.7 will move them to encrypted SQLite with envelope encryption.
- [x] Append-only audit log of every tool call (inputs, outputs, who, when)
- [x] HostBill draft-not-send for customer replies (v1.1) — never auto-sends

---

## What's optional, what's required

```
                          REQUIRED
                          ────────
       ANTHROPIC_API_KEY  +  at least one Zabbix instance + token
                          ────────
                               │
                               ▼
                          works in CLI
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   +SLACK creds            +URL signing           +HOSTBILL
   +channel allowlist       key                    creds
        │                      │                      │
        ▼                      ▼                      ▼
   Slack adapter           Zabbix UI              memory.find_resolved_tickets
   /slack/events           right-click            falls back to "not configured"
                           /investigate           when absent
        │                      │                      │
        └──────────────────────┴──────────────────────┘
                          OPTIONAL
                          ────────
        Each adapter is independently mountable; missing config
        means the route is simply not registered. Health check at
        /healthz works regardless.
```

---

## Roadmap pointer

Current shipped: v0.2 → v0.5 (CLI, Slack, Zabbix UI, memory + HostBill
lookup). See README "Roadmap" for v0.6 (forecasting), v0.7 (admin UI),
v1.0 (GA), v1.1 (HostBill ticket triage), v1.2 (auto-mode).
