from pathlib import Path

import pytest

from zabbix_ai.audit import AuditLog
from zabbix_ai.memory import Memory


@pytest.fixture
async def audit(tmp_path):
    m = Memory(tmp_path / "a.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield AuditLog(m)
    await m.close()

async def test_log_start_returns_investigation_id(audit):
    inv_id = await audit.log_start(source="cli", instance="monitoring", eventid=99)
    assert inv_id > 0

async def test_log_tool_records_call(audit):
    inv_id = await audit.log_start(source="cli")
    await audit.log_tool(inv_id, "diag.df", {"hostid": 1}, "Filesystem ...")
    rows = await audit.memory.fetchall(
        "SELECT event_type, tool_name FROM audit_log WHERE investigation_id=?", (inv_id,)
    )
    assert ("tool_call", "diag.df") in rows

async def test_log_end_marks_complete(audit):
    inv_id = await audit.log_start(source="cli")
    await audit.log_end(inv_id, summary="ok", duration_ms=1234)
    row = await audit.memory.fetchone(
        "SELECT summary, duration_ms FROM investigations WHERE id=?", (inv_id,)
    )
    assert row == ("ok", 1234)
