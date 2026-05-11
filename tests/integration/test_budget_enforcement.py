"""End-to-end integration test for the daily budget gate (v1.5).

Drives the orchestrator with a mocked Claude client and confirms that
once today's spend exceeds the configured cap, subsequent investigations
either run on haiku (default action) or are blocked outright.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.audit import AuditLog
from zabbix_ai.config import BudgetSettings, Settings
from zabbix_ai.memory import Memory
from zabbix_ai.orchestrator import InvestigationContext, Orchestrator
from zabbix_ai.services.budget import HAIKU_MODEL, BudgetExceededError

_MIGRATIONS = Path(__file__).resolve().parent.parent.parent / "migrations"


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


def _resp(stop_reason: str, blocks: list, in_t: int = 100, out_t: int = 50):
    return MagicMock(
        stop_reason=stop_reason, content=blocks,
        usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                        cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "budget_e2e.db")
    await m.connect()
    await m.run_migrations(_MIGRATIONS)
    yield m
    await m.close()


def _settings(*, cap: float, action: str = "haiku_only") -> Settings:
    s = Settings()
    s.budget = BudgetSettings(
        daily_inr_cap=cap,
        over_budget_action=action,  # type: ignore[arg-type]
        reset_hour_utc=0,
        usd_to_inr=83.0,
    )
    s.default_model = "claude-sonnet-4-6"
    return s


async def test_over_budget_haiku_fallback_swaps_model(memory):
    """A first investigation that burns ~₹1494 (1M+1M sonnet) puts spend
    over the ₹0.10 cap; the next investigation must run on haiku."""
    settings = _settings(cap=0.10, action="haiku_only")
    audit = AuditLog(memory)

    claude = MagicMock()
    # With ctx.problem_name empty, write-back is skipped, so each
    # investigation makes exactly one claude.create() call.
    claude.create = AsyncMock(side_effect=[
        _resp("end_turn", [_Block(type="text", text="first done")],
              in_t=1_000_000, out_t=1_000_000),
        _resp("end_turn", [_Block(type="text", text="second done")],
              in_t=10, out_t=5),
    ])

    orch = Orchestrator(
        claude=claude, audit=audit,
        model="claude-sonnet-4-6", summary_model=HAIKU_MODEL,
        max_tool_calls=4, clients={},
        memory=memory, settings=settings,
    )

    r1 = await orch.investigate(InvestigationContext(source="cli", question="q1"))
    assert r1.summary == "first done"
    # First call landed on sonnet — the budget gate ran *before* it but
    # there was no prior spend, so cap wasn't breached.
    first_call_model = claude.create.call_args_list[0].kwargs["model"]
    assert first_call_model == "claude-sonnet-4-6"

    r2 = await orch.investigate(InvestigationContext(source="cli", question="q2"))
    assert r2.summary == "second done"
    # Second claude call (= run 2) should be on haiku.
    second_call_model = claude.create.call_args_list[1].kwargs["model"]
    assert second_call_model == HAIKU_MODEL

    # And the investigations row for run 2 records the effective model.
    row = await memory.fetchone(
        "SELECT model FROM investigations ORDER BY id DESC LIMIT 1",
    )
    assert row[0] == HAIKU_MODEL

    # Audit row(s) were written by enforce_budget.
    audit_rows = await memory.fetchall(
        "SELECT action, model_effective FROM budget_audit ORDER BY id",
    )
    # Run 1's gate logged 'allowed' (spent was 0); run 2's logged
    # 'downgraded_haiku'.
    actions = [r[0] for r in audit_rows]
    assert "allowed" in actions
    assert "downgraded_haiku" in actions


async def test_over_budget_pause_blocks_with_exception(memory):
    settings = _settings(cap=0.10, action="pause")
    audit = AuditLog(memory)
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("end_turn", [_Block(type="text", text="first done")],
              in_t=1_000_000, out_t=1_000_000),
    ])
    orch = Orchestrator(
        claude=claude, audit=audit,
        model="claude-sonnet-4-6", summary_model=HAIKU_MODEL,
        max_tool_calls=4, clients={},
        memory=memory, settings=settings,
    )
    await orch.investigate(InvestigationContext(source="cli", question="q1"))
    with pytest.raises(BudgetExceededError):
        await orch.investigate(InvestigationContext(source="cli", question="q2"))
    # Audit row recorded.
    rows = await memory.fetchall(
        "SELECT action FROM budget_audit ORDER BY id",
    )
    assert "paused" in [r[0] for r in rows]


async def test_no_cap_orchestrator_unchanged(memory):
    """With cap=0, the orchestrator must not change models or write audit rows."""
    settings = _settings(cap=0.0)
    audit = AuditLog(memory)
    claude = MagicMock()
    claude.create = AsyncMock(side_effect=[
        _resp("end_turn", [_Block(type="text", text="done")],
              in_t=1_000_000, out_t=1_000_000),
        _resp("end_turn", [_Block(type="text", text="done2")],
              in_t=1_000_000, out_t=1_000_000),
    ])
    orch = Orchestrator(
        claude=claude, audit=audit,
        model="claude-sonnet-4-6", summary_model=HAIKU_MODEL,
        max_tool_calls=4, clients={},
        memory=memory, settings=settings,
    )
    await orch.investigate(InvestigationContext(source="cli", question="q1"))
    await orch.investigate(InvestigationContext(source="cli", question="q2"))
    # Both runs used sonnet.
    used = [c.kwargs["model"] for c in claude.create.call_args_list
            if c.kwargs.get("model") == "claude-sonnet-4-6"]
    assert len(used) == 2
    rows = await memory.fetchall("SELECT COUNT(*) FROM budget_audit")
    assert rows[0][0] == 0
