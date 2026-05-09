# Zabbix RCA AI — v0.5 Memory + HostBill Live Lookup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Add three memory tools so the AI can recognise familiar problems and learn over time: `memory.find_similar_past_investigations` and `memory.find_pattern` (read from local SQLite), and `memory.find_resolved_tickets` (queries HostBill live via API). Add automatic write-back at the end of every investigation so the local memory grows.

**Architecture:** Orchestrator gains a `_write_back()` step that runs a cheap Haiku summarisation pass to extract a pattern signature + host facts, upserts them into existing SQLite tables (`investigations`, `host_facts`, `patterns`). New `HostBillClient` wraps the admin API; `memory.find_resolved_tickets` calls it live — no local mirror, no sync, no CSV. When HostBill isn't configured, the tool returns a graceful "not configured" string so Claude proceeds without it.

**Tech Stack:** existing (Anthropic, httpx, aiosqlite, FastAPI). No new deps.

---

## File Structure

```
zabbix_ai/
  config.py                       MODIFY: add HostBillSettings (url, api_id_env, api_key_env)
  clients/
    hostbill.py                   NEW: async HostBill admin API client (read-only methods)
  memory.py                       MODIFY: add high-level helpers (write_investigation_summary,
                                          upsert_host_facts, upsert_pattern,
                                          find_similar_past_investigations, find_pattern,
                                          compute_pattern_signature)
  tools/
    memory.py                     NEW: register 3 memory tools
  orchestrator.py                 MODIFY: add _write_back() called from both
                                          investigate() and investigate_streaming()
  services/
    investigation_runner.py       MODIFY: register memory tools, build HostBillClient
                                          when settings.hostbill present
config.example.yaml               MODIFY: add hostbill: section
.env.example                      MODIFY: add HOSTBILL_API_ID, HOSTBILL_API_KEY
README.md                         MODIFY: add HostBill section, mark v0.5 done
tests/
  unit/
    test_memory_helpers.py        NEW: signature, upsert, find helpers
    test_hostbill_client.py       NEW: respx mock, search_tickets, error path
    test_tools_memory.py          NEW: dispatch each memory tool
    test_orchestrator_writeback.py NEW: investigate() upserts pattern + facts
  integration/
    test_writeback_e2e.py         NEW: full investigation → SQLite has new rows
```

Each unit small and focused. The HostBill client is in `clients/` next to `zabbix.py`, `claude.py`, `slack.py` — same pattern. The pattern-signature computation is deterministic in v0.5 (string normalisation, no LLM call) so it's testable; the per-investigation **summary** uses one cheap Haiku call.

---

## Task 1: HostBill settings in config

**Files:**
- Modify: `zabbix_ai/config.py`
- Modify: `config.example.yaml`
- Modify: `.env.example`
- Test: `tests/unit/test_config.py` (extend)

- [ ] **Step 1: Failing test**

Append to `tests/unit/test_config.py`:

```python
def test_hostbill_settings_loaded(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
hostbill:
  api_url: https://billing.test/admin/api.php
  api_id_env: HOSTBILL_API_ID
  api_key_env: HOSTBILL_API_KEY
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("HOSTBILL_API_ID", "id-1")
    monkeypatch.setenv("HOSTBILL_API_KEY", "key-1")
    s = load_settings(cfg)
    assert s.hostbill is not None
    assert s.hostbill.api_id.get_secret_value() == "id-1"
    assert s.hostbill.api_key.get_secret_value() == "key-1"
    assert str(s.hostbill.api_url).startswith("https://billing.test")


def test_hostbill_section_optional(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    assert load_settings(cfg).hostbill is None


def test_hostbill_missing_api_id_env_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
hostbill:
  api_url: https://billing.test/admin/api.php
  api_id_env: NOPE_ID
  api_key_env: HOSTBILL_API_KEY
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("HOSTBILL_API_KEY", "k")
    monkeypatch.delenv("NOPE_ID", raising=False)
    with pytest.raises(ValueError, match="NOPE_ID"):
        load_settings(cfg)
```

- [ ] **Step 2: Modify `zabbix_ai/config.py`**

Add `HostBillSettings` after `ZabbixUiSettings`:

```python
class HostBillSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    api_url: HttpUrl
    api_id_env: str
    api_key_env: str
    api_id: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")
```

Add to `Settings`:

```python
    hostbill: HostBillSettings | None = None
```

In `load_settings`, after the Zabbix UI block:

```python
    if s.hostbill is not None:
        api_id = os.environ.get(s.hostbill.api_id_env)
        if not api_id:
            raise ValueError(f"{s.hostbill.api_id_env} not set in environment")
        api_key = os.environ.get(s.hostbill.api_key_env)
        if not api_key:
            raise ValueError(f"{s.hostbill.api_key_env} not set in environment")
        s.hostbill.api_id = SecretStr(api_id)
        s.hostbill.api_key = SecretStr(api_key)
```

- [ ] **Step 3: Append to `config.example.yaml`**

```yaml

# Optional — enables HostBill ticket lookup as an investigation tool.
# Leave commented out until you have an API user with read access to tickets.
hostbill:
  api_url: https://billing.leapswitch.com/admin/api.php
  api_id_env: HOSTBILL_API_ID
  api_key_env: HOSTBILL_API_KEY
```

- [ ] **Step 4: Append to `.env.example`**

```
HOSTBILL_API_ID=
HOSTBILL_API_KEY=
```

- [ ] **Step 5: Run** `pytest tests/unit/test_config.py -v` — expect 14 tests pass.

---

## Task 2: HostBill async client

**Files:**
- Create: `zabbix_ai/clients/hostbill.py`
- Test: `tests/unit/test_hostbill_client.py`

HostBill admin API: form-POST to `<api_url>` with `api_id` + `api_key` + `call=<method>` + method-specific params. Returns JSON with `success: 1|0` plus payload keys.

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_hostbill_client.py
import pytest
import respx
from httpx import Response
from zabbix_ai.clients.hostbill import HostBillClient, HostBillError

URL = "https://billing.test/admin/api.php"

@pytest.fixture
def client():
    return HostBillClient(api_url=URL, api_id="id-1", api_key="key-1")

@respx.mock
async def test_search_tickets_returns_list(client):
    route = respx.post(URL).mock(
        return_value=Response(200, json={
            "success": 1,
            "tickets": [
                {"id": "100", "subject": "Server down", "status": "Closed",
                 "client_id": "7", "lastreply": "2026-04-10 14:00:00"},
                {"id": "101", "subject": "SSL not working", "status": "Closed",
                 "client_id": "8", "lastreply": "2026-04-12 09:00:00"},
            ],
        }),
    )
    rows = await client.search_tickets(query="Server down", status="Closed", limit=20)
    assert route.called
    body = route.calls.last.request.read().decode()
    assert "api_id=id-1" in body
    assert "api_key=key-1" in body
    assert "call=getTickets" in body
    assert len(rows) == 2
    assert rows[0]["subject"] == "Server down"

@respx.mock
async def test_get_ticket_details(client):
    respx.post(URL).mock(
        return_value=Response(200, json={"success": 1,
                                          "ticket": {"id": "100", "replies": []}}),
    )
    t = await client.get_ticket_details(100)
    assert t["id"] == "100"

@respx.mock
async def test_error_response_raises(client):
    respx.post(URL).mock(
        return_value=Response(200, json={"success": 0,
                                          "error": "Invalid API credentials"}),
    )
    with pytest.raises(HostBillError, match="Invalid API credentials"):
        await client.search_tickets(query="x", status="Closed", limit=10)
```

- [ ] **Step 2: Implement `zabbix_ai/clients/hostbill.py`**

```python
from __future__ import annotations
from typing import Any
import httpx


class HostBillError(Exception):
    pass


class HostBillClient:
    """Read-only client for HostBill's admin API.

    HostBill expects form-encoded POSTs with api_id, api_key, call=<method>,
    plus method-specific params. Responses are JSON {"success": 0|1, ...}.
    """

    def __init__(self, *, api_url: str, api_id: str, api_key: str,
                 timeout: float = 10.0):
        self._api_url = api_url
        self._api_id = api_id
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        form = {"api_id": self._api_id, "api_key": self._api_key,
                "call": method, **{k: str(v) for k, v in params.items()
                                    if v is not None}}
        r = await self._client.post(self._api_url, data=form)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise HostBillError(f"{method}: {data.get('error', 'unknown')}")
        return data

    async def search_tickets(self, *, query: str = "", status: str = "Closed",
                             limit: int = 20,
                             department_id: int | None = None) -> list[dict]:
        data = await self._call(
            "getTickets",
            search=query, status=status,
            limit=limit, department_id=department_id,
        )
        return data.get("tickets", []) or []

    async def get_ticket_details(self, ticket_id: int) -> dict[str, Any]:
        data = await self._call("getTicketDetails", id=ticket_id)
        return data.get("ticket", {}) or {}

    async def get_client_details(self, client_id: int) -> dict[str, Any]:
        data = await self._call("getClientDetails", id=client_id)
        return data.get("client", {}) or {}
```

- [ ] **Step 3: Run** `pytest tests/unit/test_hostbill_client.py -v` — expect 3 pass.

---

## Task 3: Memory helpers

**Files:**
- Modify: `zabbix_ai/memory.py` (add helpers below the existing `Memory` class)
- Test: `tests/unit/test_memory_helpers.py`

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_memory_helpers.py
from pathlib import Path
import pytest
from zabbix_ai.memory import (
    Memory, compute_pattern_signature,
    write_investigation_summary, upsert_host_facts, upsert_pattern,
    find_similar_past_investigations, find_pattern,
)

@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "mem.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()

def test_signature_normalises_text():
    a = compute_pattern_signature(problem_name="Disk Space LOW on /var",
                                   hostgroup="Managed cPanel VPS")
    b = compute_pattern_signature(problem_name="disk space low on /var",
                                   hostgroup="managed cpanel vps")
    assert a == b
    # signature is a short stable string
    assert len(a) <= 64
    # different problem produces different signature
    c = compute_pattern_signature(problem_name="apache down",
                                   hostgroup="Managed cPanel VPS")
    assert a != c


async def test_write_investigation_summary_updates_row(mem):
    await mem.execute(
        "INSERT INTO investigations (source, started_at) VALUES (?, ?)",
        ("test", "2026-05-09T00:00:00+00:00"),
    )
    inv_id = (await mem.fetchone("SELECT last_insert_rowid()"))[0]
    await write_investigation_summary(
        mem, investigation_id=inv_id,
        summary="ok", root_cause="rc", suggested_actions="do x",
        confidence="high", pattern_signature="sig-1",
    )
    row = await mem.fetchone(
        "SELECT root_cause, suggested_actions, confidence, pattern_signature "
        "FROM investigations WHERE id=?", (inv_id,),
    )
    assert row == ("rc", "do x", "high", "sig-1")


async def test_upsert_host_facts_inserts_then_updates(mem):
    await upsert_host_facts(mem, hostid=12345, facts={
        "primary_role": "mysql replica",
        "rack": "DC2-R7",
    }, source_investigation_id=1)
    rows = await mem.fetchall(
        "SELECT key, value FROM host_facts WHERE hostid=?", (12345,),
    )
    assert dict(rows) == {"primary_role": "mysql replica", "rack": "DC2-R7"}
    # update overrides
    await upsert_host_facts(mem, hostid=12345, facts={"rack": "DC2-R8"},
                              source_investigation_id=2)
    rows = await mem.fetchall(
        "SELECT key, value FROM host_facts WHERE hostid=?", (12345,),
    )
    assert dict(rows) == {"primary_role": "mysql replica", "rack": "DC2-R8"}


async def test_upsert_pattern_increments_occurrences(mem):
    await upsert_pattern(mem, signature="sig-1",
                         typical_root_cause="disk full on /var",
                         typical_fix="rotate logs")
    await upsert_pattern(mem, signature="sig-1",
                         typical_root_cause="disk full on /var",
                         typical_fix="rotate logs")
    row = await mem.fetchone(
        "SELECT occurrences, typical_fix FROM patterns WHERE signature=?",
        ("sig-1",),
    )
    assert row[0] == 2
    assert row[1] == "rotate logs"


async def test_find_similar_past_investigations(mem):
    for sig, hostid, summary in [
        ("sig-A", 1, "old run 1"),
        ("sig-A", 1, "old run 2"),
        ("sig-B", 1, "different pattern"),
        ("sig-A", 2, "different host"),
    ]:
        await mem.execute(
            "INSERT INTO investigations (source, started_at, hostid, "
            " pattern_signature, summary) VALUES (?, ?, ?, ?, ?)",
            ("cli", "2026-05-09T00:00:00+00:00", hostid, sig, summary),
        )
    by_host = await find_similar_past_investigations(
        mem, hostid=1, pattern_signature=None, limit=5,
    )
    assert len(by_host) == 3  # 3 rows for hostid=1
    by_pattern = await find_similar_past_investigations(
        mem, hostid=None, pattern_signature="sig-A", limit=5,
    )
    assert len(by_pattern) == 3  # 3 rows with sig-A
    by_both = await find_similar_past_investigations(
        mem, hostid=1, pattern_signature="sig-A", limit=5,
    )
    assert len(by_both) == 2


async def test_find_pattern_returns_pattern_row(mem):
    await upsert_pattern(mem, signature="sig-x",
                         typical_root_cause="rc", typical_fix="fix")
    row = await find_pattern(mem, signature="sig-x")
    assert row is not None
    assert row["signature"] == "sig-x"
    assert row["typical_root_cause"] == "rc"
    assert row["occurrences"] == 1
    # missing signature → None
    assert await find_pattern(mem, signature="nope") is None
```

- [ ] **Step 2: Append to `zabbix_ai/memory.py`**

```python
import hashlib
import re
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_pattern_signature(*, problem_name: str, hostgroup: str = "") -> str:
    """Stable, lowercase, whitespace-collapsed hash of (problem, hostgroup).

    Deterministic so re-occurrences of the same alert on the same kind of
    host produce the same signature. Returns a hex string (16 chars) — short
    enough to read in logs, wide enough for collisions to be ignorable at
    the volume we expect (<<1M patterns).
    """
    norm = lambda s: re.sub(r"\s+", " ", (s or "").lower()).strip()
    raw = f"{norm(problem_name)}|{norm(hostgroup)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def write_investigation_summary(
    memory: "Memory", *, investigation_id: int,
    summary: str = "", root_cause: str = "", suggested_actions: str = "",
    confidence: str = "", pattern_signature: str = "",
) -> None:
    await memory.execute(
        """UPDATE investigations
           SET summary=?, root_cause=?, suggested_actions=?, confidence=?,
               pattern_signature=?
           WHERE id=?""",
        (summary, root_cause, suggested_actions, confidence,
         pattern_signature, investigation_id),
    )


async def upsert_host_facts(
    memory: "Memory", *, hostid: int, facts: dict[str, str],
    source_investigation_id: int | None = None,
) -> None:
    ts = _now_iso()
    for key, value in facts.items():
        await memory.execute(
            """INSERT INTO host_facts (hostid, key, value,
                                        source_investigation_id, learned_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(hostid, key) DO UPDATE SET
                 value=excluded.value,
                 source_investigation_id=excluded.source_investigation_id,
                 learned_at=excluded.learned_at""",
            (hostid, key, value, source_investigation_id, ts),
        )


async def upsert_pattern(
    memory: "Memory", *, signature: str,
    typical_root_cause: str = "", typical_fix: str = "",
) -> None:
    ts = _now_iso()
    await memory.execute(
        """INSERT INTO patterns (signature, first_seen, last_seen, occurrences,
                                  typical_root_cause, typical_fix,
                                  confidence_score)
           VALUES (?, ?, ?, 1, ?, ?, 0.5)
           ON CONFLICT(signature) DO UPDATE SET
             last_seen=excluded.last_seen,
             occurrences=patterns.occurrences + 1,
             typical_root_cause=excluded.typical_root_cause,
             typical_fix=excluded.typical_fix""",
        (signature, ts, ts, typical_root_cause, typical_fix),
    )


async def find_similar_past_investigations(
    memory: "Memory", *, hostid: int | None,
    pattern_signature: str | None, limit: int = 5,
) -> list[dict]:
    where = []
    params: list = []
    if hostid is not None:
        where.append("hostid = ?")
        params.append(hostid)
    if pattern_signature:
        where.append("pattern_signature = ?")
        params.append(pattern_signature)
    if not where:
        return []
    sql = (
        "SELECT id, started_at, hostid, hostname, pattern_signature, "
        "       summary, root_cause, confidence "
        "FROM investigations "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY id DESC LIMIT ?"
    )
    params.append(limit)
    rows = await memory.fetchall(sql, tuple(params))
    keys = ("id", "started_at", "hostid", "hostname", "pattern_signature",
            "summary", "root_cause", "confidence")
    return [dict(zip(keys, r)) for r in rows]


async def find_pattern(memory: "Memory", *, signature: str) -> dict | None:
    row = await memory.fetchone(
        "SELECT signature, first_seen, last_seen, occurrences, "
        "       typical_root_cause, typical_fix, confidence_score "
        "FROM patterns WHERE signature=?",
        (signature,),
    )
    if not row:
        return None
    return dict(zip(
        ("signature", "first_seen", "last_seen", "occurrences",
         "typical_root_cause", "typical_fix", "confidence_score"), row,
    ))
```

- [ ] **Step 3: Run** `pytest tests/unit/test_memory_helpers.py -v` — expect 6 pass.

---

## Task 4: Memory tools

**Files:**
- Create: `zabbix_ai/tools/memory.py`
- Test: `tests/unit/test_tools_memory.py`

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_tools_memory.py
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from zabbix_ai.memory import Memory, upsert_pattern
from zabbix_ai.tools import dispatch
from zabbix_ai.tools.memory import register_tools

@pytest.fixture
async def context(tmp_path):
    m = Memory(tmp_path / "t.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    await upsert_pattern(m, signature="sig-A",
                         typical_root_cause="disk full",
                         typical_fix="rotate logs")
    await m.execute(
        "INSERT INTO investigations (source, started_at, hostid, "
        " pattern_signature, summary) VALUES (?, ?, ?, ?, ?)",
        ("cli", "2026-05-09T00:00:00+00:00", 12345, "sig-A", "old run"),
    )
    yield {"memory": m, "hostbill_client": None}
    await m.close()


async def test_find_similar_by_hostid(context):
    register_tools()
    rows = await dispatch(
        "memory.find_similar_past_investigations",
        {"hostid": 12345}, context=context,
    )
    assert len(rows) == 1
    assert rows[0]["summary"] == "old run"


async def test_find_pattern(context):
    register_tools()
    row = await dispatch(
        "memory.find_pattern",
        {"signature": "sig-A"}, context=context,
    )
    assert row is not None
    assert row["typical_fix"] == "rotate logs"


async def test_find_resolved_tickets_when_hostbill_not_configured(context):
    register_tools()
    out = await dispatch(
        "memory.find_resolved_tickets",
        {"alert_pattern": "disk full", "limit": 5}, context=context,
    )
    assert isinstance(out, str)
    assert "not configured" in out.lower()


async def test_find_resolved_tickets_calls_hostbill(tmp_path):
    m = Memory(tmp_path / "tb.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    fake = MagicMock()
    fake.search_tickets = AsyncMock(return_value=[
        {"id": "100", "subject": "Disk full on web-01", "status": "Closed",
         "client_id": "7", "lastreply": "2026-04-10 14:00:00"},
    ])
    register_tools()
    out = await dispatch(
        "memory.find_resolved_tickets",
        {"alert_pattern": "Disk full", "limit": 5},
        context={"memory": m, "hostbill_client": fake},
    )
    await m.close()
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["subject"] == "Disk full on web-01"
    fake.search_tickets.assert_awaited_once()
```

- [ ] **Step 2: Implement `zabbix_ai/tools/memory.py`**

```python
from __future__ import annotations
from zabbix_ai.memory import (
    find_pattern as _find_pattern,
    find_similar_past_investigations as _find_similar,
)
from zabbix_ai.tools import register


def _memory(ctx: dict):
    mem = ctx.get("memory")
    if mem is None:
        raise ValueError("memory not in context — orchestrator misconfigured")
    return mem


def register_tools() -> None:
    @register(
        "memory.find_similar_past_investigations",
        description=(
            "Find past investigations on the same host or matching the same "
            "pattern signature. Use when an alert looks familiar."
        ),
        schema={"type": "object",
                "properties": {
                    "hostid": {"type": "integer"},
                    "pattern_signature": {"type": "string"},
                    "limit": {"type": "integer", "default": 5}},
                "required": []},
    )
    async def _similar(*, hostid: int | None = None,
                       pattern_signature: str | None = None,
                       limit: int = 5, _ctx: dict) -> list[dict]:
        return await _find_similar(_memory(_ctx),
                                    hostid=hostid,
                                    pattern_signature=pattern_signature,
                                    limit=limit)

    @register(
        "memory.find_pattern",
        description=(
            "Look up an alert pattern by its stable signature. Returns typical "
            "root cause and fix from past investigations of the same pattern."
        ),
        schema={"type": "object",
                "properties": {"signature": {"type": "string"}},
                "required": ["signature"]},
    )
    async def _pattern(*, signature: str, _ctx: dict) -> dict | None:
        return await _find_pattern(_memory(_ctx), signature=signature)

    @register(
        "memory.find_resolved_tickets",
        description=(
            "Search HostBill for closed customer tickets that match the given "
            "alert pattern. Use to surface past resolutions for similar issues."
        ),
        schema={"type": "object",
                "properties": {
                    "alert_pattern": {"type": "string"},
                    "limit": {"type": "integer", "default": 5}},
                "required": ["alert_pattern"]},
    )
    async def _tickets(*, alert_pattern: str, limit: int = 5,
                       _ctx: dict) -> list[dict] | str:
        client = _ctx.get("hostbill_client")
        if client is None:
            return "HostBill not configured — pattern lookup unavailable"
        rows = await client.search_tickets(query=alert_pattern,
                                            status="Closed", limit=limit)
        # Return only the fields a Claude needs
        keys = ("id", "subject", "status", "client_id", "lastreply")
        return [{k: r.get(k) for k in keys} for r in rows]
```

- [ ] **Step 3: Run** `pytest tests/unit/test_tools_memory.py -v` — expect 4 pass.

---

## Task 5: Orchestrator write-back

The orchestrator already updates the `investigations` row's `summary` etc. in `audit.log_end()`. We add a separate `_write_back()` that:

1. Calls Haiku for a brief summary + extracted host facts (1 cheap call)
2. Computes pattern signature from problem name + first hostgroup
3. Upserts host_facts + patterns

`_write_back()` is called from BOTH `investigate()` and `investigate_streaming()`. It's resilient: failures are logged but don't break the user-facing result.

**Files:**
- Modify: `zabbix_ai/orchestrator.py`
- Test: `tests/unit/test_orchestrator_writeback.py`

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_orchestrator_writeback.py
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from zabbix_ai.orchestrator import Orchestrator, InvestigationContext
from zabbix_ai.memory import Memory, find_pattern
from zabbix_ai.audit import AuditLog
from zabbix_ai.tools import register


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


def _resp(stop_reason, blocks, in_t=10, out_t=5):
    return MagicMock(stop_reason=stop_reason, content=blocks,
                     usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))


@pytest.fixture
async def setup(tmp_path):
    @register("test.echo", description="echo",
              schema={"type": "object", "properties": {"x": {"type": "string"}},
                      "required": ["x"]})
    async def _e(*, x: str) -> str:
        return x
    m = Memory(tmp_path / "wb.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m, AuditLog(m)
    await m.close()


async def test_writeback_creates_pattern_row_with_signature(setup):
    m, audit = setup
    claude = MagicMock()
    # main loop: end_turn directly, then _write_back haiku call returning JSON
    claude.create = AsyncMock(side_effect=[
        _resp("end_turn", [_Block(type="text",
                                   text="root_cause: disk full\nconfidence: high")]),
        _resp("end_turn", [_Block(type="text",
                                   text=json.dumps({
                                       "root_cause_short": "disk full on /var",
                                       "fix_short": "rotate logs",
                                       "host_facts": {"primary_role": "web"}}))]),
    ])
    o = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                     max_tool_calls=8,
                     clients={}, memory=m)
    ctx = InvestigationContext(
        source="cli", instance="monitoring", hostid=12345,
        hostname="web-01",
        problem_name="Disk space low on /var",
        hostgroup="Managed cPanel VPS",
    )
    result = await o.investigate(ctx)
    # pattern table populated
    sig = result.pattern_signature
    assert sig
    pat = await find_pattern(m, signature=sig)
    assert pat is not None
    assert pat["typical_fix"] == "rotate logs"
    # host_facts populated
    rows = await m.fetchall(
        "SELECT key, value FROM host_facts WHERE hostid=12345",
    )
    assert ("primary_role", "web") in rows


async def test_writeback_failure_does_not_break_result(setup):
    m, audit = setup
    claude = MagicMock()
    # main returns end_turn ok, _write_back haiku raises
    claude.create = AsyncMock(side_effect=[
        _resp("end_turn", [_Block(type="text", text="ok")]),
        Exception("haiku timeout"),
    ])
    o = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                     max_tool_calls=8, clients={}, memory=m)
    result = await o.investigate(InvestigationContext(
        source="cli", problem_name="x", hostid=1,
    ))
    assert result.summary == "ok"
    assert result.investigation_id  # didn't crash
```

- [ ] **Step 2: Modify `zabbix_ai/orchestrator.py`**

Extend `InvestigationContext` (top of file) with two new optional fields used for signature computation:

```python
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
    problem_name: str = ""
    hostgroup: str = ""
```

Extend `InvestigationResult` with `pattern_signature`:

```python
@dataclass
class InvestigationResult:
    investigation_id: int
    summary: str
    tool_calls: int
    tokens_in: int
    tokens_out: int
    duration_ms: int
    transcript: list[dict[str, Any]] = field(default_factory=list)
    pattern_signature: str = ""
```

Update `Orchestrator.__init__` to accept an optional `memory: Memory | None = None`:

```python
class Orchestrator:
    def __init__(self, *, claude, audit: AuditLog, model: str, summary_model: str,
                 max_tool_calls: int, clients: dict[str, Any],
                 memory: "Memory | None" = None):
        self.claude = claude
        self.audit = audit
        self.model = model
        self.summary_model = summary_model
        self.max_tool_calls = max_tool_calls
        self.clients = clients
        self.memory = memory
```

Add at the top of the file:

```python
import json
import logging
from zabbix_ai.memory import (
    Memory, compute_pattern_signature,
    upsert_host_facts, upsert_pattern,
)

_log = logging.getLogger(__name__)
```

Add the `_write_back` private method on the class:

```python
    async def _write_back(self, *, ctx: InvestigationContext,
                          investigation_id: int,
                          final_text: str) -> str:
        """Run a cheap summarisation pass and update memory.

        Returns the pattern signature (empty string if write-back failed or
        memory is not configured).
        """
        if self.memory is None or not ctx.problem_name:
            return ""
        sig = compute_pattern_signature(problem_name=ctx.problem_name,
                                         hostgroup=ctx.hostgroup or "")
        try:
            prompt = (
                "Summarise this investigation as a JSON object with keys: "
                "root_cause_short (one sentence), fix_short (one sentence), "
                "host_facts (object of key→value strings, can be empty). "
                "Output ONLY the JSON object.\n\n"
                f"Investigation summary:\n{final_text[:4000]}"
            )
            resp = await self.claude.create(
                model=self.summary_model,
                system=[{"type": "text",
                         "text": "You extract structured facts from "
                                  "investigation summaries."}],
                tools=[],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
            )
            text = self._extract_text(resp.content)
            data = json.loads(text)
            await upsert_pattern(
                self.memory, signature=sig,
                typical_root_cause=str(data.get("root_cause_short", "")),
                typical_fix=str(data.get("fix_short", "")),
            )
            facts = data.get("host_facts") or {}
            if isinstance(facts, dict) and ctx.hostid is not None and facts:
                await upsert_host_facts(
                    self.memory, hostid=ctx.hostid,
                    facts={k: str(v) for k, v in facts.items()
                           if isinstance(v, (str, int, float))},
                    source_investigation_id=investigation_id,
                )
        except Exception as e:  # noqa: BLE001
            _log.warning("write_back failed for inv %s: %s",
                         investigation_id, e)
        return sig
```

In `investigate()`, immediately before the `return InvestigationResult(...)`, call:

```python
        signature = await self._write_back(
            ctx=ctx, investigation_id=inv_id, final_text=final_text,
        )
```

And include it on the return:

```python
        return InvestigationResult(
            investigation_id=inv_id, summary=final_text,
            tool_calls=tool_calls, tokens_in=tokens_in, tokens_out=tokens_out,
            duration_ms=duration_ms, transcript=messages,
            pattern_signature=signature,
        )
```

Apply the same `_write_back` call inside `investigate_streaming()` immediately before yielding the `final` event, and include `pattern_signature` in the final event's data:

```python
        signature = await self._write_back(
            ctx=ctx, investigation_id=inv_id, final_text=final_text,
        )
        ...
        yield {"event": "final",
               "data": {"investigation_id": inv_id,
                        "summary": final_text,
                        "tool_calls": tool_calls,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "duration_ms": duration_ms,
                        "pattern_signature": signature}}
```

- [ ] **Step 3: Run** `pytest tests/unit/test_orchestrator_writeback.py -v` — expect 2 pass.

---

## Task 6: InvestigationRunner wires HostBill + memory tools

**Files:**
- Modify: `zabbix_ai/services/investigation_runner.py`
- Test: `tests/integration/test_writeback_e2e.py`

- [ ] **Step 1: Failing test**

```python
# tests/integration/test_writeback_e2e.py
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from zabbix_ai.config import load_settings
from zabbix_ai.memory import Memory, find_pattern
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.services.investigation_runner import InvestigationRunner


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


def _claude(stop_reason, blocks, in_t=10, out_t=5):
    return MagicMock(stop_reason=stop_reason, content=blocks,
                     usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))


@pytest.fixture
def settings(tmp_path, monkeypatch):
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
    return load_settings(cfg), tmp_path / "state.db"


async def test_runner_writeback_persists_to_sqlite(settings):
    s, db_path = settings
    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as MockA:
        MockA.return_value.messages.create = AsyncMock(side_effect=[
            _claude("end_turn", [_Block(type="text", text="ok")]),
            _claude("end_turn", [_Block(type="text",
                                         text=json.dumps({
                                             "root_cause_short": "disk full",
                                             "fix_short": "rotate logs",
                                             "host_facts": {"role": "web"}}))]),
        ])
        async with InvestigationRunner(s) as runner:
            ctx = InvestigationContext(
                source="cli", instance="monitoring",
                hostid=12345, hostname="web-01",
                problem_name="Disk space low on /var",
                hostgroup="Managed cPanel VPS",
            )
            result = await runner.investigate(ctx)
            assert result.pattern_signature

    # reopen DB and confirm rows
    m = Memory(str(db_path))
    await m.connect()
    pat = await find_pattern(m, signature=result.pattern_signature)
    assert pat is not None
    assert pat["typical_fix"] == "rotate logs"
    rows = await m.fetchall(
        "SELECT key, value FROM host_facts WHERE hostid=12345",
    )
    await m.close()
    assert ("role", "web") in rows
```

- [ ] **Step 2: Modify `zabbix_ai/services/investigation_runner.py`**

Add imports near the top:

```python
from zabbix_ai.clients.hostbill import HostBillClient
from zabbix_ai.tools import memory as tools_memory
```

In `__aenter__`, add memory tool registration (next to the others):

```python
        tools_zabbix.register_tools()
        tools_diag.register_tools()
        tools_lookup.register_tools()
        tools_memory.register_tools()
```

Build `HostBillClient` if configured:

```python
        self._hostbill: HostBillClient | None = None
        if self.settings.hostbill is not None:
            self._hostbill = HostBillClient(
                api_url=str(self.settings.hostbill.api_url),
                api_id=self.settings.hostbill.api_id.get_secret_value(),
                api_key=self.settings.hostbill.api_key.get_secret_value(),
            )
```

(Initialise `self._hostbill = None` in `__init__` for type-stability.)

Pass `memory` and `hostbill_client` into the orchestrator. The orchestrator constructor needs `memory`; the `clients` arg now also needs to deliver `hostbill_client` to the memory tools' context. Two approaches — pick the one with smaller surface change:

Approach A: extend orchestrator's tool-call context to include `memory` and `hostbill_client` directly. This means inside `dispatch(...)`, the context dict has `clients`, `memory`, `hostbill_client`. The memory tools already read `ctx["memory"]` and `ctx["hostbill_client"]`. The Zabbix tools currently read `ctx["clients"]`. Both keep working.

Modify `Orchestrator` constructor (one more arg) and the two places where `dispatch(...)` is called:

In `orchestrator.py`:

```python
class Orchestrator:
    def __init__(self, *, claude, audit, model, summary_model,
                 max_tool_calls, clients,
                 memory: "Memory | None" = None,
                 hostbill_client=None):
        ...
        self.hostbill_client = hostbill_client
```

In both `investigate()` and `investigate_streaming()`, where we currently pass `context={"clients": self.clients, "investigation_id": inv_id}` into `dispatch`, change to:

```python
                context={
                    "clients": self.clients,
                    "investigation_id": inv_id,
                    "memory": self.memory,
                    "hostbill_client": self.hostbill_client,
                },
```

Now back in `investigation_runner.py`'s `__aenter__`:

```python
        self._orch = Orchestrator(
            claude=claude,
            audit=AuditLog(self._mem),
            model=self.settings.default_model,
            summary_model=self.settings.summary_model,
            max_tool_calls=self.settings.max_tool_calls,
            clients=self._zabbix_clients,
            memory=self._mem,
            hostbill_client=self._hostbill,
        )
```

In `__aexit__`, also close hostbill:

```python
        for c in self._zabbix_clients.values():
            await c.aclose()
        if self._hostbill is not None:
            await self._hostbill.aclose()
        if self._mem:
            await self._mem.close()
```

- [ ] **Step 3: Run** `pytest tests/integration/test_writeback_e2e.py -v` — expect 1 pass.

---

## Task 7: README + final test pass + commit + tag

**Files:**
- Modify: `README.md`
- Bump: `zabbix_ai/__init__.py` and `pyproject.toml`

- [ ] **Step 1: Update `README.md`**

After the Zabbix UI section, before the agent UserParameters section, insert:

```markdown
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
```

Mark `v0.5` complete in the Roadmap line.

- [ ] **Step 2: Run full suite + ruff**

```bash
. .venv/bin/activate
pytest -v
ruff check zabbix_ai tests --fix
ruff check zabbix_ai tests
```

Expected: ~95 tests pass, ruff clean.

- [ ] **Step 3: Bump version**

```bash
sed -i 's/__version__ = "0.4.0"/__version__ = "0.5.0"/' zabbix_ai/__init__.py
sed -i 's/version = "0.4.0"/version = "0.5.0"/' pyproject.toml
```

- [ ] **Step 4: Single commit + tag**

```bash
git add -A
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(v0.5): memory write-back, pattern recognition, live HostBill lookup

Adds three memory tools to the orchestrator: find_similar_past_investigations
and find_pattern (read from local SQLite), and find_resolved_tickets which
queries HostBill live via the admin API. Orchestrator now runs a cheap
Haiku-based write-back at the end of every investigation, computing a
deterministic pattern signature from problem name + hostgroup, upserting
the patterns table (occurrences ++) and learned host facts. HostBill
section is optional — when not configured, find_resolved_tickets returns
a graceful "not configured" string. Local memory works regardless.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git tag v0.5.0
```

---

## Self-Review Notes

- **Spec coverage** (design §8): ✓ Memory schema reused as-is, ✓ pattern signature computation, ✓ memory tools, ✓ live HostBill lookup as the path for ticket history (CSV explicitly skipped per user request), ✓ write-back loop with cheap Haiku summarisation.
- **Type consistency**: `compute_pattern_signature`, `upsert_host_facts`, `upsert_pattern`, `find_similar_past_investigations`, `find_pattern` defined in Task 3 and consumed unchanged in Tasks 4, 5, 6. `InvestigationContext` gets two new fields used by `_write_back`. `InvestigationResult` gains `pattern_signature`. All names lowercase + underscored consistently.
- **No placeholders.** Every step shows the exact code/command/expected.
- **Deferred to v0.7**: HostBill admin UI form (replaces env-config), encrypted secret storage. The current env approach works the moment user gets API access; v0.7 makes it self-service.
