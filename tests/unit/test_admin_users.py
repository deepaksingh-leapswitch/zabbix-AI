from __future__ import annotations

import re
from pathlib import Path

import pyotp
import pytest

from zabbix_ai.admin import users
from zabbix_ai.memory import Memory


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


def test_hash_password_round_trip():
    h = users.hash_password("secret123")
    assert users.verify_password("secret123", h) is True


def test_verify_password_wrong():
    h = users.hash_password("correct")
    assert users.verify_password("wrong", h) is False


def test_generate_totp_secret_is_base32():
    s = users.generate_totp_secret()
    # pyotp returns 32-char base32 string
    assert re.fullmatch(r"[A-Z2-7]{32}", s)


def test_verify_totp_accepts_current():
    secret = users.generate_totp_secret()
    code = pyotp.TOTP(secret).now()
    assert users.verify_totp(secret, code) is True


def test_verify_totp_rejects_wrong():
    secret = users.generate_totp_secret()
    assert users.verify_totp(secret, "000000") is False


async def test_create_and_get_user(mem):
    u = await users.create_user(mem, username="alice", password="pw1", role="admin")
    assert u["username"] == "alice"
    assert u["role"] == "admin"
    assert len(u["totp_secret"]) == 32

    fetched = await users.get_user_by_username(mem, "alice")
    assert fetched is not None
    assert fetched["username"] == "alice"
    assert fetched["totp_enrolled"] == 0
    assert fetched["disabled"] == 0
    assert users.verify_password("pw1", fetched["password_hash"])


async def test_get_user_unknown_returns_none(mem):
    assert await users.get_user_by_username(mem, "nobody") is None


async def test_ensure_bootstrap_admin_creates_when_empty(mem):
    result = await users.ensure_bootstrap_admin(mem, username="admin", password="bootstrap")
    assert result is not None
    assert result["username"] == "admin"
    assert result["role"] == "admin"


async def test_ensure_bootstrap_admin_skips_when_users_exist(mem):
    await users.create_user(mem, username="existing", password="pw")
    result = await users.ensure_bootstrap_admin(mem, username="admin", password="bootstrap")
    assert result is None
    # the existing user is still the only one
    row = await mem.fetchone("SELECT COUNT(*) FROM users")
    assert row and row[0] == 1


async def test_set_totp_enrolled(mem):
    u = await users.create_user(mem, username="bob", password="pw2")
    await users.set_totp_enrolled(mem, u["id"])
    fetched = await users.get_user_by_username(mem, "bob")
    assert fetched is not None
    assert fetched["totp_enrolled"] == 1


async def test_create_oauth_user_and_get_by_oauth(mem):
    created = await users.create_oauth_user(
        mem,
        username="google_user@example.com",
        provider="google",
        subject="123456789",
        role="operator",
    )
    assert created["username"] == "google_user@example.com"
    assert created["role"] == "operator"
    assert "id" in created

    found = await users.get_user_by_oauth(
        mem, provider="google", subject="123456789",
    )
    assert found is not None
    assert found["username"] == "google_user@example.com"
    assert found["role"] == "operator"
    assert not found["disabled"]


async def test_get_user_by_oauth_returns_none_for_unknown(mem):
    result = await users.get_user_by_oauth(
        mem, provider="google", subject="nonexistent",
    )
    assert result is None


async def test_create_oauth_user_totp_enrolled(mem):
    """OAuth users are pre-enrolled in TOTP (Google does its own 2FA)."""
    created = await users.create_oauth_user(
        mem, username="sso@example.com", provider="google", subject="sub42",
    )
    row = await mem.fetchone(
        "SELECT totp_enrolled FROM users WHERE id=?", (created["id"],),
    )
    assert row is not None
    assert row[0] == 1
