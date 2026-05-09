import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.audit import AuditLog
from zabbix_ai.memory import Memory, find_pattern
from zabbix_ai.orchestrator import InvestigationContext, Orchestrator
from zabbix_ai.tools import register


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


def _resp(stop_reason, blocks, in_t=10, out_t=5):
    return MagicMock(stop_reason=stop_reason, content=blocks,
                     usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))


@pytest.fixture
async def setup(tmp_path):
    @register("test.echo", description="echo",
              schema={"type": "object", "properties": {"x": {"type": "string"}},
                      "required": ["x"]})
    async def _e(*, x: str) -> str:
        return x
    m = Memory(tmp_path / "wb.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m, AuditLog(m)
    await m.close()


async def test_writeback_creates_pattern_row_with_signature(setup):
    m, audit = setup
    claude = MagicMock()
    # main loop: end_turn directly, then _write_back haiku call returning JSON
    claude.create = AsyncMock(side_effect=[
        _resp("end_turn", [_Block(type="text",
                                   text="root_cause: disk full\nconfidence: high")]),
        _resp("end_turn", [_Block(type="text",
                                   text=json.dumps({
                                       "root_cause_short": "disk full on /var",
                                       "fix_short": "rotate logs",
                                       "host_facts": {"primary_role": "web"}}))]),
    ])
    o = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                     max_tool_calls=8,
                     clients={}, memory=m)
    ctx = InvestigationContext(
        source="cli", instance="monitoring", hostid=12345,
        hostname="web-01",
        problem_name="Disk space low on /var",
        hostgroup="Managed cPanel VPS",
    )
    result = await o.investigate(ctx)
    # pattern table populated
    sig = result.pattern_signature
    assert sig
    pat = await find_pattern(m, signature=sig)
    assert pat is not None
    assert pat["typical_fix"] == "rotate logs"
    # host_facts populated
    rows = await m.fetchall(
        "SELECT key, value FROM host_facts WHERE hostid=12345",
    )
    assert ("primary_role", "web") in rows


async def test_writeback_failure_does_not_break_result(setup):
    m, audit = setup
    claude = MagicMock()
    # main returns end_turn ok, _write_back haiku raises
    claude.create = AsyncMock(side_effect=[
        _resp("end_turn", [_Block(type="text", text="ok")]),
        Exception("haiku timeout"),
    ])
    o = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                     max_tool_calls=8, clients={}, memory=m)
    result = await o.investigate(InvestigationContext(
        source="cli", problem_name="x", hostid=1,
    ))
    assert result.summary == "ok"
    assert result.investigation_id  # didn't crash
