# tests/unit/test_url_signing.py
import pytest

from zabbix_ai.url_signing import UrlSignatureError, sign_url_token, verify_url_token

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
