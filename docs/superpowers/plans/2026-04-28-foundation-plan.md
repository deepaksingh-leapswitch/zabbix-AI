# Zabbix RCA AI — Foundation (v0.1 + v0.2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational `zabbix-ai` Python service: config loader, multi-instance Zabbix client, SQLite state + audit log, read-only tool registry (~15 tools), Claude tool-use orchestrator with prompt caching, and a CLI adapter that runs end-to-end investigations from a single command. No Slack, no Zabbix UI, no admin UI yet — those are Plans 2-6.

**Architecture:** FastAPI app skeleton + uvicorn for future HTTP adapters; pydantic-based config; aiosqlite for state; httpx for Zabbix JSON-RPC; Anthropic SDK for Claude with explicit `cache_control` blocks; argparse CLI as the v0.2 entrypoint. Read-only tool registry is the trust boundary — every Claude tool call dispatches through a Python allowlist that enforces argument validation before any external API call.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, pydantic v2, aiosqlite, httpx, anthropic, pytest, pytest-asyncio, respx (HTTP mocking).

---

## File Structure (decomposition lock-in)

```
zabbix_ai/
  __init__.py            version constant
  __main__.py            python -m zabbix_ai → CLI dispatch
  app.py                 FastAPI factory (skeleton; routes added in later plans)
  config.py              pydantic Settings; loads /etc/zabbix-ai/config.yaml + env
  audit.py               append-only audit log helpers
  memory.py              SQLite connection + migration runner; lightweight in v0.2
  orchestrator.py        Claude tool-use loop (the heart)
  prompts.py             system prompt string + cache-control markers
  tools/
    __init__.py          TOOL_REGISTRY dict, register(), dispatch(); ALLOWED list
    zabbix.py            zabbix.* read tools
    diag.py              diag.* read tools (calls Zabbix client task.create + history.get)
    lookup.py            lookup.* (host_by_domain, host_by_ip)
  clients/
    __init__.py
    zabbix.py            multi-instance JSON-RPC client
    claude.py            Anthropic SDK wrapper, prompt-cache helpers
  adapters/
    __init__.py
    cli.py               argparse CLI — investigate, diag, list-instances
  renderers/
    __init__.py
    text.py              plain-text renderer (CLI output)

migrations/
  001_initial.sql        all v0.2 tables

deploy/
  zabbix-agent/diag.conf zabbix agent UserParameters file
  systemd/zabbix-ai.service systemd unit (referenced; not yet activated in v0.2)

tests/
  conftest.py            shared fixtures (tmp config, fake zabbix server)
  unit/
    test_config.py
    test_audit.py
    test_memory.py
    test_zabbix_client.py
    test_tools_registry.py
    test_tools_zabbix.py
    test_tools_diag.py
    test_tools_lookup.py
    test_claude_client.py
    test_orchestrator.py
  integration/
    test_cli_e2e.py

config.example.yaml
.env.example
pyproject.toml
README.md (expand existing)
```

Each file has **one** responsibility. Tools are split by namespace, not lumped into one module. Tests sit beside what they test. The orchestrator never imports a tool module directly — it dispatches through `tools.dispatch()` which is the only place the allowlist is enforced.

---

## Task 1 — Project scaffolding (pyproject, layout, lockfile)

**Files:**
- Create: `pyproject.toml`
- Create: `zabbix_ai/__init__.py`
- Create: `zabbix_ai/__main__.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/integration/__init__.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "zabbix-ai"
version = "0.2.0"
description = "AI-assisted root-cause analysis for Zabbix monitoring"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "aiosqlite>=0.20",
    "httpx>=0.28",
    "anthropic>=0.39",
    "pyyaml>=6.0",
    "structlog>=24.4",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "respx>=0.21",
    "ruff>=0.7",
    "mypy>=1.13",
]

[project.scripts]
zabbix-ai = "zabbix_ai.adapters.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["zabbix_ai"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF"]

[tool.mypy]
python_version = "3.12"
strict = true
```

- [ ] **Step 2: Write `zabbix_ai/__init__.py`**

```python
__version__ = "0.2.0"
```

- [ ] **Step 3: Write `zabbix_ai/__main__.py`**

```python
from zabbix_ai.adapters.cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Touch test packages**

```bash
touch tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py
```

- [ ] **Step 5: Install deps in venv and confirm import**

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
python -c "import zabbix_ai; print(zabbix_ai.__version__)"
```

Expected: `0.2.0`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml zabbix_ai/__init__.py zabbix_ai/__main__.py tests/
git commit -m "chore: project scaffolding and dependencies"
```

---

## Task 2 — Config loader (pydantic Settings)

**Files:**
- Create: `zabbix_ai/config.py`
- Create: `config.example.yaml`
- Create: `.env.example`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config.py
import pytest
from zabbix_ai.config import Settings, ZabbixInstance, load_settings

def test_load_settings_reads_yaml_and_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://monitoring.leapswitch.com
    token_env: MONITORING_TOKEN
  - name: dcmonitoring
    url: https://dcmonitoring.leapswitch.com
    token_env: DCMON_TOKEN
sqlite_path: /tmp/state.db
log_level: INFO
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("MONITORING_TOKEN", "tok-m")
    monkeypatch.setenv("DCMON_TOKEN", "tok-d")

    s = load_settings(cfg)

    assert s.anthropic_api_key == "sk-ant-test"
    assert len(s.zabbix_instances) == 2
    assert s.zabbix_instances[0].token == "tok-m"
    assert s.zabbix_instances[1].url == "https://dcmonitoring.leapswitch.com"
    assert s.sqlite_path == "/tmp/state.db"

def test_missing_anthropic_key_raises(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("zabbix_instances: []\nsqlite_path: /tmp/x\n")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        load_settings(cfg)

def test_missing_zabbix_token_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x
    token_env: NONEXISTENT_TOKEN
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(ValueError, match="NONEXISTENT_TOKEN"):
        load_settings(cfg)
```

- [ ] **Step 2: Run — expect ImportError**

```bash
pytest tests/unit/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'zabbix_ai.config'`

- [ ] **Step 3: Implement `zabbix_ai/config.py`**

```python
from __future__ import annotations
import os
from pathlib import Path
import yaml
from pydantic import BaseModel, Field

class ZabbixInstance(BaseModel):
    name: str
    url: str
    token_env: str
    token: str = ""

class Settings(BaseModel):
    zabbix_instances: list[ZabbixInstance] = Field(default_factory=list)
    sqlite_path: str = "/var/lib/zabbix-ai/state.db"
    log_level: str = "INFO"
    anthropic_api_key: str = ""
    default_model: str = "claude-sonnet-4-6"
    summary_model: str = "claude-haiku-4-5-20251001"
    max_tool_calls: int = 8
    max_input_tokens: int = 50_000
    max_output_tokens: int = 10_000

def load_settings(config_path: Path | str) -> Settings:
    raw = yaml.safe_load(Path(config_path).read_text()) or {}
    s = Settings(**raw)
    s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not s.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment")
    for inst in s.zabbix_instances:
        tok = os.environ.get(inst.token_env)
        if not tok:
            raise ValueError(f"{inst.token_env} not set in environment")
        inst.token = tok
    return s
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/unit/test_config.py -v
```

- [ ] **Step 5: Write `config.example.yaml`**

```yaml
# /etc/zabbix-ai/config.yaml
zabbix_instances:
  - name: monitoring
    url: https://monitoring.leapswitch.com
    token_env: ZABBIX_TOKEN_MONITORING
  - name: dcmonitoring
    url: https://dcmonitoring.leapswitch.com
    token_env: ZABBIX_TOKEN_DCMONITORING
  - name: strads
    url: https://monitoring.stradsolutions.com
    token_env: ZABBIX_TOKEN_STRADS

sqlite_path: /var/lib/zabbix-ai/state.db
log_level: INFO

default_model: claude-sonnet-4-6
summary_model: claude-haiku-4-5-20251001
max_tool_calls: 8
```

- [ ] **Step 6: Write `.env.example`**

```
ANTHROPIC_API_KEY=sk-ant-...
ZABBIX_TOKEN_MONITORING=...
ZABBIX_TOKEN_DCMONITORING=...
ZABBIX_TOKEN_STRADS=...
```

- [ ] **Step 7: Commit**

```bash
git add zabbix_ai/config.py config.example.yaml .env.example tests/unit/test_config.py
git commit -m "feat(config): pydantic settings loader for yaml + env"
```

---

## Task 3 — SQLite migrations + memory module

**Files:**
- Create: `migrations/001_initial.sql`
- Create: `zabbix_ai/memory.py`
- Test: `tests/unit/test_memory.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_memory.py
import pytest
from pathlib import Path
from zabbix_ai.memory import Memory

@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()

async def test_schema_created(memory):
    rows = await memory.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    names = {r[0] for r in rows}
    assert {"investigations", "host_facts", "patterns",
            "ticket_resolutions", "audit_log", "schema_version"} <= names

async def test_migrations_idempotent(memory):
    await memory.run_migrations(Path("migrations"))
    rows = await memory.fetchall("SELECT version FROM schema_version")
    assert rows == [(1,)]
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

```bash
pytest tests/unit/test_memory.py -v
```

- [ ] **Step 3: Write `migrations/001_initial.sql`**

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    instance TEXT,
    eventid INTEGER,
    ticket_id INTEGER,
    customer_id INTEGER,
    hostid INTEGER,
    hostname TEXT,
    started_at TEXT NOT NULL,
    duration_ms INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    model TEXT,
    summary TEXT,
    root_cause TEXT,
    suggested_actions TEXT,
    confidence TEXT,
    pattern_signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_pattern ON investigations(pattern_signature);
CREATE INDEX IF NOT EXISTS idx_inv_host ON investigations(hostid);

CREATE TABLE IF NOT EXISTS host_facts (
    hostid INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    source_investigation_id INTEGER,
    learned_at TEXT NOT NULL,
    PRIMARY KEY (hostid, key)
);

CREATE TABLE IF NOT EXISTS patterns (
    signature TEXT PRIMARY KEY,
    first_seen TEXT,
    last_seen TEXT,
    occurrences INTEGER NOT NULL DEFAULT 0,
    typical_root_cause TEXT,
    typical_fix TEXT,
    confidence_score REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ticket_resolutions (
    ticket_id INTEGER PRIMARY KEY,
    alert_pattern TEXT,
    resolution_text TEXT,
    customer_id INTEGER,
    hostname TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticket_pattern ON ticket_resolutions(alert_pattern);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    investigation_id INTEGER,
    event_type TEXT NOT NULL,
    tool_name TEXT,
    tool_input TEXT,
    tool_output TEXT,
    user TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_inv ON audit_log(investigation_id);

INSERT OR IGNORE INTO schema_version(version) VALUES (1);
```

- [ ] **Step 4: Implement `zabbix_ai/memory.py`**

```python
from __future__ import annotations
import re
from pathlib import Path
import aiosqlite

class Memory:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def run_migrations(self, migrations_dir: Path) -> None:
        assert self._conn
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        async with self._conn.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        current = row[0] or 0
        files = sorted(migrations_dir.glob("*.sql"))
        for f in files:
            m = re.match(r"(\d+)_", f.name)
            if not m:
                continue
            v = int(m.group(1))
            if v <= current:
                continue
            await self._conn.executescript(f.read_text())
        await self._conn.commit()

    async def execute(self, sql: str, params: tuple = ()) -> None:
        assert self._conn
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
pytest tests/unit/test_memory.py -v
```

- [ ] **Step 6: Commit**

```bash
git add migrations/001_initial.sql zabbix_ai/memory.py tests/unit/test_memory.py
git commit -m "feat(memory): SQLite schema and migration runner"
```

---

## Task 4 — Audit log helpers

**Files:**
- Create: `zabbix_ai/audit.py`
- Test: `tests/unit/test_audit.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_audit.py
import json
from pathlib import Path
import pytest
from zabbix_ai.memory import Memory
from zabbix_ai.audit import AuditLog

@pytest.fixture
async def audit(tmp_path):
    m = Memory(tmp_path / "a.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield AuditLog(m)
    await m.close()

async def test_log_start_returns_investigation_id(audit):
    inv_id = await audit.log_start(source="cli", instance="monitoring", eventid=99)
    assert inv_id > 0

async def test_log_tool_records_call(audit):
    inv_id = await audit.log_start(source="cli")
    await audit.log_tool(inv_id, "diag.df", {"hostid": 1}, "Filesystem ...")
    rows = await audit.memory.fetchall(
        "SELECT event_type, tool_name FROM audit_log WHERE investigation_id=?", (inv_id,)
    )
    assert ("tool_call", "diag.df") in rows

async def test_log_end_marks_complete(audit):
    inv_id = await audit.log_start(source="cli")
    await audit.log_end(inv_id, summary="ok", duration_ms=1234)
    row = await audit.memory.fetchone(
        "SELECT summary, duration_ms FROM investigations WHERE id=?", (inv_id,)
    )
    assert row == ("ok", 1234)
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

- [ ] **Step 3: Implement `zabbix_ai/audit.py`**

```python
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any
from zabbix_ai.memory import Memory

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class AuditLog:
    def __init__(self, memory: Memory):
        self.memory = memory

    async def log_start(
        self, *, source: str, instance: str | None = None,
        eventid: int | None = None, ticket_id: int | None = None,
        customer_id: int | None = None, hostid: int | None = None,
        hostname: str | None = None, model: str | None = None,
    ) -> int:
        await self.memory.execute(
            """INSERT INTO investigations
               (source, instance, eventid, ticket_id, customer_id,
                hostid, hostname, started_at, model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, instance, eventid, ticket_id, customer_id,
             hostid, hostname, _now(), model),
        )
        row = await self.memory.fetchone("SELECT last_insert_rowid()")
        inv_id = int(row[0]) if row else 0
        await self.memory.execute(
            "INSERT INTO audit_log (ts, investigation_id, event_type, source) "
            "VALUES (?, ?, 'start', ?)",
            (_now(), inv_id, source),
        )
        return inv_id

    async def log_tool(
        self, inv_id: int, tool_name: str, tool_input: dict, tool_output: Any
    ) -> None:
        await self.memory.execute(
            """INSERT INTO audit_log
               (ts, investigation_id, event_type, tool_name, tool_input, tool_output)
               VALUES (?, ?, 'tool_call', ?, ?, ?)""",
            (_now(), inv_id, tool_name,
             json.dumps(tool_input, default=str),
             str(tool_output)[:8000]),
        )

    async def log_error(self, inv_id: int, message: str) -> None:
        await self.memory.execute(
            """INSERT INTO audit_log
               (ts, investigation_id, event_type, tool_output)
               VALUES (?, ?, 'error', ?)""",
            (_now(), inv_id, message[:8000]),
        )

    async def log_end(
        self, inv_id: int, *, summary: str = "", root_cause: str = "",
        suggested_actions: str = "", confidence: str = "",
        pattern_signature: str = "", duration_ms: int = 0,
        tokens_in: int = 0, tokens_out: int = 0,
    ) -> None:
        await self.memory.execute(
            """UPDATE investigations
               SET summary=?, root_cause=?, suggested_actions=?, confidence=?,
                   pattern_signature=?, duration_ms=?, tokens_in=?, tokens_out=?
               WHERE id=?""",
            (summary, root_cause, suggested_actions, confidence,
             pattern_signature, duration_ms, tokens_in, tokens_out, inv_id),
        )
        await self.memory.execute(
            "INSERT INTO audit_log (ts, investigation_id, event_type) "
            "VALUES (?, ?, 'end')",
            (_now(), inv_id),
        )
```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/audit.py tests/unit/test_audit.py
git commit -m "feat(audit): append-only investigation + tool-call audit log"
```

---

## Task 5 — Zabbix JSON-RPC client (multi-instance)

**Files:**
- Create: `zabbix_ai/clients/__init__.py`
- Create: `zabbix_ai/clients/zabbix.py`
- Test: `tests/unit/test_zabbix_client.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_zabbix_client.py
import pytest
import respx
from httpx import Response
from zabbix_ai.clients.zabbix import ZabbixClient, ZabbixError

@pytest.fixture
def client():
    return ZabbixClient(name="monitoring", url="https://zbx.test", token="tok")

@respx.mock
async def test_call_sends_jsonrpc_with_auth(client):
    route = respx.post("https://zbx.test/api_jsonrpc.php").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": [], "id": 1})
    )
    await client.call("host.get", {"output": ["hostid"]})
    assert route.called
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    body = req.read().decode()
    assert '"method":"host.get"' in body or '"method": "host.get"' in body

@respx.mock
async def test_call_raises_on_error(client):
    respx.post("https://zbx.test/api_jsonrpc.php").mock(
        return_value=Response(200, json={"jsonrpc": "2.0",
                                         "error": {"code": -32602, "message": "Bad",
                                                   "data": "host not found"}, "id": 1})
    )
    with pytest.raises(ZabbixError, match="host not found"):
        await client.call("host.get", {})

@respx.mock
async def test_get_problem(client):
    respx.post("https://zbx.test/api_jsonrpc.php").mock(
        return_value=Response(200, json={"jsonrpc": "2.0",
                                         "result": [{"eventid": "42", "name": "test",
                                                     "severity": "4", "hosts": [{"hostid": "7"}]}],
                                         "id": 1})
    )
    p = await client.get_problem(42)
    assert p["eventid"] == "42"
    assert p["hosts"][0]["hostid"] == "7"
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

- [ ] **Step 3: Implement `zabbix_ai/clients/__init__.py`**

```python
```

- [ ] **Step 4: Implement `zabbix_ai/clients/zabbix.py`**

```python
from __future__ import annotations
import asyncio
from typing import Any
import httpx

class ZabbixError(Exception):
    pass

class ZabbixClient:
    def __init__(self, name: str, url: str, token: str, timeout: float = 15.0):
        self.name = name
        self.url = url.rstrip("/") + "/api_jsonrpc.php"
        self.token = token
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json-rpc",
                     "Authorization": f"Bearer {token}"},
        )
        self._id = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def call(self, method: str, params: dict | list[Any] | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "method": method,
                   "params": params or {}, "id": self._next_id()}
        r = await self._client.post(self.url, json=payload)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            raise ZabbixError(f"{err.get('message')}: {err.get('data')}")
        return data["result"]

    # ─── high-level helpers used by tools ───

    async def get_problem(self, eventid: int) -> dict:
        rows = await self.call("problem.get", {
            "eventids": [str(eventid)],
            "output": "extend",
            "selectHosts": ["hostid", "host", "name"],
            "selectTags": ["tag", "value"],
            "recent": "true",
        })
        if not rows:
            raise ZabbixError(f"no problem found for eventid={eventid}")
        return rows[0]

    async def get_open_problems(self, hostid: int | None = None,
                                hostgroupid: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"output": "extend", "recent": "true"}
        if hostid:
            params["hostids"] = [str(hostid)]
        if hostgroupid:
            params["groupids"] = [str(hostgroupid)]
        return await self.call("problem.get", params)

    async def get_host(self, hostid: int) -> dict:
        rows = await self.call("host.get", {
            "hostids": [str(hostid)],
            "output": "extend",
            "selectGroups": ["groupid", "name"],
            "selectInterfaces": ["ip", "dns", "type"],
            "selectInventory": "extend",
            "selectTags": ["tag", "value"],
        })
        if not rows:
            raise ZabbixError(f"no host found for hostid={hostid}")
        return rows[0]

    async def get_history(self, hostid: int, keys: list[str], range_seconds: int = 3600) -> dict:
        items = await self.call("item.get", {
            "hostids": [str(hostid)],
            "search": {"key_": keys},
            "searchByAny": True,
            "output": ["itemid", "key_", "value_type", "name"],
        })
        if not items:
            return {}
        import time
        time_from = int(time.time()) - range_seconds
        result: dict[str, list] = {}
        for item in items:
            history = await self.call("history.get", {
                "itemids": [item["itemid"]],
                "history": int(item["value_type"]),
                "time_from": time_from,
                "sortfield": "clock",
                "sortorder": "ASC",
                "limit": 200,
            })
            result[item["key_"]] = [{"clock": int(h["clock"]),
                                     "value": h["value"]} for h in history]
        return result

    async def get_item(self, hostid: int, key: str) -> dict | None:
        rows = await self.call("item.get", {
            "hostids": [str(hostid)],
            "filter": {"key_": key},
            "output": ["itemid", "key_", "value_type", "lastvalue", "lastclock"],
        })
        return rows[0] if rows else None

    async def task_create_check_now(self, itemid: str) -> None:
        await self.call("task.create", [{"type": 6,
                                          "request": {"itemid": str(itemid)}}])

    async def wait_for_fresh_value(self, itemid: str, after_clock: int,
                                   timeout: float = 15.0) -> str:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            rows = await self.call("item.get", {
                "itemids": [str(itemid)],
                "output": ["lastvalue", "lastclock"],
            })
            if rows and int(rows[0].get("lastclock") or 0) > after_clock:
                return rows[0]["lastvalue"]
            await asyncio.sleep(1.0)
        raise ZabbixError(f"timeout waiting for fresh value on item {itemid}")
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
pytest tests/unit/test_zabbix_client.py -v
```

- [ ] **Step 6: Commit**

```bash
git add zabbix_ai/clients/__init__.py zabbix_ai/clients/zabbix.py tests/unit/test_zabbix_client.py
git commit -m "feat(clients): multi-instance Zabbix JSON-RPC client"
```

---

## Task 6 — Tool registry + dispatcher

**Files:**
- Create: `zabbix_ai/tools/__init__.py`
- Test: `tests/unit/test_tools_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_registry.py
import pytest
from zabbix_ai.tools import register, dispatch, ALLOWED_TOOLS, claude_tool_definitions

def test_register_adds_tool():
    @register("test.echo", description="echo back",
              schema={"type": "object", "properties": {"x": {"type": "string"}},
                      "required": ["x"]})
    async def echo(*, x: str) -> str:
        return x
    assert "test.echo" in ALLOWED_TOOLS

async def test_dispatch_calls_registered_tool():
    @register("test.add", description="add",
              schema={"type": "object",
                      "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                      "required": ["a", "b"]})
    async def add(*, a: int, b: int) -> int:
        return a + b
    result = await dispatch("test.add", {"a": 2, "b": 3}, context={})
    assert result == 5

async def test_dispatch_unknown_raises():
    with pytest.raises(KeyError, match="not allowed"):
        await dispatch("evil.delete_everything", {}, context={})

def test_claude_tool_definitions_returns_list():
    defs = claude_tool_definitions()
    assert isinstance(defs, list)
    assert all("name" in d and "input_schema" in d for d in defs)
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

- [ ] **Step 3: Implement `zabbix_ai/tools/__init__.py`**

```python
from __future__ import annotations
from collections.abc import Awaitable, Callable
from typing import Any

ToolFunc = Callable[..., Awaitable[Any]]

ALLOWED_TOOLS: dict[str, ToolFunc] = {}
_TOOL_META: dict[str, dict[str, Any]] = {}

def register(name: str, *, description: str, schema: dict[str, Any]) -> Callable[[ToolFunc], ToolFunc]:
    def decorator(fn: ToolFunc) -> ToolFunc:
        ALLOWED_TOOLS[name] = fn
        _TOOL_META[name] = {"description": description, "input_schema": schema}
        return fn
    return decorator

async def dispatch(name: str, args: dict[str, Any], *, context: dict[str, Any]) -> Any:
    if name not in ALLOWED_TOOLS:
        raise KeyError(f"tool '{name}' not allowed")
    fn = ALLOWED_TOOLS[name]
    return await fn(**args, _ctx=context) if _accepts_ctx(fn) else await fn(**args)

def _accepts_ctx(fn: ToolFunc) -> bool:
    import inspect
    sig = inspect.signature(fn)
    return "_ctx" in sig.parameters

def claude_tool_definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": meta["description"], "input_schema": meta["input_schema"]}
        for name, meta in _TOOL_META.items()
    ]
```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/tools/__init__.py tests/unit/test_tools_registry.py
git commit -m "feat(tools): registry and dispatcher with Claude schema export"
```

---

## Task 7 — `zabbix.*` tool wrappers

**Files:**
- Create: `zabbix_ai/tools/zabbix.py`
- Test: `tests/unit/test_tools_zabbix.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_zabbix.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from zabbix_ai.tools.zabbix import register_tools
from zabbix_ai.tools import dispatch, ALLOWED_TOOLS

@pytest.fixture
def fake_client():
    c = MagicMock()
    c.get_problem = AsyncMock(return_value={"eventid": "42", "name": "disk full",
                                             "severity": "4",
                                             "hosts": [{"hostid": "7", "host": "web-1"}],
                                             "tags": []})
    c.get_open_problems = AsyncMock(return_value=[])
    c.get_host = AsyncMock(return_value={"hostid": "7", "host": "web-1",
                                          "groups": [{"groupid": "1", "name": "WebServers"}]})
    c.get_history = AsyncMock(return_value={"vfs.fs.size[/,pused]": [{"clock": 1, "value": "92"}]})
    return c

@pytest.fixture
def context(fake_client):
    return {"clients": {"monitoring": fake_client}}

async def test_get_problem_dispatch(context):
    register_tools()
    result = await dispatch("zabbix.get_problem",
                            {"eventid": 42, "instance": "monitoring"},
                            context=context)
    assert result["eventid"] == "42"

async def test_get_problem_unknown_instance_raises(context):
    register_tools()
    with pytest.raises(ValueError, match="unknown instance"):
        await dispatch("zabbix.get_problem",
                       {"eventid": 42, "instance": "nope"}, context=context)

async def test_get_host(context):
    register_tools()
    r = await dispatch("zabbix.get_host",
                       {"hostid": 7, "instance": "monitoring"}, context=context)
    assert r["host"] == "web-1"

async def test_get_history(context):
    register_tools()
    r = await dispatch("zabbix.get_history",
                       {"hostid": 7, "instance": "monitoring",
                        "keys": ["vfs.fs.size[/,pused]"], "range_seconds": 3600},
                       context=context)
    assert "vfs.fs.size[/,pused]" in r
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

- [ ] **Step 3: Implement `zabbix_ai/tools/zabbix.py`**

```python
from __future__ import annotations
from typing import Any
from zabbix_ai.tools import register

def _client(ctx: dict, instance: str):
    clients = ctx.get("clients") or {}
    if instance not in clients:
        raise ValueError(f"unknown instance '{instance}'")
    return clients[instance]

def register_tools() -> None:
    @register("zabbix.get_problem", description="Get a Zabbix problem by event id.",
              schema={"type": "object",
                      "properties": {
                          "eventid": {"type": "integer"},
                          "instance": {"type": "string"}},
                      "required": ["eventid", "instance"]})
    async def _get_problem(*, eventid: int, instance: str, _ctx: dict) -> dict:
        return await _client(_ctx, instance).get_problem(eventid)

    @register("zabbix.get_open_problems",
              description="List currently open problems for a host or hostgroup.",
              schema={"type": "object",
                      "properties": {
                          "instance": {"type": "string"},
                          "hostid": {"type": "integer"},
                          "hostgroupid": {"type": "integer"}},
                      "required": ["instance"]})
    async def _open(*, instance: str, hostid: int | None = None,
                    hostgroupid: int | None = None, _ctx: dict) -> list[dict]:
        return await _client(_ctx, instance).get_open_problems(hostid, hostgroupid)

    @register("zabbix.get_host", description="Get full host info including groups, interfaces, inventory.",
              schema={"type": "object",
                      "properties": {
                          "hostid": {"type": "integer"},
                          "instance": {"type": "string"}},
                      "required": ["hostid", "instance"]})
    async def _host(*, hostid: int, instance: str, _ctx: dict) -> dict:
        return await _client(_ctx, instance).get_host(hostid)

    @register("zabbix.get_history",
              description="Get historical metric values for given item keys on a host.",
              schema={"type": "object",
                      "properties": {
                          "hostid": {"type": "integer"},
                          "instance": {"type": "string"},
                          "keys": {"type": "array", "items": {"type": "string"}},
                          "range_seconds": {"type": "integer", "default": 3600}},
                      "required": ["hostid", "instance", "keys"]})
    async def _history(*, hostid: int, instance: str, keys: list[str],
                       range_seconds: int = 3600, _ctx: dict) -> dict:
        return await _client(_ctx, instance).get_history(hostid, keys, range_seconds)
```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/tools/zabbix.py tests/unit/test_tools_zabbix.py
git commit -m "feat(tools): zabbix.* read-only wrappers (problem, host, history)"
```

---

## Task 8 — `diag.*` tool wrappers

**Files:**
- Create: `zabbix_ai/tools/diag.py`
- Create: `deploy/zabbix-agent/diag.conf`
- Test: `tests/unit/test_tools_diag.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_diag.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from zabbix_ai.tools.diag import register_tools, ALLOWED_DIAG_KEYS
from zabbix_ai.tools import dispatch

@pytest.fixture
def fake_client():
    c = MagicMock()
    c.get_item = AsyncMock(return_value={"itemid": "100", "lastclock": "1000"})
    c.task_create_check_now = AsyncMock()
    c.wait_for_fresh_value = AsyncMock(return_value="Filesystem  Size  Use%  Mounted")
    return c

@pytest.fixture
def context(fake_client):
    return {"clients": {"monitoring": fake_client}}

async def test_diag_df_runs(context):
    register_tools()
    out = await dispatch("diag.df", {"hostid": 7, "instance": "monitoring"},
                        context=context)
    assert "Filesystem" in out

async def test_diag_systemctl_status_arg(context):
    register_tools()
    out = await dispatch("diag.systemctl_status",
                        {"hostid": 7, "instance": "monitoring", "unit": "mysql"},
                        context=context)
    assert out

async def test_diag_unknown_command_rejected(context):
    register_tools()
    # not in registry — dispatch raises before any client call
    with pytest.raises(KeyError):
        await dispatch("diag.rm_rf", {"hostid": 7, "instance": "monitoring"},
                       context=context)

def test_allowlist_complete():
    assert "diag.df" in ALLOWED_DIAG_KEYS
    assert "diag.mysql_processlist" in ALLOWED_DIAG_KEYS
    assert "system.run" not in ALLOWED_DIAG_KEYS
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

- [ ] **Step 3: Implement `zabbix_ai/tools/diag.py`**

```python
from __future__ import annotations
from typing import Any
from zabbix_ai.tools import register

ALLOWED_DIAG_KEYS = {
    "diag.df", "diag.free", "diag.uptime", "diag.top",
    "diag.dmesg_tail", "diag.journal_tail", "diag.systemctl_status",
    "diag.ss_listen", "diag.ps_aux", "diag.iostat",
    "diag.mysql_status", "diag.mysql_processlist", "diag.apache_status",
}

def _client(ctx: dict, instance: str):
    clients = ctx.get("clients") or {}
    if instance not in clients:
        raise ValueError(f"unknown instance '{instance}'")
    return clients[instance]

async def _run_diag(client, hostid: int, key: str) -> str:
    if key not in ALLOWED_DIAG_KEYS:
        raise ValueError(f"diagnostic '{key}' not allowed")
    item = await client.get_item(hostid, key)
    if not item:
        raise ValueError(f"agent on host {hostid} does not expose {key}")
    after = int(item.get("lastclock") or 0)
    await client.task_create_check_now(item["itemid"])
    return await client.wait_for_fresh_value(item["itemid"], after_clock=after, timeout=15)

_HOST_INST_SCHEMA = {
    "type": "object",
    "properties": {"hostid": {"type": "integer"},
                   "instance": {"type": "string"}},
    "required": ["hostid", "instance"],
}

def _register_simple(name: str, key: str, description: str) -> None:
    @register(name, description=description, schema=_HOST_INST_SCHEMA)
    async def _impl(*, hostid: int, instance: str, _ctx: dict) -> str:
        return await _run_diag(_client(_ctx, instance), hostid, key)
    _impl.__name__ = f"_{name.replace('.', '_')}"

def register_tools() -> None:
    _register_simple("diag.df", "diag.df", "Disk usage on the host (df -hP).")
    _register_simple("diag.free", "diag.free", "Memory usage (free -m).")
    _register_simple("diag.uptime", "diag.uptime", "System uptime and load.")
    _register_simple("diag.top", "diag.top", "Top CPU/memory processes (top -bn1).")
    _register_simple("diag.dmesg_tail", "diag.dmesg_tail", "Last 100 lines of kernel ring buffer.")
    _register_simple("diag.ss_listen", "diag.ss_listen", "Listening sockets (ss -tunap).")
    _register_simple("diag.ps_aux", "diag.ps_aux", "Process list sorted by memory.")
    _register_simple("diag.iostat", "diag.iostat", "I/O statistics (iostat -xz 1 2).")
    _register_simple("diag.mysql_status", "diag.mysql_status", "MySQL server status summary.")
    _register_simple("diag.mysql_processlist", "diag.mysql_processlist",
                     "MySQL SHOW FULL PROCESSLIST.")
    _register_simple("diag.apache_status", "diag.apache_status", "Apache server-status output.")

    @register("diag.systemctl_status",
              description="systemctl status <unit> output (read-only).",
              schema={"type": "object",
                      "properties": {
                          "hostid": {"type": "integer"},
                          "instance": {"type": "string"},
                          "unit": {"type": "string"}},
                      "required": ["hostid", "instance", "unit"]})
    async def _systemctl(*, hostid: int, instance: str, unit: str, _ctx: dict) -> str:
        if not unit.replace("-", "").replace(".", "").replace("_", "").replace("@", "").isalnum():
            raise ValueError("invalid unit name")
        client = _client(_ctx, instance)
        item = await client.get_item(hostid, f"diag.systemctl_status[{unit}]")
        if not item:
            raise ValueError(f"agent does not expose diag.systemctl_status[{unit}]")
        after = int(item.get("lastclock") or 0)
        await client.task_create_check_now(item["itemid"])
        return await client.wait_for_fresh_value(item["itemid"], after_clock=after, timeout=15)

    @register("diag.journal_tail",
              description="Last N lines of journalctl for a unit.",
              schema={"type": "object",
                      "properties": {
                          "hostid": {"type": "integer"},
                          "instance": {"type": "string"},
                          "lines": {"type": "integer", "default": 200}},
                      "required": ["hostid", "instance"]})
    async def _journal(*, hostid: int, instance: str, lines: int = 200, _ctx: dict) -> str:
        if not 1 <= lines <= 1000:
            raise ValueError("lines must be between 1 and 1000")
        client = _client(_ctx, instance)
        item = await client.get_item(hostid, f"diag.journal_tail[{lines}]")
        if not item:
            raise ValueError(f"agent does not expose diag.journal_tail[{lines}]")
        after = int(item.get("lastclock") or 0)
        await client.task_create_check_now(item["itemid"])
        return await client.wait_for_fresh_value(item["itemid"], after_clock=after, timeout=15)
```

- [ ] **Step 4: Write `deploy/zabbix-agent/diag.conf`**

```conf
# Zabbix RCA AI — read-only diagnostic UserParameters
# Deploy to: /etc/zabbix/zabbix_agentd.d/diag.conf  (then: systemctl restart zabbix-agent)
# Agent runs as 'zabbix' user — no root.

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

- [ ] **Step 5: Run tests — expect PASS**

```bash
pytest tests/unit/test_tools_diag.py -v
```

- [ ] **Step 6: Commit**

```bash
git add zabbix_ai/tools/diag.py deploy/zabbix-agent/diag.conf tests/unit/test_tools_diag.py
git commit -m "feat(tools): diag.* read-only wrappers + agent UserParameter file"
```

---

## Task 9 — `lookup.*` tool wrappers

**Files:**
- Create: `zabbix_ai/tools/lookup.py`
- Test: `tests/unit/test_tools_lookup.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_lookup.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from zabbix_ai.tools.lookup import register_tools
from zabbix_ai.tools import dispatch

@pytest.fixture
def context():
    c = MagicMock()
    c.call = AsyncMock(side_effect=[
        # host.get by tag
        [{"hostid": "7", "host": "web-1"}],
        # host.get by interface ip
        [{"hostid": "9", "host": "db-1"}],
    ])
    return {"clients": {"monitoring": c}}

async def test_host_by_domain(context):
    register_tools()
    r = await dispatch("lookup.host_by_domain",
                       {"domain": "shop.example.com", "instance": "monitoring"},
                       context=context)
    assert r["host"] == "web-1"

async def test_host_by_ip(context):
    register_tools()
    r = await dispatch("lookup.host_by_ip",
                       {"ip": "10.0.0.5", "instance": "monitoring"},
                       context=context)
    assert r["host"] == "db-1"
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

- [ ] **Step 3: Implement `zabbix_ai/tools/lookup.py`**

```python
from __future__ import annotations
from zabbix_ai.tools import register

def _client(ctx: dict, instance: str):
    clients = ctx.get("clients") or {}
    if instance not in clients:
        raise ValueError(f"unknown instance '{instance}'")
    return clients[instance]

def register_tools() -> None:
    @register("lookup.host_by_domain",
              description="Find a Zabbix host by served domain (uses host tags).",
              schema={"type": "object",
                      "properties": {
                          "domain": {"type": "string"},
                          "instance": {"type": "string"}},
                      "required": ["domain", "instance"]})
    async def _by_domain(*, domain: str, instance: str, _ctx: dict) -> dict | None:
        client = _client(_ctx, instance)
        rows = await client.call("host.get", {
            "output": ["hostid", "host", "name"],
            "tags": [{"tag": "domain", "value": domain, "operator": "0"}],
        })
        return rows[0] if rows else None

    @register("lookup.host_by_ip",
              description="Find a Zabbix host by primary interface IP.",
              schema={"type": "object",
                      "properties": {
                          "ip": {"type": "string"},
                          "instance": {"type": "string"}},
                      "required": ["ip", "instance"]})
    async def _by_ip(*, ip: str, instance: str, _ctx: dict) -> dict | None:
        client = _client(_ctx, instance)
        rows = await client.call("host.get", {
            "output": ["hostid", "host", "name"],
            "filter": {"ip": ip},
        })
        return rows[0] if rows else None
```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/tools/lookup.py tests/unit/test_tools_lookup.py
git commit -m "feat(tools): lookup.* host_by_domain and host_by_ip"
```

---

## Task 10 — Claude client with prompt caching

**Files:**
- Create: `zabbix_ai/clients/claude.py`
- Create: `zabbix_ai/prompts.py`
- Test: `tests/unit/test_claude_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_claude_client.py
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from zabbix_ai.clients.claude import ClaudeClient
from zabbix_ai.prompts import SYSTEM_PROMPT, build_cached_system_blocks

def test_system_prompt_includes_safety_rules():
    assert "read-only" in SYSTEM_PROMPT.lower()
    assert "no shell" in SYSTEM_PROMPT.lower() or "never get a shell" in SYSTEM_PROMPT.lower()

def test_cached_blocks_have_cache_control():
    tools = [{"name": "x", "description": "x", "input_schema": {"type": "object"}}]
    inv = "host inventory snapshot"
    blocks = build_cached_system_blocks(SYSTEM_PROMPT, tools, inv)
    assert blocks[0]["type"] == "text"
    # the last system block carries cache_control to break the cache after inventory
    assert any(b.get("cache_control") == {"type": "ephemeral"} for b in blocks)

async def test_claude_client_calls_messages_create_with_cache():
    fake_resp = MagicMock(stop_reason="end_turn", content=[],
                          usage=MagicMock(input_tokens=10, output_tokens=5,
                                          cache_creation_input_tokens=0,
                                          cache_read_input_tokens=0))
    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as MockA:
        MockA.return_value.messages.create = AsyncMock(return_value=fake_resp)
        c = ClaudeClient(api_key="sk-ant-test")
        resp = await c.create(model="claude-sonnet-4-6",
                              system=[{"type": "text", "text": "sys",
                                       "cache_control": {"type": "ephemeral"}}],
                              tools=[], messages=[{"role": "user", "content": "hi"}],
                              max_tokens=100)
        assert resp.stop_reason == "end_turn"
        MockA.return_value.messages.create.assert_awaited_once()
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

- [ ] **Step 3: Implement `zabbix_ai/prompts.py`**

```python
from __future__ import annotations
from typing import Any

SYSTEM_PROMPT = """\
You are a NOC engineer for Leapswitch performing root-cause analysis on Zabbix alerts and customer tickets.

Rules:
- All your tools are read-only. You cannot delete, restart, or change anything.
- You never get a shell. Diagnostics run only through the fixed `diag.*` allowlist.
- When uncertain, prefer to gather one more diagnostic before concluding.
- Stop calling tools as soon as you have enough evidence.

Output schema (final assistant message — JSON-like, plain text accepted):
- root_cause: one paragraph
- evidence: bullet list of facts you actually observed via tools
- suggested_actions: numbered list of read-only or human-approved next steps
- confidence: high | medium | low

Memory tools surface past investigations and learned host facts; use them when an
alert pattern looks familiar. Avoid hallucinating facts that no tool returned.
"""

def build_cached_system_blocks(system_prompt: str, tools: list[dict[str, Any]],
                               host_inventory_summary: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system_prompt},
    ]
    if host_inventory_summary:
        blocks.append({"type": "text",
                       "text": "Host inventory snapshot (refreshed hourly):\n"
                               + host_inventory_summary,
                       "cache_control": {"type": "ephemeral"}})
    else:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks
```

- [ ] **Step 4: Implement `zabbix_ai/clients/claude.py`**

```python
from __future__ import annotations
from typing import Any
from anthropic import AsyncAnthropic

class ClaudeClient:
    def __init__(self, api_key: str):
        self._client = AsyncAnthropic(api_key=api_key)

    async def create(self, *, model: str, system: list[dict[str, Any]],
                     tools: list[dict[str, Any]], messages: list[dict[str, Any]],
                     max_tokens: int = 2048) -> Any:
        return await self._client.messages.create(
            model=model, system=system, tools=tools,
            messages=messages, max_tokens=max_tokens,
        )
```

- [ ] **Step 5: Run tests — expect PASS**

- [ ] **Step 6: Commit**

```bash
git add zabbix_ai/clients/claude.py zabbix_ai/prompts.py tests/unit/test_claude_client.py
git commit -m "feat(claude): client wrapper and prompt-cached system blocks"
```

---

## Task 11 — Orchestrator (Claude tool-use loop)

**Files:**
- Create: `zabbix_ai/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_orchestrator.py
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from zabbix_ai.orchestrator import Orchestrator, InvestigationContext
from zabbix_ai.memory import Memory
from zabbix_ai.audit import AuditLog
from zabbix_ai.tools import register

class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)

def _resp(stop_reason: str, blocks: list, in_t: int = 100, out_t: int = 50):
    return MagicMock(
        stop_reason=stop_reason, content=blocks,
        usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                        cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )

@pytest.fixture
async def setup(tmp_path):
    @register("test.echo", description="echo",
              schema={"type": "object", "properties": {"msg": {"type": "string"}},
                      "required": ["msg"]})
    async def echo(*, msg: str) -> str:
        return f"got:{msg}"

    m = Memory(tmp_path / "o.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    audit = AuditLog(m)
    yield m, audit
    await m.close()

async def test_orchestrator_runs_tool_then_stops(setup):
    m, audit = setup
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id="t1",
                                  name="test.echo", input={"msg": "hello"})]),
        _resp("end_turn", [_Block(type="text", text="done")]),
    ])
    orch = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                        max_tool_calls=8, clients={})
    ctx = InvestigationContext(source="cli", question="what is the test?")
    result = await orch.investigate(ctx)
    assert result.summary == "done"
    assert claude.create.await_count == 2

async def test_orchestrator_unknown_tool_continues(setup):
    m, audit = setup
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id="t1",
                                  name="evil.delete", input={})]),
        _resp("end_turn", [_Block(type="text", text="bailed")]),
    ])
    orch = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                        max_tool_calls=8, clients={})
    result = await orch.investigate(InvestigationContext(source="cli", question="?"))
    assert result.summary == "bailed"

async def test_orchestrator_caps_tool_calls(setup):
    m, audit = setup
    claude = MagicMock()
    # always asks for a tool — orchestrator must stop at max_tool_calls
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id=f"t{i}",
                                  name="test.echo", input={"msg": str(i)})])
        for i in range(10)
    ] + [_resp("end_turn", [_Block(type="text", text="capped")])])
    orch = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                        max_tool_calls=3, clients={})
    result = await orch.investigate(InvestigationContext(source="cli", question="?"))
    # 3 tool turns + 1 forced final summary = 4 calls max
    assert claude.create.await_count <= 4
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

- [ ] **Step 3: Implement `zabbix_ai/orchestrator.py`**

```python
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any
from zabbix_ai.audit import AuditLog
from zabbix_ai.tools import dispatch, claude_tool_definitions
from zabbix_ai.prompts import SYSTEM_PROMPT, build_cached_system_blocks

@dataclass
class InvestigationContext:
    source: str
    question: str = ""
    instance: str | None = None
    eventid: int | None = None
    ticket_id: int | None = None
    customer_id: int | None = None
    hostid: int | None = None
    hostname: str | None = None
    host_inventory_summary: str = ""

@dataclass
class InvestigationResult:
    investigation_id: int
    summary: str
    tool_calls: int
    tokens_in: int
    tokens_out: int
    duration_ms: int
    transcript: list[dict[str, Any]] = field(default_factory=list)

class Orchestrator:
    def __init__(self, *, claude, audit: AuditLog, model: str, summary_model: str,
                 max_tool_calls: int, clients: dict[str, Any]):
        self.claude = claude
        self.audit = audit
        self.model = model
        self.summary_model = summary_model
        self.max_tool_calls = max_tool_calls
        self.clients = clients

    async def investigate(self, ctx: InvestigationContext) -> InvestigationResult:
        start = time.monotonic()
        inv_id = await self.audit.log_start(
            source=ctx.source, instance=ctx.instance, eventid=ctx.eventid,
            ticket_id=ctx.ticket_id, customer_id=ctx.customer_id,
            hostid=ctx.hostid, hostname=ctx.hostname, model=self.model,
        )
        system_blocks = build_cached_system_blocks(
            SYSTEM_PROMPT, claude_tool_definitions(), ctx.host_inventory_summary,
        )
        user_prompt = self._render_user_prompt(ctx)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        tool_calls = 0
        tokens_in = tokens_out = 0
        final_text = ""

        while True:
            resp = await self.claude.create(
                model=self.model, system=system_blocks,
                tools=claude_tool_definitions(),
                messages=messages, max_tokens=2048,
            )
            tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
            tokens_out += getattr(resp.usage, "output_tokens", 0) or 0

            if resp.stop_reason == "end_turn":
                final_text = self._extract_text(resp.content)
                break

            if tool_calls >= self.max_tool_calls:
                # ask Claude for a final summary using what it has
                messages.append({"role": "user",
                                 "content": "Tool budget exhausted. Produce final summary now."})
                continue

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls += 1
                try:
                    output = await dispatch(block.name, block.input or {},
                                             context={"clients": self.clients,
                                                      "investigation_id": inv_id})
                    await self.audit.log_tool(inv_id, block.name, block.input or {}, output)
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": str(output)[:8000]})
                except Exception as e:
                    await self.audit.log_tool(inv_id, block.name, block.input or {},
                                              f"ERROR: {e}")
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": f"ERROR: {e}",
                                         "is_error": True})
            messages.append({"role": "user", "content": tool_results})

        duration_ms = int((time.monotonic() - start) * 1000)
        await self.audit.log_end(
            inv_id, summary=final_text, duration_ms=duration_ms,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
        return InvestigationResult(
            investigation_id=inv_id, summary=final_text,
            tool_calls=tool_calls, tokens_in=tokens_in, tokens_out=tokens_out,
            duration_ms=duration_ms, transcript=messages,
        )

    @staticmethod
    def _render_user_prompt(ctx: InvestigationContext) -> str:
        parts = [f"Source: {ctx.source}"]
        if ctx.instance: parts.append(f"Zabbix instance: {ctx.instance}")
        if ctx.eventid: parts.append(f"Event id: {ctx.eventid}")
        if ctx.hostid: parts.append(f"Host id: {ctx.hostid} ({ctx.hostname or ''})")
        if ctx.ticket_id: parts.append(f"Ticket id: {ctx.ticket_id}")
        if ctx.question: parts.append(f"\nQuestion / context:\n{ctx.question}")
        parts.append("\nInvestigate using the provided tools and produce the final structured answer.")
        return "\n".join(parts)

    @staticmethod
    def _extract_text(content: list[Any]) -> str:
        out = []
        for b in content:
            t = getattr(b, "type", None)
            if t == "text":
                out.append(getattr(b, "text", ""))
        return "\n".join(out).strip()
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/unit/test_orchestrator.py -v
```

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat(orchestrator): Claude tool-use loop with hard caps and audit"
```

---

## Task 12 — CLI adapter

**Files:**
- Create: `zabbix_ai/adapters/__init__.py`
- Create: `zabbix_ai/adapters/cli.py`
- Create: `zabbix_ai/renderers/__init__.py`
- Create: `zabbix_ai/renderers/text.py`
- Test: `tests/integration/test_cli_e2e.py`

- [ ] **Step 1: Write `zabbix_ai/renderers/__init__.py`**

```python
```

- [ ] **Step 2: Write `zabbix_ai/renderers/text.py`**

```python
from __future__ import annotations
from zabbix_ai.orchestrator import InvestigationResult

def render(result: InvestigationResult) -> str:
    return (
        f"=== Investigation #{result.investigation_id} ===\n"
        f"Tool calls: {result.tool_calls}\n"
        f"Tokens: in={result.tokens_in} out={result.tokens_out}\n"
        f"Duration: {result.duration_ms} ms\n\n"
        f"{result.summary}\n"
    )
```

- [ ] **Step 3: Write `zabbix_ai/adapters/__init__.py`**

```python
```

- [ ] **Step 4: Write `zabbix_ai/adapters/cli.py`**

```python
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path
from zabbix_ai.config import load_settings
from zabbix_ai.memory import Memory
from zabbix_ai.audit import AuditLog
from zabbix_ai.clients.zabbix import ZabbixClient
from zabbix_ai.clients.claude import ClaudeClient
from zabbix_ai.tools import zabbix as tools_zabbix, diag as tools_diag, lookup as tools_lookup
from zabbix_ai.orchestrator import Orchestrator, InvestigationContext
from zabbix_ai.renderers.text import render

DEFAULT_CONFIG = "/etc/zabbix-ai/config.yaml"

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zabbix-ai")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("investigate", help="Run an AI investigation")
    inv.add_argument("--eventid", type=int)
    inv.add_argument("--hostid", type=int)
    inv.add_argument("--instance", required=True)
    inv.add_argument("--question", default="")
    inv.add_argument("--ticket-id", type=int)

    sub.add_parser("list-instances", help="Show configured Zabbix instances")
    return p

async def _run(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.config))

    if args.cmd == "list-instances":
        for inst in settings.zabbix_instances:
            print(f"{inst.name}\t{inst.url}")
        return 0

    # build clients
    clients: dict[str, ZabbixClient] = {}
    for inst in settings.zabbix_instances:
        clients[inst.name] = ZabbixClient(inst.name, inst.url, inst.token)

    # register tools
    tools_zabbix.register_tools()
    tools_diag.register_tools()
    tools_lookup.register_tools()

    # memory + audit
    Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    mem = Memory(settings.sqlite_path)
    await mem.connect()
    await mem.run_migrations(Path(__file__).resolve().parents[2] / "migrations")
    audit = AuditLog(mem)

    claude = ClaudeClient(api_key=settings.anthropic_api_key)
    orch = Orchestrator(
        claude=claude, audit=audit,
        model=settings.default_model, summary_model=settings.summary_model,
        max_tool_calls=settings.max_tool_calls, clients=clients,
    )

    if args.cmd == "investigate":
        ctx = InvestigationContext(
            source="cli", instance=args.instance,
            eventid=args.eventid, hostid=args.hostid,
            ticket_id=args.ticket_id, question=args.question,
        )
        result = await orch.investigate(ctx)
        print(render(result))
        for c in clients.values():
            await c.aclose()
        await mem.close()
        return 0
    return 1

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Write integration test (mocks Claude + Zabbix)**

```python
# tests/integration/test_cli_e2e.py
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from zabbix_ai.adapters.cli import _run, build_parser

class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)

def _resp(stop_reason, blocks, in_t=10, out_t=5):
    return MagicMock(stop_reason=stop_reason, content=blocks,
                     usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))

async def test_cli_investigate_end_to_end(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
zabbix_instances:
  - name: monitoring
    url: https://zbx.test
    token_env: ZBX_TOK
sqlite_path: {tmp_path / 'state.db'}
default_model: m
summary_model: h
max_tool_calls: 4
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ZBX_TOK", "tok")

    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as MockA:
        MockA.return_value.messages.create = AsyncMock(side_effect=[
            _resp("end_turn", [_Block(type="text",
                                      text="root_cause: tested\nconfidence: high")]),
        ])
        args = build_parser().parse_args([
            "--config", str(cfg),
            "investigate", "--instance", "monitoring", "--question", "is it ok?",
        ])
        rc = await _run(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Investigation #" in out
        assert "root_cause: tested" in out
```

- [ ] **Step 6: Run tests — expect PASS**

```bash
pytest tests/integration/test_cli_e2e.py -v
```

- [ ] **Step 7: Commit**

```bash
git add zabbix_ai/adapters/ zabbix_ai/renderers/ tests/integration/test_cli_e2e.py
git commit -m "feat(cli): investigate command with end-to-end orchestrator wiring"
```

---

## Task 13 — FastAPI app skeleton (for future plans)

**Files:**
- Create: `zabbix_ai/app.py`
- Create: `deploy/systemd/zabbix-ai.service`

- [ ] **Step 1: Write `zabbix_ai/app.py`**

```python
from __future__ import annotations
from fastapi import FastAPI
from zabbix_ai import __version__

def create_app() -> FastAPI:
    app = FastAPI(title="zabbix-ai", version=__version__)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__}

    return app

app = create_app()
```

- [ ] **Step 2: Add a smoke test**

```python
# tests/unit/test_app.py
from fastapi.testclient import TestClient
from zabbix_ai.app import app

def test_healthz():
    c = TestClient(app)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
```

- [ ] **Step 3: Run — expect PASS**

```bash
pytest tests/unit/test_app.py -v
```

- [ ] **Step 4: Write `deploy/systemd/zabbix-ai.service`**

```ini
[Unit]
Description=Zabbix RCA AI service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=zabbix-ai
Group=zabbix-ai
WorkingDirectory=/opt/zabbix-ai
EnvironmentFile=/etc/zabbix-ai/env
ExecStart=/opt/zabbix-ai/.venv/bin/uvicorn zabbix_ai.app:app --host 127.0.0.1 --port 8088
Restart=on-failure
RestartSec=5s
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/zabbix-ai /var/log/zabbix-ai
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/app.py deploy/systemd/zabbix-ai.service tests/unit/test_app.py
git commit -m "feat(app): FastAPI skeleton + systemd unit"
```

---

## Task 14 — README + setup docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace `README.md` with full setup instructions**

```markdown
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

- v0.1+v0.2 (this plan) — CLI, orchestrator, ~15 read-only tools
- v0.3 — Slack adapter
- v0.4 — Zabbix UI right-click adapter
- v0.5 — Memory + pattern recognition + ticket history seeding
- v0.6 — Forecasting / anomaly detection
- v0.7 — Admin UI (auth, encrypted secret store)
- v1.0 — GA
- v1.1 — HostBill webhook + customer ticket flow
- v1.2 — Optional auto-mode for Disaster severity
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README with install, config, and usage"
```

---

## Task 15 — Final smoke check

**Files:** none

- [ ] **Step 1: Run the full suite**

```bash
pytest -v --cov=zabbix_ai --cov-report=term-missing
```

Expected: all tests PASS, coverage > 80% on `zabbix_ai/`.

- [ ] **Step 2: Run lint and types**

```bash
ruff check zabbix_ai tests
mypy zabbix_ai
```

Expected: clean output, or only warnings on `tests/` (ok to ignore).

- [ ] **Step 3: Confirm CLI dry-run on real config**

With real `ANTHROPIC_API_KEY` and one Zabbix token in env:

```bash
python -m zabbix_ai --config ./config.example.yaml list-instances
```

Expected: prints configured instance names (this exercises config + env loading).

- [ ] **Step 4: Tag and commit**

```bash
git tag v0.2.0
git log --oneline | head
```

---

## Open Items Carried Forward

These are not bugs; they're scope of later plans:

- Slack/HostBill/Zabbix-UI adapters → Plans 2-3, 7
- Memory write-back, pattern recognition, ticket-history seeding → Plan 4
- Forecasting / anomaly tools → Plan 5
- Admin UI + encrypted secret store → Plan 6
- Per-host MySQL credential setup is a deployment doc, not service code

## Self-Review Notes

Spec coverage check:

- §3 Architecture — Tasks 1, 13 (skeleton + FastAPI), 11 (orchestrator)
- §4 Tool Registry — Tasks 6, 7, 8, 9 (subset for v0.2: zabbix.*, diag.*, lookup.*)
  — forecasting/correlation/memory tools deferred to Plans 4-5
- §5 Diagnostic Mechanism — Task 8 (UserParameter file + wrappers)
- §6 Safety Rules — Tasks 6 (registry), 8 (key allowlist), 4 (audit)
- §7 Token strategy — Task 10 (prompt cache), Task 11 (hard caps)
- §8 Memory — Task 3 (schema only); write-back deferred to Plan 4
- §9 Adapters — Task 12 (CLI only); others deferred
- §10 Auth — Task 2 (env-based secrets) — admin-UI encrypted store in Plan 6
- §12 Observability — partial: audit log Task 4; metrics endpoint deferred

No placeholders. Method/property names consistent across tasks
(`Memory`, `AuditLog`, `Orchestrator`, `InvestigationContext`,
`InvestigationResult`, `register`, `dispatch`, `claude_tool_definitions`,
`build_cached_system_blocks`).
