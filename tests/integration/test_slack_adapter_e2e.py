# tests/integration/test_slack_adapter_e2e.py
import hashlib
import hmac
import json
import time
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

    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create = AsyncMock(side_effect=[
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
