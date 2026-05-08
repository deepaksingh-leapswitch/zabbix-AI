from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.audit import AuditLog
from zabbix_ai.memory import Memory
from zabbix_ai.orchestrator import InvestigationContext, Orchestrator
from zabbix_ai.tools import register


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)

def _resp(stop_reason: str, blocks: list, in_t: int = 100, out_t: int = 50):
    return MagicMock(
        stop_reason=stop_reason, content=blocks,
        usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                        cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )

@pytest.fixture
async def setup(tmp_path):
    @register("test.echo", description="echo",
              schema={"type": "object", "properties": {"msg": {"type": "string"}},
                      "required": ["msg"]})
    async def echo(*, msg: str) -> str:
        return f"got:{msg}"

    m = Memory(tmp_path / "o.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    audit = AuditLog(m)
    yield m, audit
    await m.close()

async def test_orchestrator_runs_tool_then_stops(setup):
    _m, audit = setup
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id="t1",
                                  name="test.echo", input={"msg": "hello"})]),
        _resp("end_turn", [_Block(type="text", text="done")]),
    ])
    orch = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                        max_tool_calls=8, clients={})
    ctx = InvestigationContext(source="cli", question="what is the test?")
    result = await orch.investigate(ctx)
    assert result.summary == "done"
    assert claude.create.await_count == 2

async def test_orchestrator_unknown_tool_continues(setup):
    _m, audit = setup
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id="t1",
                                  name="evil.delete", input={})]),
        _resp("end_turn", [_Block(type="text", text="bailed")]),
    ])
    orch = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                        max_tool_calls=8, clients={})
    result = await orch.investigate(InvestigationContext(source="cli", question="?"))
    assert result.summary == "bailed"

async def test_orchestrator_caps_tool_calls(setup):
    _m, audit = setup
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("tool_use", [_Block(type="tool_use", id=f"t{i}",
                                  name="test.echo", input={"msg": str(i)})])
        for i in range(10)
    ] + [_resp("end_turn", [_Block(type="text", text="capped")])])
    orch = Orchestrator(claude=claude, audit=audit, model="m", summary_model="h",
                        max_tool_calls=3, clients={})
    await orch.investigate(InvestigationContext(source="cli", question="?"))
    assert claude.create.await_count <= 6
