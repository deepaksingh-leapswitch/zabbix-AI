# tests/integration/test_zabbix_ui_e2e.py
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
    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as mock_a:
        mock_a.return_value.messages.create = AsyncMock(side_effect=[
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
