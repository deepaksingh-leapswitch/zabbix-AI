# tests/unit/test_streaming_orchestrator.py
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.audit import AuditLog
from zabbix_ai.memory import Memory
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
async def orch(tmp_path):
    @register("test.echo", description="echo",
              schema={"type": "object",
                      "properties": {"msg": {"type": "string"}},
                      "required": ["msg"]})
    async def echo(*, msg: str) -> str:
        return f"got:{msg}"
    m = Memory(tmp_path / "s.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    audit = AuditLog(m)
    yield m, audit
    await m.close()

async def test_streaming_yields_tool_call_then_final(orch):
    _m, audit = orch
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id="t1",
                                  name="test.echo", input={"msg": "hi"})]),
        _resp("end_turn", [_Block(type="text", text="done")]),
    ])
    o = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                     max_tool_calls=8, clients={})
    events = [e async for e in o.investigate_streaming(
        InvestigationContext(source="ui", question="?"),
    )]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started"
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "final"
    final = events[-1]["data"]
    assert final["summary"] == "done"
    assert final["tool_calls"] == 1


async def test_streaming_yields_error_event_on_unknown_tool(orch):
    _m, audit = orch
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id="t1",
                                  name="evil.delete", input={})]),
        _resp("end_turn", [_Block(type="text", text="bailed")]),
    ])
    o = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                     max_tool_calls=8, clients={})
    events = [e async for e in o.investigate_streaming(
        InvestigationContext(source="ui", question="?"),
    )]
    kinds = [e["event"] for e in events]
    # Even on unknown tool, the loop continues and emits a tool_result with is_error
    assert any(e["event"] == "tool_result" and
               e["data"].get("is_error") for e in events)
    assert kinds[-1] == "final"
