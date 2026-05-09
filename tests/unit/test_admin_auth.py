from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zabbix_ai.admin import auth, users
from zabbix_ai.memory import Memory

SECRET = "test-session-secret-32-bytes-ok!"


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


@pytest.fixture
async def user_id(mem):
    u = await users.create_user(mem, username="testuser", password="pw", role="admin")
    return u["id"]


async def test_create_and_resolve_session(mem, user_id):
    cookie = await auth.create_session(
        mem, user_id=user_id, secret=SECRET, ttl_seconds=3600,
    )
    resolved = await auth.resolve_session(mem, signed_cookie=cookie, secret=SECRET)
    assert resolved is not None
    assert resolved["username"] == "testuser"
    assert resolved["role"] == "admin"
    assert resolved["user_id"] == user_id


async def test_expired_session_returns_none(mem, user_id):
    # create a session and then manually set expires_at in the past
    cookie = await auth.create_session(
        mem, user_id=user_id, secret=SECRET, ttl_seconds=3600,
    )
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await mem.execute("UPDATE sessions SET expires_at=?", (past,))
    resolved = await auth.resolve_session(mem, signed_cookie=cookie, secret=SECRET)
    assert resolved is None


async def test_disabled_user_returns_none(mem, user_id):
    cookie = await auth.create_session(
        mem, user_id=user_id, secret=SECRET, ttl_seconds=3600,
    )
    await mem.execute("UPDATE users SET disabled=1 WHERE id=?", (user_id,))
    resolved = await auth.resolve_session(mem, signed_cookie=cookie, secret=SECRET)
    assert resolved is None


async def test_bad_signature_returns_none(mem, user_id):
    cookie = await auth.create_session(
        mem, user_id=user_id, secret=SECRET, ttl_seconds=3600,
    )
    tampered = cookie + "x"
    resolved = await auth.resolve_session(mem, signed_cookie=tampered, secret=SECRET)
    assert resolved is None


async def test_destroy_session_removes_row(mem, user_id):
    cookie = await auth.create_session(
        mem, user_id=user_id, secret=SECRET, ttl_seconds=3600,
    )
    resolved = await auth.resolve_session(mem, signed_cookie=cookie, secret=SECRET)
    assert resolved is not None
    sid = resolved["sid"]
    await auth.destroy_session(mem, sid)
    resolved_after = await auth.resolve_session(mem, signed_cookie=cookie, secret=SECRET)
    assert resolved_after is None
