from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from zabbix_ai.admin.auth import login_required

router = APIRouter()

# Approximate token cost constants (USD per 1M tokens) for a rough estimate
_COST_PER_1M_IN = 3.0   # claude-sonnet-4 input
_COST_PER_1M_OUT = 15.0  # claude-sonnet-4 output


def _today_start() -> str:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _week_start() -> str:
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    return week_ago.isoformat()


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request,
                    user: dict = Depends(login_required())) -> HTMLResponse:
    memory = request.app.state.memory
    today = _today_start()
    week = _week_start()

    # Investigations today
    row = await memory.fetchone(
        "SELECT COUNT(*) FROM investigations WHERE started_at >= ?", (today,),
    )
    inv_today = row[0] if row else 0

    # Investigations this week
    row = await memory.fetchone(
        "SELECT COUNT(*) FROM investigations WHERE started_at >= ?", (week,),
    )
    inv_week = row[0] if row else 0

    # Average duration today (ms)
    row = await memory.fetchone(
        "SELECT AVG(duration_ms) FROM investigations "
        "WHERE started_at >= ? AND duration_ms IS NOT NULL",
        (today,),
    )
    avg_duration_ms = int(row[0]) if row and row[0] is not None else 0

    # Tokens today
    row = await memory.fetchone(
        """SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0)
           FROM investigations WHERE started_at >= ?""",
        (today,),
    )
    tokens_in_today = row[0] if row else 0
    tokens_out_today = row[1] if row else 0
    cost_today = (tokens_in_today / 1_000_000 * _COST_PER_1M_IN +
                  tokens_out_today / 1_000_000 * _COST_PER_1M_OUT)

    # Top 5 hosts by host_fact count
    top_hosts = await memory.fetchall(
        """SELECT hostid, COUNT(*) as cnt FROM host_facts
           GROUP BY hostid ORDER BY cnt DESC LIMIT 5""",
    )

    # Top 5 patterns by occurrences
    top_patterns = await memory.fetchall(
        """SELECT signature, occurrences, typical_root_cause
           FROM patterns ORDER BY occurrences DESC LIMIT 5""",
    )

    return request.app.state.templates.TemplateResponse(
        request, "admin/dashboard.html",
        {
            "user": user,
            "flashes": [],
            "active": "dashboard",
            "inv_today": inv_today,
            "inv_week": inv_week,
            "avg_duration_ms": avg_duration_ms,
            "tokens_in_today": tokens_in_today,
            "tokens_out_today": tokens_out_today,
            "cost_today": round(cost_today, 4),
            "top_hosts": top_hosts,
            "top_patterns": top_patterns,
        },
    )
