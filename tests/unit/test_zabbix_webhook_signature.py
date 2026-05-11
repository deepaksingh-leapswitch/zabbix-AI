"""Unit tests for the Zabbix-webhook HMAC verifier."""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from zabbix_ai.adapters.zabbix_webhook import (
    WebhookSignatureError,
    compute_webhook_signature,
    verify_webhook_hmac,
)

_SECRET = "super-secret-shared-with-zabbix"


def _sig_plain(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sig_versioned(body: bytes, ts: str, secret: str = _SECRET) -> str:
    base = f"v0:{ts}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_plain_hex_signature_accepted():
    body = b'{"eventid":"42"}'
    sig = _sig_plain(body)
    verify_webhook_hmac(body, sig, _SECRET)  # no exception


def test_sha256_prefixed_signature_accepted():
    body = b'{"eventid":"42"}'
    sig = "sha256=" + _sig_plain(body)
    verify_webhook_hmac(body, sig, _SECRET)


def test_compute_webhook_signature_matches_verifier():
    body = b'{"eventid":"99","hostid":"7"}'
    sig = compute_webhook_signature(body, _SECRET)
    verify_webhook_hmac(body, sig, _SECRET)


def test_bad_signature_rejected():
    body = b'{"eventid":"42"}'
    with pytest.raises(WebhookSignatureError, match="signature mismatch"):
        verify_webhook_hmac(body, "0" * 64, _SECRET)


def test_wrong_secret_rejected():
    body = b'{"eventid":"42"}'
    sig = _sig_plain(body)
    with pytest.raises(WebhookSignatureError, match="signature mismatch"):
        verify_webhook_hmac(body, sig, "different-secret")


def test_empty_secret_rejects_everything():
    body = b'{"eventid":"42"}'
    sig = _sig_plain(body, "anything")
    with pytest.raises(WebhookSignatureError, match="no webhook secret"):
        verify_webhook_hmac(body, sig, "")


def test_missing_signature_header_rejected():
    with pytest.raises(WebhookSignatureError, match="missing signature"):
        verify_webhook_hmac(b"body", "", _SECRET)


def test_tampered_body_rejected():
    body = b'{"eventid":"42"}'
    sig = _sig_plain(body)
    tampered = b'{"eventid":"99"}'
    with pytest.raises(WebhookSignatureError, match="signature mismatch"):
        verify_webhook_hmac(tampered, sig, _SECRET)


# ── Versioned (timestamped) branch — replay protection ──────────────────────

def test_versioned_signature_with_fresh_timestamp_accepted():
    body = b'{"eventid":"42"}'
    ts = str(int(time.time()))
    sig = _sig_versioned(body, ts)
    verify_webhook_hmac(body, sig, _SECRET, timestamp=ts)


def test_versioned_signature_rejects_stale_timestamp():
    """Timestamp older than the 5-min window must be refused."""
    body = b'{"eventid":"42"}'
    stale_ts = str(int(time.time()) - 60 * 6)  # 6 min in the past
    sig = _sig_versioned(body, stale_ts)
    with pytest.raises(WebhookSignatureError, match="tolerance window"):
        verify_webhook_hmac(body, sig, _SECRET, timestamp=stale_ts)


def test_versioned_signature_rejects_future_timestamp():
    body = b'{"eventid":"42"}'
    future_ts = str(int(time.time()) + 60 * 6)
    sig = _sig_versioned(body, future_ts)
    with pytest.raises(WebhookSignatureError, match="tolerance window"):
        verify_webhook_hmac(body, sig, _SECRET, timestamp=future_ts)


def test_versioned_signature_rejects_bad_timestamp_format():
    body = b'{"eventid":"42"}'
    sig = _sig_versioned(body, "1700000000")
    with pytest.raises(WebhookSignatureError, match="invalid timestamp"):
        verify_webhook_hmac(body, sig, _SECRET, timestamp="not-a-number")


def test_versioned_signature_rejects_wrong_digest():
    body = b'{"eventid":"42"}'
    ts = str(int(time.time()))
    # Use the plain-hex signature with the v0 timestamp branch — must be
    # rejected because the verifier expects the v0=… format.
    plain = _sig_plain(body)
    with pytest.raises(WebhookSignatureError, match="signature mismatch"):
        verify_webhook_hmac(body, plain, _SECRET, timestamp=ts)
