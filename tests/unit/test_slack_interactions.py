import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from zabbix_ai.adapters.slack_interactions import build_router
from zabbix_ai.config import Settings, SlackSettings, TicketFlowSettings

SECRET = "shhh-signing-secret"


def _settings(approvers=None):
    s = Settings(
        slack=SlackSettings(bot_token_env="B", signing_secret_env="S"),
        ticket_flow=TicketFlowSettings(
            enabled=True, approver_slack_user_ids=approvers or []),
    )
    s.slack.bot_token = SecretStr("xoxb-test")
    s.slack.signing_secret = SecretStr(SECRET)
    return s


def _client(settings):
    app = FastAPI()
    app.include_router(build_router(settings))
    return TestClient(app)


def _signed(body: bytes, ts: str | None = None):
    ts = ts or str(int(time.time()))
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def _payload_body(action_id="ticket_approve", value="1", user="U_OK"):
    payload = {
        "type": "block_actions",
        "user": {"id": user},
        "response_url": "https://hooks.slack.test/abc",
        "actions": [{"action_id": action_id, "value": value}],
    }
    import json
    return urlencode({"payload": json.dumps(payload)}).encode()


def test_bad_signature_rejected():
    client = _client(_settings())
    body = _payload_body()
    resp = client.post("/slack/interactions", content=body,
                       headers={"X-Slack-Request-Timestamp": str(int(time.time())),
                                "X-Slack-Signature": "v0=deadbeef"})
    assert resp.status_code == 401


def test_stale_timestamp_rejected():
    client = _client(_settings())
    body = _payload_body()
    old = str(int(time.time()) - 9999)
    resp = client.post("/slack/interactions", content=body, headers=_signed(body, old))
    assert resp.status_code == 401


def test_unauthorized_user_blocked():
    # Approver allowlist set; a different user must be refused (no work done).
    client = _client(_settings(approvers=["U_ADMIN"]))
    body = _payload_body(user="U_RANDOM")
    resp = client.post("/slack/interactions", content=body, headers=_signed(body))
    assert resp.status_code == 200
    assert "allowlist" in resp.json()["text"]


def test_non_block_actions_ignored():
    client = _client(_settings())
    import json
    body = urlencode({"payload": json.dumps({"type": "view_submission"})}).encode()
    resp = client.post("/slack/interactions", content=body, headers=_signed(body))
    assert resp.status_code == 200
