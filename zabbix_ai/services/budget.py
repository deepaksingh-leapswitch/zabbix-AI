"""Daily Anthropic budget enforcement (v1.5).

Computes today's spend in INR from the ``investigations`` table using
:mod:`zabbix_ai.services.pricing`. The cutoff between "today" and
"yesterday" is configurable via ``settings.budget.reset_hour_utc`` so an
operator who wants the daily budget to roll over at, say, 06:00 IST
(00:30 UTC) can configure that.

Public surface:

* :func:`today_spend_inr` — sum cost over today's investigations.
* :func:`enforce_budget` — main gate; returns the model the orchestrator
  should actually use (which may be a haiku fallback) or ``None`` to mean
  "block this investigation". Writes one row to ``budget_audit`` per call.
* :func:`remaining_budget_inr` — helper used by the cost dashboard.
* :class:`BudgetExceededError` — raised by callers when ``enforce_budget``
  returns ``None`` and the action is ``pause``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from zabbix_ai.services.pricing import cost_inr

if TYPE_CHECKING:
    from zabbix_ai.config import Settings
    from zabbix_ai.memory import Memory


# Model the orchestrator falls back to when over budget and the action is
# 'haiku_only'. Kept as a module-level constant so it's grep-able and
# easy to change in lockstep with pricing.py.
HAIKU_MODEL = "claude-haiku-4-5-20251001"


class BudgetExceededError(RuntimeError):
    """Raised when the daily INR cap is hit and the policy is to pause.

    The string form includes the reason code (e.g. ``over_budget_paused``)
    so log scrapers can match it.
    """


def _today_start(now: datetime, reset_hour_utc: int) -> datetime:
    """Return the start of the current budget day in UTC.

    If ``now`` is at or after ``reset_hour_utc`` today, the day starts at
    today's ``reset_hour_utc``. Otherwise it started yesterday at
    ``reset_hour_utc``.
    """
    candidate = now.replace(hour=reset_hour_utc, minute=0, second=0,
                            microsecond=0)
    if now < candidate:
        candidate = candidate - timedelta(days=1)
    return candidate


async def today_spend_inr(memory: Memory, *, fx: float,
                          reset_hour_utc: int) -> float:
    """Sum INR cost over today's investigations.

    "Today" starts at ``reset_hour_utc`` UTC. Per-model token totals are
    fetched in one query, then converted with the same pricing table the
    cost dashboard uses, so the budget gate never disagrees with the
    dashboard about today's spend.
    """
    start = _today_start(datetime.now(UTC), reset_hour_utc).isoformat()
    rows = await memory.fetchall(
        """SELECT COALESCE(model, ''),
                  COALESCE(SUM(tokens_in), 0),
                  COALESCE(SUM(tokens_out), 0)
           FROM investigations
           WHERE started_at >= ?
           GROUP BY model""",
        (start,),
    )
    total = 0.0
    for model, t_in, t_out in rows:
        total += cost_inr(model, t_in or 0, t_out or 0, fx)
    return total


async def _audit(memory: Memory, *, action: str, spent: float, limit: float,
                model_requested: str, model_effective: str | None,
                investigation_id: int | None) -> None:
    await memory.execute(
        """INSERT INTO budget_audit
           (ts, action, daily_spent_inr, daily_limit_inr,
            model_requested, model_effective, investigation_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(UTC).isoformat(), action, float(spent), float(limit),
         model_requested, model_effective, investigation_id),
    )


async def enforce_budget(
    memory: Memory, settings: Settings, *,
    investigation_id: int | None = None,
    model_requested: str,
) -> tuple[str | None, str]:
    """Decide whether the next investigation may run, and on what model.

    Returns ``(effective_model, reason)``. ``effective_model`` is:

    * ``model_requested`` — under cap or cap is disabled.
    * ``HAIKU_MODEL`` — over cap, ``over_budget_action='haiku_only'``.
    * ``None`` — over cap, ``over_budget_action='pause'``; caller should
      raise :class:`BudgetExceededError` or return HTTP 503.

    Side effect: every call (except the cap=0 fast path) writes one row
    to ``budget_audit``.
    """
    cap = float(settings.budget.daily_inr_cap or 0.0)
    if cap <= 0.0:
        # Cap disabled — no audit row, just allow.
        return model_requested, "no_cap"

    fx = float(settings.budget.usd_to_inr or 83.0)
    reset_hour = int(settings.budget.reset_hour_utc or 0)
    spent = await today_spend_inr(memory, fx=fx, reset_hour_utc=reset_hour)

    if spent < cap:
        await _audit(memory, action="allowed", spent=spent, limit=cap,
                     model_requested=model_requested,
                     model_effective=model_requested,
                     investigation_id=investigation_id)
        return model_requested, "allowed"

    action = settings.budget.over_budget_action
    if action == "haiku_only":
        await _audit(memory, action="downgraded_haiku", spent=spent, limit=cap,
                     model_requested=model_requested,
                     model_effective=HAIKU_MODEL,
                     investigation_id=investigation_id)
        return HAIKU_MODEL, "over_budget_haiku_fallback"
    if action == "pause":
        await _audit(memory, action="paused", spent=spent, limit=cap,
                     model_requested=model_requested,
                     model_effective=None,
                     investigation_id=investigation_id)
        return None, "over_budget_paused"
    # warn → allow but mark
    await _audit(memory, action="allowed", spent=spent, limit=cap,
                 model_requested=model_requested,
                 model_effective=model_requested,
                 investigation_id=investigation_id)
    return model_requested, "over_budget_warn_only"


async def remaining_budget_inr(memory: Memory, settings: Settings) -> dict:
    """Return a summary suitable for the cost dashboard headline.

    Keys:
      * ``limit`` — configured cap in INR (None if disabled).
      * ``spent`` — INR spent so far in the current budget day.
      * ``remaining`` — limit minus spent (clamped at 0; None if disabled).
      * ``pct_remaining`` — 0 to 100 (None if disabled).
      * ``reset_at`` — ISO timestamp when the budget day next rolls over
        (None if disabled).
      * ``status`` — ``ok`` | ``haiku-fallback`` | ``paused`` | ``warn``
        | ``disabled``.
      * ``action`` — the configured ``over_budget_action``.
    """
    cap = float(settings.budget.daily_inr_cap or 0.0)
    fx = float(settings.budget.usd_to_inr or 83.0)
    reset_hour = int(settings.budget.reset_hour_utc or 0)
    if cap <= 0.0:
        spent = await today_spend_inr(memory, fx=fx, reset_hour_utc=reset_hour)
        return {
            "limit": None, "spent": spent, "remaining": None,
            "pct_remaining": None, "reset_at": None,
            "status": "disabled",
            "action": settings.budget.over_budget_action,
        }
    spent = await today_spend_inr(memory, fx=fx, reset_hour_utc=reset_hour)
    remaining = max(0.0, cap - spent)
    pct = 100.0 * remaining / cap if cap > 0 else 0.0
    next_reset = _today_start(datetime.now(UTC), reset_hour) + timedelta(days=1)
    if spent < cap:
        status = "ok"
    else:
        action = settings.budget.over_budget_action
        status = {"haiku_only": "haiku-fallback",
                  "pause": "paused",
                  "warn": "warn"}.get(action, "ok")
    return {
        "limit": cap, "spent": spent, "remaining": remaining,
        "pct_remaining": pct, "reset_at": next_reset.isoformat(),
        "status": status,
        "action": settings.budget.over_budget_action,
    }
