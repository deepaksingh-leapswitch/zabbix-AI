# Zabbix RCA AI — v0.4 Zabbix UI Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Add a Zabbix UI right-click ("Investigate with AI") flow: a signed URL endpoint that renders a streaming HTML page so an L1 engineer clicking on a Zabbix problem sees the AI's tool calls and reasoning live.

**Architecture:** A FastAPI route `/investigate` validates an HMAC-signed URL, opens a Server-Sent Events stream, and pipes orchestrator progress events through a thin streaming wrapper around the existing `Orchestrator`. The streaming wrapper publishes `tool_call`, `tool_result`, `thinking`, and `final` events as the tool-use loop runs. A Jinja2 template renders the page shell and a tiny JS client consumes the SSE stream and appends events to the DOM.

**Tech Stack:** FastAPI `EventSourceResponse` (sse-starlette), Jinja2 templates, hmac/hashlib, the existing orchestrator, plus a Zabbix Frontend Script of type URL.

---

## File Structure

```
zabbix_ai/
  adapters/
    zabbix_ui.py             NEW: /investigate route + URL signature verify + SSE stream
  orchestrator.py            MODIFY: add investigate_streaming() async generator
  renderers/
    html.py                  NEW: Jinja2 environment + render_page(eventid, instance, sign_token)
  templates/
    investigate.html         NEW: page shell + SSE-consuming JS
  url_signing.py             NEW: sign_url_token() / verify_url_token() with TTL
  app.py                     MODIFY: mount Zabbix UI router, mount static if needed
  config.py                  MODIFY: add ZabbixUiSettings (signing key env, allowed_ips, ttl)
deploy/
  zabbix-frontend-script.md  NEW: ops doc — how to wire the right-click in Zabbix
tests/
  unit/
    test_url_signing.py      NEW: sign/verify happy path, expired, tampered, wrong key
    test_html_renderer.py    NEW: page shell renders, escapes, no XSS in eventid
    test_streaming_orchestrator.py  NEW: investigate_streaming yields expected events
  integration/
    test_zabbix_ui_e2e.py    NEW: signed GET → SSE stream → final event
config.example.yaml          MODIFY: add zabbix_ui: section
.env.example                 MODIFY: add URL_SIGNING_KEY
pyproject.toml               MODIFY: add sse-starlette + jinja2
README.md                    MODIFY: add Zabbix UI setup section, mark v0.4 done
```

Each module has one responsibility. URL signing is its own module (reusable later for HostBill webhook signing). HTML renderer doesn't know about FastAPI; the adapter binds them together. Streaming orchestrator is an additive method on `Orchestrator` — we don't refactor `investigate()`.

---

## Task 1: Add Jinja2 + sse-starlette dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add deps**

In `pyproject.toml`, append to `dependencies`:

```
    "jinja2>=3.1",
    "sse-starlette>=2.1",
```

- [ ] **Step 2: Install**

```bash
. .venv/bin/activate
pip install -e ".[dev]"
```

Confirm:

```bash
python -c "import jinja2, sse_starlette; print(jinja2.__version__, sse_starlette.__version__)"
```

---

## Task 2: URL signing module

**Files:**
- Create: `zabbix_ai/url_signing.py`
- Test: `tests/unit/test_url_signing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_url_signing.py
import time
import pytest
from zabbix_ai.url_signing import sign_url_token, verify_url_token, UrlSignatureError

KEY = "test-key-32-bytes-or-more-please-pad"

def test_sign_then_verify_passes():
    payload = {"eventid": 998877, "instance": "monitoring"}
    token = sign_url_token(payload, ttl_seconds=300, signing_key=KEY)
    out = verify_url_token(token, signing_key=KEY)
    assert out["eventid"] == 998877
    assert out["instance"] == "monitoring"

def test_expired_token_rejected():
    payload = {"eventid": 1, "instance": "x"}
    token = sign_url_token(payload, ttl_seconds=-10, signing_key=KEY)  # already expired
    with pytest.raises(UrlSignatureError, match="expired"):
        verify_url_token(token, signing_key=KEY)

def test_tampered_payload_rejected():
    token = sign_url_token({"eventid": 1, "instance": "x"},
                           ttl_seconds=300, signing_key=KEY)
    # flip one byte in the encoded payload portion
    parts = token.split(".")
    parts[0] = parts[0][:-1] + ("A" if parts[0][-1] != "A" else "B")
    tampered = ".".join(parts)
    with pytest.raises(UrlSignatureError, match="signature"):
        verify_url_token(tampered, signing_key=KEY)

def test_wrong_key_rejected():
    token = sign_url_token({"eventid": 1, "instance": "x"},
                           ttl_seconds=300, signing_key=KEY)
    with pytest.raises(UrlSignatureError, match="signature"):
        verify_url_token(token, signing_key="other-key")

def test_malformed_token_rejected():
    with pytest.raises(UrlSignatureError):
        verify_url_token("not-a-token", signing_key=KEY)

def test_payload_round_trips_extra_fields():
    payload = {"eventid": 7, "instance": "monitoring", "user": "alice"}
    token = sign_url_token(payload, ttl_seconds=300, signing_key=KEY)
    out = verify_url_token(token, signing_key=KEY)
    assert out["user"] == "alice"
```

- [ ] **Step 2: Run — expect FAIL (ModuleNotFoundError)**

```bash
pytest tests/unit/test_url_signing.py -v
```

- [ ] **Step 3: Implement**

```python
# zabbix_ai/url_signing.py
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import time
from typing import Any

class UrlSignatureError(Exception):
    pass

def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)

def sign_url_token(payload: dict[str, Any], *, ttl_seconds: int,
                   signing_key: str) -> str:
    """Return a token of the form: b64(payload_json).b64(exp).b64(hmac_sha256)."""
    exp = int(time.time()) + ttl_seconds
    payload_b = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload_part = _b64encode(payload_b)
    exp_part = _b64encode(str(exp).encode())
    msg = f"{payload_part}.{exp_part}".encode()
    sig = hmac.new(signing_key.encode(), msg, hashlib.sha256).digest()
    return f"{payload_part}.{exp_part}.{_b64encode(sig)}"

def verify_url_token(token: str, *, signing_key: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise UrlSignatureError("malformed token")
    payload_part, exp_part, sig_part = parts
    try:
        payload_b = _b64decode(payload_part)
        exp = int(_b64decode(exp_part))
        provided_sig = _b64decode(sig_part)
    except (ValueError, json.JSONDecodeError) as e:
        raise UrlSignatureError("malformed token") from e
    expected_sig = hmac.new(signing_key.encode(),
                             f"{payload_part}.{exp_part}".encode(),
                             hashlib.sha256).digest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        raise UrlSignatureError("signature mismatch")
    if int(time.time()) > exp:
        raise UrlSignatureError("token expired")
    try:
        return json.loads(payload_b)
    except json.JSONDecodeError as e:
        raise UrlSignatureError("malformed payload") from e
```

- [ ] **Step 4: Run — expect 6 PASS**

---

## Task 3: Streaming orchestrator method

The existing `Orchestrator.investigate()` returns the final `InvestigationResult`. For SSE we need an async generator that yields progress events.

**Files:**
- Modify: `zabbix_ai/orchestrator.py` — add `investigate_streaming()`
- Test: `tests/unit/test_streaming_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_streaming_orchestrator.py
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from zabbix_ai.orchestrator import Orchestrator, InvestigationContext
from zabbix_ai.memory import Memory
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
async def orch(tmp_path):
    @register("test.echo", description="echo",
              schema={"type": "object",
                      "properties": {"msg": {"type": "string"}},
                      "required": ["msg"]})
    async def echo(*, msg: str) -> str:
        return f"got:{msg}"
    m = Memory(tmp_path / "s.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    audit = AuditLog(m)
    yield m, audit
    await m.close()

async def test_streaming_yields_tool_call_then_final(orch):
    _m, audit = orch
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id="t1",
                                  name="test.echo", input={"msg": "hi"})]),
        _resp("end_turn", [_Block(type="text", text="done")]),
    ])
    o = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                     max_tool_calls=8, clients={})
    events = [e async for e in o.investigate_streaming(
        InvestigationContext(source="ui", question="?"),
    )]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started"
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "final"
    final = events[-1]["data"]
    assert final["summary"] == "done"
    assert final["tool_calls"] == 1


async def test_streaming_yields_error_event_on_unknown_tool(orch):
    _m, audit = orch
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id="t1",
                                  name="evil.delete", input={})]),
        _resp("end_turn", [_Block(type="text", text="bailed")]),
    ])
    o = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                     max_tool_calls=8, clients={})
    events = [e async for e in o.investigate_streaming(
        InvestigationContext(source="ui", question="?"),
    )]
    kinds = [e["event"] for e in events]
    # Even on unknown tool, the loop continues and emits a tool_result with is_error
    assert any(e["event"] == "tool_result" and
               e["data"].get("is_error") for e in events)
    assert kinds[-1] == "final"
```

- [ ] **Step 2: Run — expect FAIL (no investigate_streaming method)**

- [ ] **Step 3: Add `investigate_streaming` to `Orchestrator`**

Append this method inside the `Orchestrator` class (after `investigate`):

```python
    async def investigate_streaming(self, ctx: "InvestigationContext"):
        """Yield SSE-friendly events as the tool-use loop runs.

        Each yielded value is {"event": <str>, "data": <dict|str>}.
        Event kinds: started, tool_call, tool_result, thinking, final.
        """
        import time as _time
        start = _time.monotonic()
        inv_id = await self.audit.log_start(
            source=ctx.source, instance=ctx.instance, eventid=ctx.eventid,
            ticket_id=ctx.ticket_id, customer_id=ctx.customer_id,
            hostid=ctx.hostid, hostname=ctx.hostname, model=self.model,
        )
        yield {"event": "started", "data": {"investigation_id": inv_id,
                                             "model": self.model}}

        system_blocks = build_cached_system_blocks(
            SYSTEM_PROMPT, claude_tool_definitions(), ctx.host_inventory_summary,
        )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._render_user_prompt(ctx)},
        ]
        tool_calls = 0
        tokens_in = tokens_out = 0
        final_text = ""
        budget_exhausted = False

        while True:
            resp = await self.claude.create(
                model=self.model, system=system_blocks,
                tools=claude_tool_definitions(),
                messages=messages, max_tokens=2048,
            )
            tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
            tokens_out += getattr(resp.usage, "output_tokens", 0) or 0

            text_chunks = [getattr(b, "text", "") for b in resp.content
                           if getattr(b, "type", None) == "text"]
            if text_chunks:
                yield {"event": "thinking",
                       "data": {"text": "\n".join(text_chunks)}}

            if resp.stop_reason == "end_turn" or budget_exhausted:
                final_text = "\n".join(text_chunks).strip()
                break

            if tool_calls >= self.max_tool_calls:
                messages.append({"role": "user",
                                 "content": "Tool budget exhausted. "
                                            "Produce final summary now."})
                budget_exhausted = True
                continue

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls += 1
                yield {"event": "tool_call",
                       "data": {"name": block.name,
                                "input": block.input or {},
                                "tool_use_id": block.id}}
                try:
                    output = await dispatch(block.name, block.input or {},
                                             context={"clients": self.clients,
                                                      "investigation_id": inv_id})
                    await self.audit.log_tool(inv_id, block.name,
                                              block.input or {}, output)
                    yield {"event": "tool_result",
                           "data": {"tool_use_id": block.id,
                                    "output": str(output)[:8000],
                                    "is_error": False}}
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": str(output)[:8000]})
                except Exception as e:  # noqa: BLE001
                    msg = f"ERROR: {e}"
                    await self.audit.log_tool(inv_id, block.name,
                                              block.input or {}, msg)
                    yield {"event": "tool_result",
                           "data": {"tool_use_id": block.id,
                                    "output": msg, "is_error": True}}
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": msg, "is_error": True})
            messages.append({"role": "user", "content": tool_results})

        duration_ms = int((_time.monotonic() - start) * 1000)
        await self.audit.log_end(
            inv_id, summary=final_text, duration_ms=duration_ms,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
        yield {"event": "final",
               "data": {"investigation_id": inv_id,
                        "summary": final_text,
                        "tool_calls": tool_calls,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "duration_ms": duration_ms}}
```

The needed imports are already present at the top of `orchestrator.py` (`build_cached_system_blocks`, `SYSTEM_PROMPT`, `dispatch`, `claude_tool_definitions`, `Any`).

- [ ] **Step 4: Run tests — expect 2 PASS**

---

## Task 4: Zabbix UI settings in config

**Files:**
- Modify: `zabbix_ai/config.py` (new `ZabbixUiSettings` model)
- Modify: `config.example.yaml`
- Modify: `.env.example`
- Test: extend `tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_config.py`:

```python
def test_zabbix_ui_settings_loaded(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
zabbix_ui:
  signing_key_env: URL_SIGNING_KEY
  link_ttl_seconds: 600
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("URL_SIGNING_KEY", "this-is-a-32-byte-key-or-more-pls")
    s = load_settings(cfg)
    assert s.zabbix_ui is not None
    assert s.zabbix_ui.signing_key.get_secret_value().startswith("this-is")
    assert s.zabbix_ui.link_ttl_seconds == 600


def test_zabbix_ui_section_optional(tmp_path, monkeypatch):
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
    assert load_settings(cfg).zabbix_ui is None


def test_zabbix_ui_missing_key_env_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
zabbix_ui:
  signing_key_env: NOPE_KEY
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(ValueError, match="NOPE_KEY"):
        load_settings(cfg)
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Modify `zabbix_ai/config.py`**

Add `ZabbixUiSettings` model alongside `SlackSettings`:

```python
class ZabbixUiSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    signing_key_env: str
    link_ttl_seconds: int = 300
    signing_key: SecretStr = SecretStr("")
```

Add to `Settings`:

```python
    zabbix_ui: ZabbixUiSettings | None = None
```

In `load_settings`, after Slack resolution:

```python
    if s.zabbix_ui is not None:
        key = os.environ.get(s.zabbix_ui.signing_key_env)
        if not key:
            raise ValueError(f"{s.zabbix_ui.signing_key_env} not set in environment")
        s.zabbix_ui.signing_key = SecretStr(key)
```

- [ ] **Step 4: Update `config.example.yaml`** — append:

```yaml

# Optional — enables the Zabbix UI right-click adapter.
zabbix_ui:
  signing_key_env: URL_SIGNING_KEY
  link_ttl_seconds: 300        # how long a right-click URL is valid
```

- [ ] **Step 5: Update `.env.example`** — append:

```
URL_SIGNING_KEY=replace-with-32-bytes-of-random-data
```

- [ ] **Step 6: Run tests — expect 3 new PASS (11 total in test_config.py)**

---

## Task 5: HTML renderer + template

**Files:**
- Create: `zabbix_ai/templates/investigate.html`
- Create: `zabbix_ai/renderers/html.py`
- Test: `tests/unit/test_html_renderer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_html_renderer.py
from zabbix_ai.renderers.html import render_investigate_page

def test_page_includes_eventid_and_instance():
    page = render_investigate_page(eventid=998877, instance="monitoring",
                                    sse_path="/investigate/stream?token=abc")
    assert "998877" in page
    assert "monitoring" in page
    assert "/investigate/stream?token=abc" in page

def test_page_escapes_dangerous_eventid():
    bad = "</script><script>alert(1)</script>"
    page = render_investigate_page(eventid=bad, instance="monitoring",
                                    sse_path="/x")
    assert "<script>alert(1)</script>" not in page

def test_page_has_sse_consumer_js():
    page = render_investigate_page(eventid=1, instance="monitoring", sse_path="/x")
    assert "EventSource" in page
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Create `zabbix_ai/templates/investigate.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Zabbix RCA AI — investigation</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif;
           margin: 0; background: #0f1115; color: #e6e8ee; }
    header { padding: 14px 20px; background: #1a1d24;
             border-bottom: 1px solid #2a2f3a; }
    header h1 { font-size: 16px; margin: 0 0 4px 0; }
    header .meta { font-size: 12px; color: #8a93a6; }
    main { padding: 16px 20px; max-width: 980px; }
    .event { margin: 8px 0; padding: 10px 12px; background: #1a1d24;
             border: 1px solid #2a2f3a; border-radius: 6px; font-size: 13px; }
    .event h3 { margin: 0 0 6px 0; font-size: 12px; text-transform: uppercase;
                letter-spacing: 0.04em; color: #8a93a6; }
    .event pre { white-space: pre-wrap; margin: 0; font-size: 12px;
                 color: #c8d0df; }
    .event.tool_call { border-left: 3px solid #4f8cf7; }
    .event.tool_result { border-left: 3px solid #6dd58c; }
    .event.tool_result.error { border-left-color: #f78b8b; }
    .event.final { border-left: 3px solid #ffba73;
                   background: #1d2230; }
    .event.thinking { color: #8a93a6; font-style: italic; }
    .footer { font-size: 12px; color: #8a93a6;
              padding: 14px 20px; border-top: 1px solid #2a2f3a; }
  </style>
</head>
<body>
  <header>
    <h1>🔎 Investigating event {{ eventid }}</h1>
    <div class="meta">Instance: {{ instance }}</div>
  </header>
  <main id="events"></main>
  <div class="footer">Read-only — the AI cannot modify state on any host.</div>
  <script>
    (function () {
      const events = document.getElementById('events');
      const src = new EventSource({{ sse_path | tojson }});

      function append(kind, body) {
        const div = document.createElement('div');
        div.className = 'event ' + kind;
        const h = document.createElement('h3');
        h.textContent = kind.replace('_', ' ');
        div.appendChild(h);
        const pre = document.createElement('pre');
        pre.textContent = body;
        div.appendChild(pre);
        events.appendChild(div);
        window.scrollTo(0, document.body.scrollHeight);
      }

      ['started', 'tool_call', 'tool_result', 'thinking', 'final'].forEach(name => {
        src.addEventListener(name, e => {
          let body = e.data;
          try {
            const parsed = JSON.parse(e.data);
            body = JSON.stringify(parsed, null, 2);
            if (name === 'tool_result' && parsed.is_error) {
              append(name + ' error', body);
              return;
            }
          } catch (_) { /* keep as text */ }
          append(name, body);
        });
      });

      src.addEventListener('error', () => { src.close(); });
      src.addEventListener('final', () => { src.close(); });
    })();
  </script>
</body>
</html>
```

- [ ] **Step 4: Create `zabbix_ai/renderers/html.py`**

```python
from __future__ import annotations
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

def render_investigate_page(*, eventid: int | str, instance: str,
                            sse_path: str) -> str:
    tmpl = _env.get_template("investigate.html")
    return tmpl.render(eventid=eventid, instance=instance, sse_path=sse_path)
```

Hatchling needs to know about template files. Update `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel]
packages = ["zabbix_ai"]

[tool.hatch.build.targets.wheel.force-include]
"zabbix_ai/templates" = "zabbix_ai/templates"
```

- [ ] **Step 5: Run tests — expect 3 PASS**

---

## Task 6: Zabbix UI adapter (route + SSE)

**Files:**
- Create: `zabbix_ai/adapters/zabbix_ui.py`
- Modify: `zabbix_ai/app.py` (mount router when settings.zabbix_ui present)
- Test: `tests/integration/test_zabbix_ui_e2e.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_zabbix_ui_e2e.py
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from zabbix_ai.app import create_app
from zabbix_ai.config import load_settings
from zabbix_ai.url_signing import sign_url_token

class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)

def _claude(stop_reason, blocks, in_t=10, out_t=5):
    return MagicMock(stop_reason=stop_reason, content=blocks,
                     usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))

@pytest.fixture
def app(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
zabbix_instances:
  - name: monitoring
    url: https://zbx.test
    token_env: ZBX_TOK
zabbix_ui:
  signing_key_env: URL_SIGNING_KEY
  link_ttl_seconds: 60
sqlite_path: {tmp_path / 'state.db'}
default_model: m
summary_model: h
max_tool_calls: 4
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ZBX_TOK", "tok")
    monkeypatch.setenv("URL_SIGNING_KEY", "test-key-32-bytes-or-more-please-pad")
    return create_app(settings=load_settings(cfg))

def test_investigate_page_serves_html(app):
    settings_key = "test-key-32-bytes-or-more-please-pad"
    token = sign_url_token({"eventid": 998877, "instance": "monitoring"},
                           ttl_seconds=60, signing_key=settings_key)
    client = TestClient(app)
    r = client.get(f"/investigate?token={token}")
    assert r.status_code == 200
    assert "998877" in r.text
    assert "monitoring" in r.text
    assert "EventSource" in r.text

def test_investigate_page_rejects_invalid_token(app):
    client = TestClient(app)
    r = client.get("/investigate?token=garbage")
    assert r.status_code == 401

def test_stream_emits_started_and_final_events(app):
    settings_key = "test-key-32-bytes-or-more-please-pad"
    token = sign_url_token({"eventid": 998877, "instance": "monitoring"},
                           ttl_seconds=60, signing_key=settings_key)
    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as MockA:
        MockA.return_value.messages.create = AsyncMock(side_effect=[
            _claude("end_turn", [_Block(type="text", text="all good")]),
        ])
        client = TestClient(app)
        with client.stream("GET", f"/investigate/stream?token={token}") as r:
            assert r.status_code == 200
            body = b"".join(r.iter_bytes()).decode()
    assert "event: started" in body
    assert "event: final" in body
    assert "all good" in body

def test_stream_rejects_invalid_token(app):
    client = TestClient(app)
    r = client.get("/investigate/stream?token=bad")
    assert r.status_code == 401
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Create `zabbix_ai/adapters/zabbix_ui.py`**

```python
from __future__ import annotations
import json
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse
from zabbix_ai.config import Settings
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.renderers.html import render_investigate_page
from zabbix_ai.services.investigation_runner import InvestigationRunner
from zabbix_ai.url_signing import UrlSignatureError, verify_url_token


def build_router(settings: Settings) -> APIRouter:
    if settings.zabbix_ui is None:
        raise RuntimeError("build_router called without zabbix_ui settings")
    signing_key = settings.zabbix_ui.signing_key.get_secret_value()
    router = APIRouter()

    def _verify(token: str) -> dict[str, Any]:
        try:
            return verify_url_token(token, signing_key=signing_key)
        except UrlSignatureError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

    @router.get("/investigate", response_class=HTMLResponse)
    async def page(token: str = "") -> HTMLResponse:
        payload = _verify(token)
        eventid = payload.get("eventid", "")
        instance = payload.get("instance", "")
        sse_path = f"/investigate/stream?token={token}"
        return HTMLResponse(render_investigate_page(
            eventid=eventid, instance=instance, sse_path=sse_path,
        ))

    @router.get("/investigate/stream")
    async def stream(token: str = "") -> EventSourceResponse:
        payload = _verify(token)
        eventid = payload.get("eventid")
        instance = payload.get("instance", "")
        hostid = payload.get("hostid")

        async def event_gen():
            async with InvestigationRunner(settings) as runner:
                ctx = InvestigationContext(
                    source="zabbix_ui",
                    instance=instance,
                    eventid=int(eventid) if eventid is not None else None,
                    hostid=int(hostid) if hostid is not None else None,
                )
                async for ev in runner.investigate_streaming(ctx):
                    yield {"event": ev["event"],
                           "data": json.dumps(ev["data"], default=str)}

        return EventSourceResponse(event_gen())

    return router
```

- [ ] **Step 4: Add `investigate_streaming` to `InvestigationRunner`**

In `zabbix_ai/services/investigation_runner.py`, add:

```python
    def investigate_streaming(self, ctx: "InvestigationContext"):
        if not self._orch:
            raise RuntimeError("InvestigationRunner not entered")
        return self._orch.investigate_streaming(ctx)
```

(`InvestigationContext` is already imported at the top of that file.)

- [ ] **Step 5: Modify `zabbix_ai/app.py`**

In `create_app`, after the Slack mount block, add:

```python
    if settings is not None and settings.zabbix_ui is not None:
        from zabbix_ai.adapters.zabbix_ui import build_router as build_ui_router
        app.include_router(build_ui_router(settings))
```

- [ ] **Step 6: Run tests — expect 4 PASS**

---

## Task 7: Frontend Script ops doc + README

**Files:**
- Create: `deploy/zabbix-frontend-script.md`
- Modify: `README.md`

- [ ] **Step 1: Create `deploy/zabbix-frontend-script.md`**

```markdown
# Wiring "Investigate with AI" into Zabbix UI

Zabbix can attach a custom right-click action to any problem via Frontend
Scripts. We use one of type **URL** that opens a signed link to the AI service.

## Requirement

- Token generation must happen server-side (signing key never goes to the
  browser). Two patterns work:
  1. (Recommended) Token signing endpoint on the AI service. Zabbix sends
     `eventid` and `instance` to a small signer route that returns the URL.
  2. (Lighter, used here) Generate the token in a tiny PHP wrapper colocated
     with Zabbix that has the signing key in its environment, then redirect.
     We do this in v0.4 because it requires no extra service call.

## Step-by-step

1. On the Zabbix server, create
   `/usr/share/zabbix/frontend-script-rca-ai.php`:

   ```php
   <?php
   $key = getenv('URL_SIGNING_KEY');
   if (!$key) { http_response_code(500); exit('signing key missing'); }
   $eventid = (int)($_GET['eventid'] ?? 0);
   $instance = preg_replace('/[^a-z0-9_-]/i', '', $_GET['instance'] ?? '');
   $payload = json_encode(['eventid' => $eventid, 'instance' => $instance],
                          JSON_UNESCAPED_SLASHES);
   $exp = time() + 300;
   $b64 = function ($s) {
       return rtrim(strtr(base64_encode($s), '+/', '-_'), '=');
   };
   $payload_p = $b64($payload);
   $exp_p = $b64((string)$exp);
   $sig = hash_hmac('sha256', "$payload_p.$exp_p", $key, true);
   $sig_p = $b64($sig);
   header('Location: https://zabbix-ai.internal/investigate?token=' .
          "$payload_p.$exp_p.$sig_p");
   ```

   `chmod 0640`, owner `www-data:www-data`. Make sure the Apache/nginx
   environment has `URL_SIGNING_KEY` set (matches the AI service's env).

2. In Zabbix UI: **Configure → Scripts → Create script**:
   - Name: `Investigate with AI`
   - Scope: `Manual event action`
   - Type: `URL`
   - URL:
     `/frontend-script-rca-ai.php?eventid={EVENT.ID}&instance=monitoring`
   - Permissions: limit to NOC user groups

3. Save. The action now appears on the right-click menu of any problem in
   the Problems view.

## Alternative without a PHP wrapper

If you don't want a PHP wrapper, expose `/sign?eventid=…&instance=…` on
the AI service behind IP-restricted auth and have a tiny shell script do
the curl. Same trust model — signing key still server-side.
```

- [ ] **Step 2: Update `README.md`**

After the Slack section, before "Deploy agent UserParameters", insert:

```markdown
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
```

Mark `v0.4` complete in the Roadmap.

---

## Task 8: Final test pass + commit + tag

- [ ] **Step 1: Run full suite**

```bash
. .venv/bin/activate
pytest -v
ruff check zabbix_ai tests
```

Fix any issues. Expected: ~70 tests pass.

- [ ] **Step 2: Bump version**

```bash
sed -i 's/__version__ = "0.3.0"/__version__ = "0.4.0"/' zabbix_ai/__init__.py
sed -i 's/version = "0.3.0"/version = "0.4.0"/' pyproject.toml
```

- [ ] **Step 3: One commit, one tag**

```bash
git add -A
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(v0.4): Zabbix UI right-click adapter with SSE-streamed investigation

Adds /investigate (HTML page) and /investigate/stream (SSE) routes,
HMAC-signed URL tokens with TTL, streaming orchestrator method that
yields tool_call/tool_result/thinking/final events as the loop runs,
Jinja2 page template with a small EventSource consumer, ZabbixUiSettings
in config, and an ops doc for wiring the Zabbix Frontend Script.
Version bumped to 0.4.0.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git tag v0.4.0
```

---

## Self-Review Notes

- **Spec coverage** (against design §9.2): ✓ HMAC URL signing, ✓ HTML page + SSE, ✓ tool_call/tool_result/thinking/final events, ✓ Frontend Script wiring docs.
- **Type consistency**: `verify_url_token` / `sign_url_token` shared by adapter and frontend wrapper. `InvestigationContext`, `InvestigationResult` reused. `InvestigationRunner` exposes both `investigate` and `investigate_streaming`.
- **No placeholders.** Every step has exact code.
- **Deferred to a later plan**: a "Post to Slack" button on the result page (cross-adapter), a `/sign` endpoint as alternative to the PHP wrapper, real-time auth via Zabbix session cookie.
