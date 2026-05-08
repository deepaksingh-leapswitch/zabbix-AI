import pytest
from pathlib import Path
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
            "ticket_resolutions", "audit_log", "schema_version"} <= names


async def test_migrations_idempotent(memory):
    await memory.run_migrations(Path("migrations"))
    rows = await memory.fetchall("SELECT version FROM schema_version")
    assert rows == [(1,)]
