from pathlib import Path

import pytest

from zabbix_ai.memory import Memory


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()


async def test_schema_created(memory):
    rows = await memory.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    names = {r[0] for r in rows}
    assert {"investigations", "host_facts", "patterns",
            "ticket_resolutions", "audit_log", "schema_version",
            "secrets_kv", "connections"} <= names


async def test_migrations_idempotent(memory):
    await memory.run_migrations(Path("migrations"))
    rows = await memory.fetchall(
        "SELECT version FROM schema_version ORDER BY version",
    )
    # Each migrations/NNN_*.sql file inserts its own version row.
    assert rows == [(1,), (2,), (3,), (4,)]
