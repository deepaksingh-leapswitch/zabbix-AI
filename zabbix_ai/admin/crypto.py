from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def derive_key(master: str) -> bytes:
    """32-byte AES-256 key derived from a master string via HKDF-SHA256."""
    if not master:
        raise ValueError("empty master key")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"zabbix-ai/secrets/v1",
        info=b"AES-GCM",
    ).derive(master.encode())


def encrypt(plaintext: str, key: bytes) -> tuple[bytes, bytes]:
    """Return (nonce, ciphertext)."""
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return nonce, ct


def decrypt(nonce: bytes, ciphertext: bytes, key: bytes) -> str:
    return AESGCM(key).decrypt(bytes(nonce), bytes(ciphertext), None).decode()
