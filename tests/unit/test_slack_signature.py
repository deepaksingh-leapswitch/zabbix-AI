# tests/unit/test_slack_signature.py
import hashlib
import hmac
import time

import pytest

from zabbix_ai.adapters.slack import SlackSignatureError, verify_slack_signature

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
