"""Unit tests for zabbix_ai.admin.crypto."""
from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from zabbix_ai.admin.crypto import decrypt, derive_key, encrypt


def test_derive_key_length():
    key = derive_key("some-master-secret")
    assert len(key) == 32


def test_derive_key_deterministic():
    k1 = derive_key("same-secret")
    k2 = derive_key("same-secret")
    assert k1 == k2


def test_derive_key_different_masters():
    k1 = derive_key("secret-a")
    k2 = derive_key("secret-b")
    assert k1 != k2


def test_derive_key_empty_raises():
    with pytest.raises(ValueError, match="empty master key"):
        derive_key("")


def test_encrypt_decrypt_roundtrip():
    key = derive_key("test-key")
    plaintext = "hello world"
    nonce, ct = encrypt(plaintext, key)
    assert decrypt(nonce, ct, key) == plaintext


def test_encrypt_produces_different_nonces():
    key = derive_key("test-key")
    nonce1, ct1 = encrypt("same", key)
    nonce2, ct2 = encrypt("same", key)
    # Nonces are random per call
    assert nonce1 != nonce2
    # Ciphertexts are different (nonce is part of AEAD)
    assert ct1 != ct2


def test_decrypt_wrong_key_raises():
    key1 = derive_key("key-one")
    key2 = derive_key("key-two")
    nonce, ct = encrypt("secret", key1)
    with pytest.raises(InvalidTag):
        decrypt(nonce, ct, key2)


def test_decrypt_tampered_ciphertext_raises():
    key = derive_key("key")
    nonce, ct = encrypt("data", key)
    tampered = bytes([ct[0] ^ 0xFF]) + ct[1:]
    with pytest.raises(InvalidTag):
        decrypt(nonce, tampered, key)
