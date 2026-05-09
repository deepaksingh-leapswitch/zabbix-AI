from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.memory import Memory, upsert_pattern
from zabbix_ai.tools import dispatch
from zabbix_ai.tools.memory import register_tools


@pytest.fixture
async def context(tmp_path):
    m = Memory(tmp_path / "t.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    await upsert_pattern(m, signature="sig-A",
                         typical_root_cause="disk full",
                         typical_fix="rotate logs")
    await m.execute(
        "INSERT INTO investigations (source, started_at, hostid, "
        " pattern_signature, summary) VALUES (?, ?, ?, ?, ?)",
        ("cli", "2026-05-09T00:00:00+00:00", 12345, "sig-A", "old run"),
    )
    yield {"memory": m, "hostbill_client": None}
    await m.close()


async def test_find_similar_by_hostid(context):
    register_tools()
    rows = await dispatch(
        "memory.find_similar_past_investigations",
        {"hostid": 12345}, context=context,
    )
    assert len(rows) == 1
    assert rows[0]["summary"] == "old run"


async def test_find_pattern(context):
    register_tools()
    row = await dispatch(
        "memory.find_pattern",
        {"signature": "sig-A"}, context=context,
    )
    assert row is not None
    assert row["typical_fix"] == "rotate logs"


async def test_find_resolved_tickets_when_hostbill_not_configured(context):
    register_tools()
    out = await dispatch(
        "memory.find_resolved_tickets",
        {"alert_pattern": "disk full", "limit": 5}, context=context,
    )
    assert isinstance(out, str)
    assert "not configured" in out.lower()


async def test_find_resolved_tickets_calls_hostbill(tmp_path):
    m = Memory(tmp_path / "tb.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    fake = MagicMock()
    fake.search_tickets = AsyncMock(return_value=[
        {"id": "100", "subject": "Disk full on web-01", "status": "Closed",
         "client_id": "7", "lastreply": "2026-04-10 14:00:00"},
    ])
    register_tools()
    out = await dispatch(
        "memory.find_resolved_tickets",
        {"alert_pattern": "Disk full", "limit": 5},
        context={"memory": m, "hostbill_client": fake},
    )
    await m.close()
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["subject"] == "Disk full on web-01"
    fake.search_tickets.assert_awaited_once()
