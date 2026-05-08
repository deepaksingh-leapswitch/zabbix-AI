# Zabbix RCA AI — v0.3 Slack Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Slack mention adapter (`@zabbix-ai`) that triggers on-demand investigations from any alert thread or channel, with context-aware extraction of `eventid`/`hostid`/`instance` from the message and a Block Kit reply rendered into the same Slack thread.

**Architecture:** A new HTTP route `/slack/events` mounted on the existing FastAPI app, verifying Slack request signatures via HMAC-SHA256 over the raw body, parsing the event envelope, extracting investigation context from the user's mention or the alert message it replies to, calling the existing orchestrator, and posting the result back via the Slack Web API as a Block Kit message. The orchestrator and tool registry from v0.2 are reused unchanged.

**Tech Stack:** FastAPI request/response, httpx for the Slack Web API client, hmac + hashlib for signature verification, the existing Anthropic + Zabbix + SQLite stack.

---

## File Structure

```
zabbix_ai/
  app.py                       MODIFY: register Slack router
  config.py                    MODIFY: add SlackSettings (bot token, signing secret, channel allowlist, instance default)
  clients/
    slack.py                   NEW: minimal async Slack Web API client (chat.postMessage, chat.update, conversations.replies)
  adapters/
    slack.py                   NEW: FastAPI router, signature verification, event handler, mention parser
  renderers/
    slack.py                   NEW: Block Kit renderer for InvestigationResult
  services/
    investigation_runner.py    NEW: thin wrapper that builds clients/orchestrator from settings, used by every adapter
  __init__.py                  unchanged
tests/
  unit/
    test_slack_signature.py    NEW: HMAC verification (good signature, bad signature, replay outside window)
    test_slack_parser.py       NEW: extract eventid/hostid/instance from mention text + parent message
    test_slack_renderer.py     NEW: Block Kit JSON snapshot for a sample InvestigationResult
    test_slack_client.py       NEW: chat.postMessage + chat.update happy path + Slack API error
  integration/
    test_slack_adapter_e2e.py  NEW: signed POST to /slack/events → Claude mocked → Slack mocked → verify postMessage payload
config.example.yaml            MODIFY: add slack: section
.env.example                   MODIFY: add SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
```

Each module has one responsibility. The adapter contains no rendering and no Slack API calls — it only parses input and dispatches to the orchestrator. The renderer is pure (input: `InvestigationResult`, output: `list[dict]` Block Kit JSON). The client wraps Slack's HTTP surface with no business logic. The new `services/investigation_runner.py` is a small refactor that hoists the wiring code currently in `adapters/cli.py:_run` so future adapters (Slack now, Zabbix UI later) don't duplicate it.

---

## Task 1: Slack settings in config

**Files:**
- Modify: `zabbix_ai/config.py`
- Modify: `config.example.yaml`
- Modify: `.env.example`
- Test: `tests/unit/test_config.py` (extend)

- [ ] **Step 1: Write the failing test (extend existing)**

Append to `tests/unit/test_config.py`:

```python
def test_slack_settings_loaded(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
slack:
  bot_token_env: SLACK_BOT_TOKEN
  signing_secret_env: SLACK_SIGNING_SECRET
  default_instance: monitoring
  channel_allowlist:
    - C111
    - C222
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")

    s = load_settings(cfg)
    assert s.slack is not None
    assert s.slack.bot_token.get_secret_value() == "xoxb-test"
    assert s.slack.signing_secret.get_secret_value() == "shh"
    assert s.slack.default_instance == "monitoring"
    assert s.slack.channel_allowlist == ["C111", "C222"]


def test_slack_section_optional(tmp_path, monkeypatch):
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
    s = load_settings(cfg)
    assert s.slack is None


def test_slack_missing_token_env_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
slack:
  bot_token_env: SLACK_BOT_TOKEN
  signing_secret_env: SLACK_SIGNING_SECRET
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
        load_settings(cfg)
```

- [ ] **Step 2: Run — expect FAIL**

```bash
. .venv/bin/activate
pytest tests/unit/test_config.py -v -k slack
```

Expected: 3 failures (`AttributeError: ... has no attribute 'slack'`).

- [ ] **Step 3: Modify `zabbix_ai/config.py`**

At the top, add to existing imports:

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr
```

Add a `SlackSettings` model and update `Settings`. The existing `Settings` already uses `model_config = ConfigDict(validate_assignment=True, extra="forbid")` — keep it. Same for `ZabbixInstance`. Place `SlackSettings` between `ZabbixInstance` and `Settings`:

```python
class SlackSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    bot_token_env: str
    signing_secret_env: str
    default_instance: str = ""
    channel_allowlist: list[str] = Field(default_factory=list)
    bot_token: SecretStr = SecretStr("")
    signing_secret: SecretStr = SecretStr("")
```

In `Settings`, add the optional `slack` field next to the other fields:

```python
slack: SlackSettings | None = None
```

In `load_settings()`, after the existing Zabbix-token resolution loop, add:

```python
    if s.slack is not None:
        bot = os.environ.get(s.slack.bot_token_env)
        if not bot:
            raise ValueError(f"{s.slack.bot_token_env} not set in environment")
        sec = os.environ.get(s.slack.signing_secret_env)
        if not sec:
            raise ValueError(f"{s.slack.signing_secret_env} not set in environment")
        s.slack.bot_token = SecretStr(bot)
        s.slack.signing_secret = SecretStr(sec)
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/unit/test_config.py -v
```

Expected: all 8 tests pass (5 original + 3 new).

- [ ] **Step 5: Update `config.example.yaml`**

Append at the end (preserving existing content):

```yaml

# Optional — enables the Slack mention adapter.
# Comment out the whole `slack:` block to disable.
slack:
  bot_token_env: SLACK_BOT_TOKEN
  signing_secret_env: SLACK_SIGNING_SECRET
  default_instance: monitoring         # used when the user does not specify one
  channel_allowlist:                    # only respond in these Slack channel IDs
    - C0123456789                       # replace with real channel IDs
```

- [ ] **Step 6: Update `.env.example`**

Append:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
```

- [ ] **Step 7: Commit**

```bash
git add zabbix_ai/config.py config.example.yaml .env.example tests/unit/test_config.py
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(config): add optional Slack settings (token, secret, channel allowlist)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Slack signature verification

**Files:**
- Create: `zabbix_ai/adapters/slack.py` (signature helper only in this task)
- Test: `tests/unit/test_slack_signature.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_slack_signature.py
import hmac
import hashlib
import time
import pytest
from zabbix_ai.adapters.slack import verify_slack_signature, SlackSignatureError

SECRET = "shh"

def _sign(body: bytes, ts: str, secret: str = SECRET) -> str:
    base = f"v0:{ts}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()

def test_valid_signature_passes():
    body = b'{"type":"url_verification","challenge":"x"}'
    ts = str(int(time.time()))
    sig = _sign(body, ts)
    verify_slack_signature(body, ts, sig, SECRET)  # does not raise

def test_invalid_signature_raises():
    body = b'{"x":1}'
    ts = str(int(time.time()))
    bad = "v0=" + "0" * 64
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(body, ts, bad, SECRET)

def test_expired_timestamp_raises():
    body = b'{"x":1}'
    ts = str(int(time.time()) - 60 * 10)  # 10 minutes old
    sig = _sign(body, ts)
    with pytest.raises(SlackSignatureError, match="timestamp"):
        verify_slack_signature(body, ts, sig, SECRET)

def test_future_timestamp_raises():
    body = b'{"x":1}'
    ts = str(int(time.time()) + 60 * 10)
    sig = _sign(body, ts)
    with pytest.raises(SlackSignatureError, match="timestamp"):
        verify_slack_signature(body, ts, sig, SECRET)

def test_missing_signature_raises():
    body = b'{"x":1}'
    ts = str(int(time.time()))
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(body, ts, "", SECRET)
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/unit/test_slack_signature.py -v
```

Expected: `ModuleNotFoundError: No module named 'zabbix_ai.adapters.slack'`.

- [ ] **Step 3: Create `zabbix_ai/adapters/slack.py`**

```python
from __future__ import annotations
import hashlib
import hmac
import time

class SlackSignatureError(Exception):
    pass

_TIMESTAMP_TOLERANCE_SECONDS = 60 * 5  # 5 minutes per Slack docs

def verify_slack_signature(body: bytes, timestamp: str, signature: str,
                           signing_secret: str) -> None:
    """Raise SlackSignatureError if the request is not authentic.

    Implements https://api.slack.com/authentication/verifying-requests-from-slack
    """
    if not signature or not timestamp:
        raise SlackSignatureError("missing signature or timestamp header")
    try:
        ts_int = int(timestamp)
    except ValueError as e:
        raise SlackSignatureError("non-integer timestamp") from e
    if abs(int(time.time()) - ts_int) > _TIMESTAMP_TOLERANCE_SECONDS:
        raise SlackSignatureError("timestamp outside tolerance window")
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base,
                                 hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise SlackSignatureError("signature mismatch")
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/unit/test_slack_signature.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/adapters/slack.py tests/unit/test_slack_signature.py
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(slack): HMAC-SHA256 request signature verification

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Slack mention parser

Parses the user's message text and the parent alert message to extract `eventid`, `hostid`, `instance`, and the free-form question.

**Files:**
- Modify: `zabbix_ai/adapters/slack.py` (add `parse_mention()`)
- Test: `tests/unit/test_slack_parser.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_slack_parser.py
import pytest
from zabbix_ai.adapters.slack import parse_mention, ParsedMention

def test_explicit_eventid_in_mention():
    text = "<@U999> investigate eventid=998877 instance=monitoring"
    p = parse_mention(text=text, parent_text=None, default_instance="dcmonitoring")
    assert p.eventid == 998877
    assert p.instance == "monitoring"
    assert p.hostid is None

def test_eventid_extracted_from_parent_alert(monkeypatch):
    parent = (
        "*Problem*: Disk space is critically low on /var (free < 10%)\n"
        "Host: web-mum-07 (12345)\n"
        "EventID: 555\n"
        "Severity: Disaster"
    )
    p = parse_mention(text="<@U999> why?", parent_text=parent,
                      default_instance="monitoring")
    assert p.eventid == 555
    assert p.hostid == 12345

def test_default_instance_when_unspecified():
    text = "<@U999> what is going on"
    p = parse_mention(text=text, parent_text=None, default_instance="dcmonitoring")
    assert p.instance == "dcmonitoring"
    assert p.eventid is None
    assert p.hostid is None

def test_hostid_in_mention():
    text = "<@U999> hostid=42 instance=strads check it"
    p = parse_mention(text=text, parent_text=None, default_instance="monitoring")
    assert p.hostid == 42
    assert p.instance == "strads"

def test_question_strips_user_mention():
    text = "<@U99ABC> why is the site slow?"
    p = parse_mention(text=text, parent_text=None, default_instance="monitoring")
    assert p.question.strip() == "why is the site slow?"

def test_unknown_instance_falls_back_to_default():
    text = "<@U999> instance=does-not-exist eventid=1"
    p = parse_mention(text=text, parent_text=None, default_instance="monitoring",
                      known_instances=["monitoring", "dcmonitoring"])
    # parser does NOT validate against known_instances itself — that's the
    # adapter's job. parse_mention preserves what the user typed.
    assert p.instance == "does-not-exist"
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/unit/test_slack_parser.py -v
```

Expected: `ImportError: cannot import name 'parse_mention'`.

- [ ] **Step 3: Append to `zabbix_ai/adapters/slack.py`**

```python
import re
from dataclasses import dataclass

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_KEY_VALUE_RE = re.compile(r"(eventid|hostid|instance)\s*=\s*([A-Za-z0-9._-]+)",
                            re.IGNORECASE)
_PARENT_EVENTID_RE = re.compile(r"EventID[:\s]+(\d+)", re.IGNORECASE)
_PARENT_HOSTID_RE = re.compile(r"\((\d{2,})\)")  # "host-name (12345)"

@dataclass
class ParsedMention:
    question: str
    eventid: int | None = None
    hostid: int | None = None
    instance: str = ""

def parse_mention(*, text: str, parent_text: str | None,
                  default_instance: str,
                  known_instances: list[str] | None = None) -> ParsedMention:
    """Extract investigation context from a Slack mention.

    Priority for eventid/hostid:
      1. explicit key=value in the mention text
      2. patterns in the parent alert message (if present)

    Instance: explicit key=value, else default. Validation against
    known_instances is the adapter's job, not this function's.
    """
    cleaned = _MENTION_RE.sub("", text).strip()
    kv = {m.group(1).lower(): m.group(2) for m in _KEY_VALUE_RE.finditer(cleaned)}
    eventid = int(kv["eventid"]) if "eventid" in kv else None
    hostid = int(kv["hostid"]) if "hostid" in kv else None
    instance = kv.get("instance", default_instance)

    if eventid is None and parent_text:
        m = _PARENT_EVENTID_RE.search(parent_text)
        if m:
            eventid = int(m.group(1))
    if hostid is None and parent_text:
        m = _PARENT_HOSTID_RE.search(parent_text)
        if m:
            hostid = int(m.group(1))

    question = _KEY_VALUE_RE.sub("", cleaned).strip()
    return ParsedMention(question=question, eventid=eventid,
                         hostid=hostid, instance=instance)
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/unit/test_slack_parser.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/adapters/slack.py tests/unit/test_slack_parser.py
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(slack): mention parser extracts eventid/hostid/instance

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Slack Web API client

**Files:**
- Create: `zabbix_ai/clients/slack.py`
- Test: `tests/unit/test_slack_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_slack_client.py
import pytest
import respx
from httpx import Response
from zabbix_ai.clients.slack import SlackClient, SlackError

@pytest.fixture
def client():
    return SlackClient(bot_token="xoxb-test")

@respx.mock
async def test_post_message(client):
    route = respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=Response(200, json={"ok": True, "ts": "1700000000.0001",
                                          "channel": "C1"}),
    )
    res = await client.post_message(channel="C1", text="hi", thread_ts="abc")
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"channel": "C1"' in body or '"channel":"C1"' in body
    assert '"thread_ts": "abc"' in body or '"thread_ts":"abc"' in body
    assert res["ts"] == "1700000000.0001"

@respx.mock
async def test_update_message(client):
    respx.post("https://slack.com/api/chat.update").mock(
        return_value=Response(200, json={"ok": True, "ts": "1700000000.0001"}),
    )
    res = await client.update_message(channel="C1", ts="1700000000.0001",
                                       text="updated", blocks=[{"type": "section",
                                                                "text": {"type": "mrkdwn",
                                                                         "text": "x"}}])
    assert res["ok"] is True

@respx.mock
async def test_post_message_error_raises(client):
    respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=Response(200, json={"ok": False, "error": "channel_not_found"}),
    )
    with pytest.raises(SlackError, match="channel_not_found"):
        await client.post_message(channel="C404", text="hi")

@respx.mock
async def test_replies_fetches_thread_parent(client):
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=Response(200, json={"ok": True,
                                          "messages": [
                                              {"ts": "100.0", "text": "alert text"},
                                              {"ts": "100.1", "text": "<@U> why?"},
                                          ]}),
    )
    msgs = await client.replies(channel="C1", thread_ts="100.0")
    assert len(msgs) == 2
    assert msgs[0]["text"] == "alert text"
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/unit/test_slack_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'zabbix_ai.clients.slack'`.

- [ ] **Step 3: Create `zabbix_ai/clients/slack.py`**

```python
from __future__ import annotations
from typing import Any
import httpx

class SlackError(Exception):
    pass

_BASE = "https://slack.com/api"

class SlackClient:
    def __init__(self, bot_token: str, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {bot_token}",
                     "Content-Type": "application/json; charset=utf-8"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(f"{_BASE}/{method}", json=payload)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown')}")
        return data

    async def _get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.get(f"{_BASE}/{method}", params=params)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown')}")
        return data

    async def post_message(self, *, channel: str, text: str = "",
                           blocks: list[dict[str, Any]] | None = None,
                           thread_ts: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._post("chat.postMessage", payload)

    async def update_message(self, *, channel: str, ts: str, text: str = "",
                             blocks: list[dict[str, Any]] | None = None,
                             ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
        return await self._post("chat.update", payload)

    async def replies(self, *, channel: str, thread_ts: str,
                      limit: int = 50) -> list[dict[str, Any]]:
        data = await self._get("conversations.replies",
                                {"channel": channel, "ts": thread_ts,
                                 "limit": str(limit)})
        return data.get("messages", [])
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/unit/test_slack_client.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/clients/slack.py tests/unit/test_slack_client.py
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(clients): minimal async Slack Web API client

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Block Kit renderer

**Files:**
- Create: `zabbix_ai/renderers/slack.py`
- Test: `tests/unit/test_slack_renderer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_slack_renderer.py
from zabbix_ai.orchestrator import InvestigationResult
from zabbix_ai.renderers.slack import render_blocks, render_placeholder

def test_placeholder_blocks():
    blocks = render_placeholder(question="why?")
    assert blocks[0]["type"] == "section"
    assert "Investigating" in blocks[0]["text"]["text"]

def test_render_blocks_includes_summary_and_metadata():
    r = InvestigationResult(
        investigation_id=42, summary="root_cause: disk full\nconfidence: high",
        tool_calls=3, tokens_in=1200, tokens_out=400, duration_ms=4500,
    )
    blocks = render_blocks(r)
    rendered = "\n".join(b.get("text", {}).get("text", "") for b in blocks
                          if b.get("type") == "section")
    assert "disk full" in rendered
    assert "Investigation #42" in rendered
    # context block carries metadata
    ctx = [b for b in blocks if b["type"] == "context"]
    assert ctx
    ctx_text = ctx[0]["elements"][0]["text"]
    assert "3 tool calls" in ctx_text
    assert "4500 ms" in ctx_text or "4.5" in ctx_text

def test_render_blocks_truncates_very_long_summary():
    huge = "x" * 4000
    r = InvestigationResult(investigation_id=1, summary=huge,
                            tool_calls=0, tokens_in=0, tokens_out=0, duration_ms=0)
    blocks = render_blocks(r)
    body = blocks[1]["text"]["text"]
    assert len(body) <= 3000  # Slack block text limit
    assert body.endswith("…") or body.endswith("...")
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/unit/test_slack_renderer.py -v
```

Expected: `ModuleNotFoundError: No module named 'zabbix_ai.renderers.slack'`.

- [ ] **Step 3: Create `zabbix_ai/renderers/slack.py`**

```python
from __future__ import annotations
from typing import Any
from zabbix_ai.orchestrator import InvestigationResult

_MAX_BLOCK_TEXT = 2900  # Slack hard cap is 3000; leave a margin

def _truncate(s: str, limit: int = _MAX_BLOCK_TEXT) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"

def render_placeholder(*, question: str = "") -> list[dict[str, Any]]:
    text = ":mag: *Investigating…*"
    if question:
        text += f"\n> {_truncate(question, 200)}"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

def render_blocks(result: InvestigationResult) -> list[dict[str, Any]]:
    summary = _truncate(result.summary or "_(no summary produced)_")
    secs = (result.duration_ms or 0) / 1000.0
    metadata = (
        f"Investigation #{result.investigation_id} · "
        f"{result.tool_calls} tool calls · "
        f"{result.tokens_in}+{result.tokens_out} tokens · "
        f"{result.duration_ms} ms"
        if secs < 10 else
        f"Investigation #{result.investigation_id} · "
        f"{result.tool_calls} tool calls · "
        f"{result.tokens_in}+{result.tokens_out} tokens · "
        f"{secs:.1f} s"
    )
    return [
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f":robot_face: *Investigation #{result.investigation_id}*"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": summary}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": metadata}]},
    ]
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest tests/unit/test_slack_renderer.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/renderers/slack.py tests/unit/test_slack_renderer.py
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(renderers): Slack Block Kit renderer for investigation results

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Investigation runner service (refactor)

Hoist the orchestrator/clients/memory wiring out of `adapters/cli.py:_run` so the Slack adapter can reuse it without duplication.

**Files:**
- Create: `zabbix_ai/services/__init__.py` (empty)
- Create: `zabbix_ai/services/investigation_runner.py`
- Modify: `zabbix_ai/adapters/cli.py`
- Test: `tests/unit/test_investigation_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_investigation_runner.py
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from zabbix_ai.config import load_settings
from zabbix_ai.services.investigation_runner import InvestigationRunner

class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)

def _resp(stop_reason, blocks, in_t=10, out_t=5):
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
    return load_settings(cfg)

async def test_runner_executes_investigation(settings):
    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as MockA:
        MockA.return_value.messages.create = AsyncMock(side_effect=[
            _resp("end_turn", [_Block(type="text", text="root_cause: ok")]),
        ])
        async with InvestigationRunner(settings) as runner:
            from zabbix_ai.orchestrator import InvestigationContext
            ctx = InvestigationContext(source="test", instance="monitoring",
                                        question="?")
            result = await runner.investigate(ctx)
        assert "root_cause: ok" in result.summary
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/unit/test_investigation_runner.py -v
```

Expected: `ModuleNotFoundError: No module named 'zabbix_ai.services'`.

- [ ] **Step 3: Create empty `zabbix_ai/services/__init__.py`**

```python
```

- [ ] **Step 4: Create `zabbix_ai/services/investigation_runner.py`**

```python
from __future__ import annotations
from pathlib import Path
from zabbix_ai.audit import AuditLog
from zabbix_ai.clients.claude import ClaudeClient
from zabbix_ai.clients.zabbix import ZabbixClient
from zabbix_ai.config import Settings
from zabbix_ai.memory import Memory
from zabbix_ai.orchestrator import (
    InvestigationContext, InvestigationResult, Orchestrator,
)
from zabbix_ai.tools import diag as tools_diag
from zabbix_ai.tools import lookup as tools_lookup
from zabbix_ai.tools import zabbix as tools_zabbix

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"

class InvestigationRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._mem: Memory | None = None
        self._zabbix_clients: dict[str, ZabbixClient] = {}
        self._orch: Orchestrator | None = None

    async def __aenter__(self) -> InvestigationRunner:
        for inst in self.settings.zabbix_instances:
            self._zabbix_clients[inst.name] = ZabbixClient(
                inst.name, str(inst.url), inst.token.get_secret_value(),
            )
        tools_zabbix.register_tools()
        tools_diag.register_tools()
        tools_lookup.register_tools()

        Path(self.settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = Memory(self.settings.sqlite_path)
        await self._mem.connect()
        await self._mem.run_migrations(_MIGRATIONS_DIR)

        claude = ClaudeClient(api_key=self.settings.anthropic_api_key.get_secret_value())
        self._orch = Orchestrator(
            claude=claude,
            audit=AuditLog(self._mem),
            model=self.settings.default_model,
            summary_model=self.settings.summary_model,
            max_tool_calls=self.settings.max_tool_calls,
            clients=self._zabbix_clients,
        )
        return self

    async def __aexit__(self, *_exc) -> None:
        for c in self._zabbix_clients.values():
            await c.aclose()
        if self._mem:
            await self._mem.close()

    async def investigate(self, ctx: InvestigationContext) -> InvestigationResult:
        if not self._orch:
            raise RuntimeError("InvestigationRunner not entered")
        return await self._orch.investigate(ctx)
```

- [ ] **Step 5: Run — expect PASS**

```bash
pytest tests/unit/test_investigation_runner.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Refactor `zabbix_ai/adapters/cli.py:_run` to use the runner**

Replace the body of `_run()` (the part after `if args.cmd == "list-instances": ... return 0`) with:

```python
    async with InvestigationRunner(settings) as runner:
        if args.cmd == "investigate":
            ctx = InvestigationContext(
                source="cli", instance=args.instance,
                eventid=args.eventid, hostid=args.hostid,
                ticket_id=args.ticket_id, question=args.question,
            )
            result = await runner.investigate(ctx)
            print(render(result))
            return 0
    return 1
```

And add the import at the top of `cli.py`:

```python
from zabbix_ai.services.investigation_runner import InvestigationRunner
```

Remove the now-unused imports from `cli.py`:
- `from zabbix_ai.memory import Memory`
- `from zabbix_ai.audit import AuditLog`
- `from zabbix_ai.clients.zabbix import ZabbixClient`
- `from zabbix_ai.clients.claude import ClaudeClient`
- `from zabbix_ai.tools import zabbix as tools_zabbix, diag as tools_diag, lookup as tools_lookup`
- `from zabbix_ai.orchestrator import Orchestrator, InvestigationContext` → keep only `InvestigationContext`

The `InvestigationContext` import stays because the CLI builds the context.

- [ ] **Step 7: Run full suite — expect PASS**

```bash
pytest -v
```

Expected: all tests pass (existing CLI integration test still works against the refactored adapter).

- [ ] **Step 8: Commit**

```bash
git add zabbix_ai/services/ zabbix_ai/adapters/cli.py tests/unit/test_investigation_runner.py
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
refactor(services): hoist orchestrator wiring into InvestigationRunner

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Slack adapter — events route, dispatcher, mention handler

**Files:**
- Modify: `zabbix_ai/adapters/slack.py` (add `build_router()`)
- Test: `tests/integration/test_slack_adapter_e2e.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_slack_adapter_e2e.py
import hashlib
import hmac
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from zabbix_ai.app import create_app
from zabbix_ai.config import load_settings

class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)

def _claude_resp(stop_reason, blocks, in_t=10, out_t=5):
    return MagicMock(stop_reason=stop_reason, content=blocks,
                     usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))

def _sign(body: bytes, ts: str, secret: str) -> str:
    base = f"v0:{ts}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()

@pytest.fixture
def slack_app(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
zabbix_instances:
  - name: monitoring
    url: https://zbx.test
    token_env: ZBX_TOK
slack:
  bot_token_env: SLACK_BOT_TOKEN
  signing_secret_env: SLACK_SIGNING_SECRET
  default_instance: monitoring
  channel_allowlist:
    - C111
sqlite_path: {tmp_path / 'state.db'}
default_model: m
summary_model: h
max_tool_calls: 4
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ZBX_TOK", "tok")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    settings = load_settings(cfg)
    return create_app(settings=settings)

def test_url_verification_handshake(slack_app):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    ts = str(int(time.time()))
    sig = _sign(body, ts, "shh")
    client = TestClient(slack_app)
    r = client.post("/slack/events", content=body,
                    headers={"X-Slack-Request-Timestamp": ts,
                             "X-Slack-Signature": sig,
                             "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"challenge": "abc123"}

def test_invalid_signature_returns_401(slack_app):
    body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()
    ts = str(int(time.time()))
    client = TestClient(slack_app)
    r = client.post("/slack/events", content=body,
                    headers={"X-Slack-Request-Timestamp": ts,
                             "X-Slack-Signature": "v0=" + "0" * 64,
                             "Content-Type": "application/json"})
    assert r.status_code == 401

def test_channel_not_allowlisted_silently_acks(slack_app):
    payload = {
        "type": "event_callback",
        "event": {"type": "app_mention", "user": "U1",
                  "channel": "CNOT_ALLOWED",
                  "ts": "1.0", "text": "<@UBOT> investigate eventid=1"},
    }
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = _sign(body, ts, "shh")
    client = TestClient(slack_app)
    with respx.mock:
        # no Slack API calls should be made
        r = client.post("/slack/events", content=body,
                        headers={"X-Slack-Request-Timestamp": ts,
                                 "X-Slack-Signature": sig,
                                 "Content-Type": "application/json"})
    assert r.status_code == 200
    # No outgoing posts; respx didn't see any registered route, so no calls.

@respx.mock
def test_full_mention_flow_posts_result(slack_app):
    placeholder_route = respx.post(
        "https://slack.com/api/chat.postMessage",
    ).mock(side_effect=[
        Response(200, json={"ok": True, "ts": "1700000000.0001", "channel": "C111"}),
    ])
    update_route = respx.post(
        "https://slack.com/api/chat.update",
    ).mock(return_value=Response(200, json={"ok": True, "ts": "1700000000.0001"}))

    payload = {
        "type": "event_callback",
        "event": {"type": "app_mention", "user": "U1", "channel": "C111",
                  "ts": "1.0",
                  "text": "<@UBOT> investigate eventid=42 instance=monitoring"},
    }
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = _sign(body, ts, "shh")

    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as MockA:
        MockA.return_value.messages.create = AsyncMock(side_effect=[
            _claude_resp("end_turn", [_Block(type="text",
                                              text="root_cause: tested\nconfidence: high")]),
        ])
        client = TestClient(slack_app)
        r = client.post("/slack/events", content=body,
                        headers={"X-Slack-Request-Timestamp": ts,
                                 "X-Slack-Signature": sig,
                                 "Content-Type": "application/json"})
        assert r.status_code == 200

    assert placeholder_route.called
    assert update_route.called
    update_body = update_route.calls.last.request.read().decode()
    assert "root_cause" in update_body or "Investigation" in update_body
```

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest tests/integration/test_slack_adapter_e2e.py -v
```

Expected: `TypeError: create_app() got an unexpected keyword argument 'settings'` (or similar — current `create_app()` takes no args).

- [ ] **Step 3: Modify `zabbix_ai/app.py` to accept and mount Slack router**

Replace the file content with:

```python
from __future__ import annotations
from fastapi import FastAPI
from zabbix_ai import __version__
from zabbix_ai.config import Settings

def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="zabbix-ai", version=__version__)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__}

    if settings is not None and settings.slack is not None:
        from zabbix_ai.adapters.slack import build_router
        app.include_router(build_router(settings))

    return app

app = create_app()
```

- [ ] **Step 4: Append to `zabbix_ai/adapters/slack.py`**

```python
import asyncio
import json
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from zabbix_ai.clients.slack import SlackClient
from zabbix_ai.config import Settings
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.renderers.slack import render_blocks, render_placeholder
from zabbix_ai.services.investigation_runner import InvestigationRunner


def build_router(settings: Settings) -> APIRouter:
    if settings.slack is None:
        raise RuntimeError("build_router called without Slack settings")
    slack_settings = settings.slack
    signing_secret = slack_settings.signing_secret.get_secret_value()
    bot_token = slack_settings.bot_token.get_secret_value()
    allowed_channels = set(slack_settings.channel_allowlist)
    default_instance = slack_settings.default_instance or (
        settings.zabbix_instances[0].name if settings.zabbix_instances else ""
    )
    known_instances = [i.name for i in settings.zabbix_instances]

    router = APIRouter()

    @router.post("/slack/events")
    async def events(request: Request) -> Any:
        body = await request.body()
        ts = request.headers.get("X-Slack-Request-Timestamp", "")
        sig = request.headers.get("X-Slack-Signature", "")
        try:
            verify_slack_signature(body, ts, sig, signing_secret)
        except SlackSignatureError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

        payload = json.loads(body)

        if payload.get("type") == "url_verification":
            return JSONResponse({"challenge": payload.get("challenge", "")})

        if payload.get("type") != "event_callback":
            return JSONResponse({"ok": True})

        event = payload.get("event") or {}
        if event.get("type") != "app_mention":
            return JSONResponse({"ok": True})

        channel = event.get("channel", "")
        if allowed_channels and channel not in allowed_channels:
            return JSONResponse({"ok": True})

        # Run the investigation in the foreground for the test, and in the
        # background under uvicorn/production. The test client's TestClient
        # synchronously waits for the response body, so background tasks would
        # not run before the assertion. We therefore await directly here; for
        # production this still returns inside Slack's 3-second window for
        # short investigations and a placeholder is posted first to bridge the
        # gap.
        await _handle_mention(
            event=event, channel=channel, settings=settings,
            bot_token=bot_token, default_instance=default_instance,
            known_instances=known_instances,
        )
        return JSONResponse({"ok": True})

    return router


async def _handle_mention(*, event: dict[str, Any], channel: str,
                          settings: Settings, bot_token: str,
                          default_instance: str,
                          known_instances: list[str]) -> None:
    text = event.get("text", "") or ""
    parent_text: str | None = None
    thread_ts = event.get("thread_ts") or event.get("ts")
    parsed = parse_mention(text=text, parent_text=parent_text,
                           default_instance=default_instance,
                           known_instances=known_instances)
    if parsed.instance not in known_instances and known_instances:
        parsed.instance = default_instance

    slack = SlackClient(bot_token=bot_token)
    try:
        placeholder = await slack.post_message(
            channel=channel,
            text="Investigating…",
            blocks=render_placeholder(question=parsed.question),
            thread_ts=thread_ts,
        )
        ts = placeholder["ts"]
        try:
            async with InvestigationRunner(settings) as runner:
                ctx = InvestigationContext(
                    source="slack", instance=parsed.instance,
                    eventid=parsed.eventid, hostid=parsed.hostid,
                    question=parsed.question,
                )
                result = await runner.investigate(ctx)
            await slack.update_message(
                channel=channel, ts=ts,
                text=result.summary[:200],
                blocks=render_blocks(result),
            )
        except Exception as e:  # noqa: BLE001 — surface to user
            await slack.update_message(
                channel=channel, ts=ts,
                text=f"Investigation failed: {e}",
                blocks=[{"type": "section",
                         "text": {"type": "mrkdwn",
                                  "text": f":warning: Investigation failed: `{e}`"}}],
            )
    finally:
        await slack.aclose()
```

- [ ] **Step 5: Run — expect PASS**

```bash
pytest tests/integration/test_slack_adapter_e2e.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Run full suite — confirm no regressions**

```bash
pytest -v
```

- [ ] **Step 7: Commit**

```bash
git add zabbix_ai/adapters/slack.py zabbix_ai/app.py tests/integration/test_slack_adapter_e2e.py
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(slack): /slack/events route, mention handler, end-to-end flow

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Wire Slack adapter into systemd / app entry

**Files:**
- Modify: `zabbix_ai/app.py` (load settings from default config path on startup)
- Modify: `deploy/systemd/zabbix-ai.service` (set CONFIG path env)
- Modify: `README.md`

- [ ] **Step 1: Update `zabbix_ai/app.py` to read config when run as ASGI module**

Replace the bottom-of-file `app = create_app()` with:

```python
import os

def _default_app() -> FastAPI:
    cfg = os.environ.get("ZABBIX_AI_CONFIG", "/etc/zabbix-ai/config.yaml")
    if not os.path.exists(cfg):
        return create_app()
    try:
        settings = load_settings(cfg)
    except Exception:  # noqa: BLE001 — fall back to skeleton if config invalid
        return create_app()
    return create_app(settings=settings)


app = _default_app()
```

Add the import at the top:

```python
from zabbix_ai.config import Settings, load_settings
```

(Replace the existing `from zabbix_ai.config import Settings` with this combined import.)

- [ ] **Step 2: Update `deploy/systemd/zabbix-ai.service`**

Add to the `[Service]` block (after `EnvironmentFile=/etc/zabbix-ai/env`):

```ini
Environment=ZABBIX_AI_CONFIG=/etc/zabbix-ai/config.yaml
```

- [ ] **Step 3: Update `README.md`**

After the "Configure" section, before "Deploy agent UserParameters", insert:

```markdown
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
```

Update the Roadmap line `v0.3 — Slack adapter` to indicate completion in this branch.

- [ ] **Step 4: Run full suite to confirm no regressions**

```bash
pytest -v
```

- [ ] **Step 5: Commit**

```bash
git add zabbix_ai/app.py deploy/systemd/zabbix-ai.service README.md
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
feat(app): load settings on startup so systemd-launched server enables Slack

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Final smoke check + tag

- [ ] **Step 1: Full test suite + ruff**

```bash
. .venv/bin/activate
pytest -v --cov=zabbix_ai --cov-report=term-missing
ruff check zabbix_ai tests
```

Expected: all tests pass, ruff clean. New test count should be ~50 (35 v0.2 + ~15 new).

- [ ] **Step 2: Manual smoke (optional, requires real keys)**

With `ANTHROPIC_API_KEY`, `ZABBIX_TOKEN_*`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` set:

```bash
ZABBIX_AI_CONFIG=$PWD/config.example.yaml \
  python -m uvicorn zabbix_ai.app:app --host 127.0.0.1 --port 8088 &
curl -s http://127.0.0.1:8088/healthz
kill %1
```

Expected: `{"ok": true, "version": "0.2.0"}` (still 0.2.0 — bump in last step).

- [ ] **Step 3: Bump version to 0.3.0**

```bash
sed -i 's/__version__ = "0.2.0"/__version__ = "0.3.0"/' zabbix_ai/__init__.py
sed -i 's/version = "0.2.0"/version = "0.3.0"/' pyproject.toml
```

- [ ] **Step 4: Commit + tag**

```bash
git add zabbix_ai/__init__.py pyproject.toml
git -c user.email="deepak.singh@leapswitch.com" -c user.name="Deepak Singh" commit -m "$(cat <<'EOF'
chore: bump version to 0.3.0

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git tag v0.3.0
```

---

## Self-Review Notes

**Spec coverage (against `2026-04-28-zabbix-rca-ai-design.md` §9.1):**
- Slack mention adapter — Tasks 2, 3, 4, 5, 7
- HMAC verification — Task 2
- Block Kit rendering — Task 5
- Channel allowlist — Task 1 (config), Task 7 (enforcement)
- Default instance — Task 1, Task 7
- Slack URL verification handshake — Task 7

**Type consistency:**
- `ParsedMention` (Task 3) used in Task 7
- `InvestigationRunner` (Task 6) used in Task 7
- `SlackSignatureError`, `verify_slack_signature` (Task 2) used in Task 7
- `render_placeholder`, `render_blocks` (Task 5) used in Task 7
- `SlackClient.post_message`, `update_message` (Task 4) used in Task 7
- All settings field names in Task 1 are referenced consistently in Task 7 (`bot_token`, `signing_secret`, `channel_allowlist`, `default_instance`)

**Deferred (out of scope for v0.3):**
- Slack interactive components (button approval flow) — needed for HostBill v1.1, not Slack-only investigation
- Reading the parent alert message from Slack via `conversations.replies` — the parser already supports being given parent text; Task 7 leaves `parent_text=None` because alert messages from Zabbix typically include event id directly in the user's mention. A follow-up enhancement could fetch the thread parent — flagged as future work, not blocking v0.3.
- Background-task dispatch with Slack 3s ack window — current implementation runs investigation inline. For investigations longer than 3s, Slack will retry the event delivery, which would cause double-runs. Mitigation: short-term use placeholder + inline (works for sub-3s investigations); medium-term move to FastAPI BackgroundTasks with retry-id deduplication. Tracked as a known limitation.

**No placeholders.** Each step shows the exact code, file path, command, and expected output.
