"""Unit tests for zabbix_ai.services.budget — daily Anthropic INR cap."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zabbix_ai.config import BudgetSettings, Settings
from zabbix_ai.memory import Memory
from zabbix_ai.services.budget import (
    HAIKU_MODEL,
    BudgetExceededError,
    enforce_budget,
    remaining_budget_inr,
    today_spend_inr,
)

# Migrations live at <repo>/migrations relative to this file.
_MIGRATIONS = Path(__file__).resolve().parent.parent.parent / "migrations"


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "budget.db")
    await m.connect()
    await m.run_migrations(_MIGRATIONS)
    yield m
    await m.close()


def _settings(*, cap: float = 0.0, action: str = "haiku_only",
              reset_hour: int = 0, fx: float = 83.0) -> Settings:
    """Minimal Settings instance — only the budget block matters for these tests."""
    s = Settings()
    s.budget = BudgetSettings(
        daily_inr_cap=cap,
        over_budget_action=action,  # type: ignore[arg-type]
        reset_hour_utc=reset_hour,
        usd_to_inr=fx,
    )
    return s


async def _insert_investigation(memory: Memory, *, started_at: str,
                                 model: str, tokens_in: int,
                                 tokens_out: int) -> None:
    await memory.execute(
        """INSERT INTO investigations
           (source, started_at, model, tokens_in, tokens_out)
           VALUES (?, ?, ?, ?, ?)""",
        ("cli", started_at, model, tokens_in, tokens_out),
    )


# ─── enforce_budget ────────────────────────────────────────────────────────


async def test_zero_cap_is_unlimited_and_writes_no_audit(memory):
    s = _settings(cap=0.0)
    model, reason = await enforce_budget(
        memory, s, investigation_id=None, model_requested="claude-sonnet-4-6",
    )
    assert model == "claude-sonnet-4-6"
    assert reason == "no_cap"
    # No audit row was written.
    rows = await memory.fetchall("SELECT COUNT(*) FROM budget_audit")
    assert rows[0][0] == 0


async def test_under_cap_returns_requested_and_writes_audit(memory):
    s = _settings(cap=1000.0)
    # Seed a tiny spend: 1k input on haiku ~ $0.0008 ~ ₹0.066
    await _insert_investigation(
        memory, started_at=datetime.now(UTC).isoformat(),
        model=HAIKU_MODEL, tokens_in=1_000, tokens_out=0,
    )
    model, reason = await enforce_budget(
        memory, s, investigation_id=None, model_requested="claude-sonnet-4-6",
    )
    assert model == "claude-sonnet-4-6"
    assert reason == "allowed"
    rows = await memory.fetchall(
        "SELECT action, model_requested, model_effective FROM budget_audit",
    )
    assert len(rows) == 1
    assert rows[0] == ("allowed", "claude-sonnet-4-6", "claude-sonnet-4-6")


async def test_over_cap_haiku_only_downgrades(memory):
    # cap=1 INR, but seed ₹1494 (1M+1M sonnet) of spend today.
    s = _settings(cap=1.0, action="haiku_only")
    await _insert_investigation(
        memory, started_at=datetime.now(UTC).isoformat(),
        model="claude-sonnet-4-6",
        tokens_in=1_000_000, tokens_out=1_000_000,
    )
    model, reason = await enforce_budget(
        memory, s, investigation_id=None, model_requested="claude-sonnet-4-6",
    )
    assert model == HAIKU_MODEL
    assert reason == "over_budget_haiku_fallback"
    rows = await memory.fetchall(
        "SELECT action, model_requested, model_effective FROM budget_audit",
    )
    assert rows[0] == ("downgraded_haiku", "claude-sonnet-4-6", HAIKU_MODEL)


async def test_over_cap_pause_returns_none(memory):
    s = _settings(cap=1.0, action="pause")
    await _insert_investigation(
        memory, started_at=datetime.now(UTC).isoformat(),
        model="claude-sonnet-4-6",
        tokens_in=1_000_000, tokens_out=1_000_000,
    )
    model, reason = await enforce_budget(
        memory, s, investigation_id=None, model_requested="claude-sonnet-4-6",
    )
    assert model is None
    assert reason == "over_budget_paused"
    rows = await memory.fetchall(
        "SELECT action, model_effective FROM budget_audit",
    )
    assert rows[0][0] == "paused"
    assert rows[0][1] is None


async def test_over_cap_warn_allows_but_audits(memory):
    s = _settings(cap=1.0, action="warn")
    await _insert_investigation(
        memory, started_at=datetime.now(UTC).isoformat(),
        model="claude-sonnet-4-6",
        tokens_in=1_000_000, tokens_out=1_000_000,
    )
    model, reason = await enforce_budget(
        memory, s, investigation_id=None, model_requested="claude-sonnet-4-6",
    )
    assert model == "claude-sonnet-4-6"
    assert reason == "over_budget_warn_only"
    rows = await memory.fetchall(
        "SELECT action FROM budget_audit",
    )
    assert rows[0][0] == "allowed"


# ─── today_spend_inr / reset hour ──────────────────────────────────────────


async def test_today_spend_excludes_yesterday(memory):
    """With reset_hour_utc=0, an investigation from > 24h ago must not count."""
    yesterday = datetime.now(UTC) - timedelta(days=1, hours=2)
    today = datetime.now(UTC)
    # 1M+1M sonnet ~ $18 ~ ₹1494
    await _insert_investigation(
        memory, started_at=yesterday.isoformat(),
        model="claude-sonnet-4-6",
        tokens_in=1_000_000, tokens_out=1_000_000,
    )
    # A tiny one today
    await _insert_investigation(
        memory, started_at=today.isoformat(),
        model=HAIKU_MODEL, tokens_in=1_000, tokens_out=0,
    )
    spent = await today_spend_inr(memory, fx=83.0, reset_hour_utc=0)
    # Only today's tiny haiku call should count — well under ₹1.
    assert spent < 1.0
    assert spent > 0.0


async def test_reset_hour_boundary_excludes_pre_reset_window(memory):
    """An investigation from 1h ago, when reset_hour is 'now', counts as yesterday."""
    now = datetime.now(UTC)
    reset_hour = now.hour  # boundary is "right now"
    pre_reset = now - timedelta(hours=2)
    # The pre_reset spend should fall on the *previous* budget day
    # because (now - 2h) is before the most recent reset boundary.
    await _insert_investigation(
        memory, started_at=pre_reset.isoformat(),
        model="claude-sonnet-4-6",
        tokens_in=1_000_000, tokens_out=1_000_000,
    )
    spent = await today_spend_inr(memory, fx=83.0, reset_hour_utc=reset_hour)
    assert spent == 0.0


# ─── remaining_budget_inr ──────────────────────────────────────────────────


async def test_remaining_budget_disabled(memory):
    s = _settings(cap=0.0)
    out = await remaining_budget_inr(memory, s)
    assert out["status"] == "disabled"
    assert out["limit"] is None
    assert out["remaining"] is None


async def test_remaining_budget_ok_status(memory):
    s = _settings(cap=1000.0)
    out = await remaining_budget_inr(memory, s)
    assert out["status"] == "ok"
    assert out["limit"] == 1000.0
    assert out["remaining"] == 1000.0
    assert out["pct_remaining"] == 100.0


async def test_remaining_budget_over_haiku_fallback(memory):
    s = _settings(cap=1.0, action="haiku_only")
    await _insert_investigation(
        memory, started_at=datetime.now(UTC).isoformat(),
        model="claude-sonnet-4-6",
        tokens_in=1_000_000, tokens_out=1_000_000,
    )
    out = await remaining_budget_inr(memory, s)
    assert out["status"] == "haiku-fallback"
    assert out["remaining"] == 0.0


# ─── error symbol ──────────────────────────────────────────────────────────


def test_budget_exceeded_error_is_runtime_error_subclass():
    # Callers (orchestrator, webhook) catch it explicitly; ensuring it
    # remains a RuntimeError lets generic except blocks still capture it.
    assert issubclass(BudgetExceededError, RuntimeError)
