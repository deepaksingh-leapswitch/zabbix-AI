from __future__ import annotations

from pathlib import Path

import pytest

from zabbix_ai.memory import Memory
from zabbix_ai.services.connection_health import get_health, record_health


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


async def test_record_success_creates_row(mem):
    await record_health(mem, kind="zabbix", name="monitoring", ok=True)
    rows = await mem.fetchall(
        "SELECT kind, name, last_success_at, last_error_at, last_error "
        "FROM connection_health"
    )
    assert len(rows) == 1
    kind, name, succ, err_at, err = rows[0]
    assert kind == "zabbix"
    assert name == "monitoring"
    assert succ  # non-empty ISO timestamp
    assert err_at is None
    assert (err or "") == ""


async def test_record_failure_updates_error_fields(mem):
    # Seed a success first.
    await record_health(mem, kind="anthropic", name="primary", ok=True)
    # Now a failure for the same (kind, name) — should update the same row.
    await record_health(
        mem, kind="anthropic", name="primary",
        ok=False, error="HTTP 529 overloaded",
    )

    rows = await mem.fetchall(
        "SELECT kind, name, last_success_at, last_error_at, last_error "
        "FROM connection_health WHERE kind='anthropic' AND name='primary'"
    )
    assert len(rows) == 1
    _, _, succ, err_at, err = rows[0]
    assert succ          # original success retained
    assert err_at        # error timestamp written
    assert err == "HTTP 529 overloaded"


async def test_record_failure_truncates_long_error(mem):
    long_err = "x" * 500
    await record_health(mem, kind="slack", name="primary",
                        ok=False, error=long_err)
    row = await mem.fetchone(
        "SELECT last_error FROM connection_health "
        "WHERE kind='slack' AND name='primary'"
    )
    assert row is not None
    assert len(row[0]) == 200


async def test_get_health_returns_mapping(mem):
    await record_health(mem, kind="zabbix", name="a", ok=True)
    await record_health(mem, kind="zabbix", name="b", ok=False, error="boom")

    out = await get_health(mem)
    assert ("zabbix", "a") in out
    assert ("zabbix", "b") in out
    assert out[("zabbix", "a")]["last_success_at"]
    assert out[("zabbix", "a")]["last_error"] == ""
    assert out[("zabbix", "b")]["last_error"] == "boom"


async def test_get_health_missing_table_returns_empty(tmp_path):
    # Build a Memory with no migrations run — connection_health does not exist.
    m = Memory(tmp_path / "empty.db")
    await m.connect()
    try:
        out = await get_health(m)
        assert out == {}
    finally:
        await m.close()


async def test_record_health_missing_table_does_not_raise(tmp_path):
    m = Memory(tmp_path / "empty.db")
    await m.connect()
    try:
        # Must not raise even though connection_health does not exist.
        await record_health(m, kind="zabbix", name="x", ok=True)
        await record_health(m, kind="zabbix", name="x", ok=False, error="boom")
    finally:
        await m.close()


async def test_record_health_with_none_memory_is_noop():
    # Should silently do nothing — used when constructor wasn't given memory.
    await record_health(None, kind="zabbix", name="x", ok=True)
    assert await get_health(None) == {}
