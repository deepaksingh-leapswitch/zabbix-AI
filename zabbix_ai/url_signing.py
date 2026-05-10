from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
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
    """Return a token of the form: b64(payload_json).b64(exp).b64(hmac_sha256).

    The payload automatically gains a ``jti`` field (16-byte hex nonce) so
    each token can be marked single-use on the server side (#2, #7).
    """
    payload = dict(payload)
    if "jti" not in payload:
        payload["jti"] = secrets.token_hex(16)
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
