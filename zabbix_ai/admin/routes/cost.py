"""Cost dashboard for v1.4 — admin/cost page + CSV export.

Computes INR cost per investigation from token counts in the
``investigations`` table using :mod:`zabbix_ai.services.pricing`.
The FX rate (USD→INR) is hardcoded via ``DEFAULT_USD_TO_INR`` for v1.4;
a future settings field can override it.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from zabbix_ai.admin.auth import login_required
from zabbix_ai.admin.rate_limit import limiter
from zabbix_ai.services.budget import remaining_budget_inr
from zabbix_ai.services.pricing import (
    DEFAULT_USD_TO_INR,
    MODEL_PRICING,
    cost_inr,
)

router = APIRouter()


def _today_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _fx_rate(request: Request) -> float:
    """Resolve USD→INR rate. Honour settings.pricing_usd_to_inr if present,
    otherwise fall back to the hardcoded default."""
    settings = getattr(request.app.state, "settings", None)
    rate = getattr(settings, "pricing_usd_to_inr", None) if settings else None
    if isinstance(rate, (int, float)) and rate > 0:
        return float(rate)
    return DEFAULT_USD_TO_INR


@router.get("/admin/cost", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def cost_dashboard(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = request.app.state.memory
    fx = _fx_rate(request)

    today_start = _today_start().isoformat()
    month_start = _month_start().isoformat()
    thirty_days_start = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    # ── Today: per-model aggregates, then sum to INR ────────────────────────
    today_rows = await memory.fetchall(
        """SELECT COALESCE(model, ''),
                  COALESCE(SUM(tokens_in), 0),
                  COALESCE(SUM(tokens_out), 0),
                  COUNT(*)
           FROM investigations
           WHERE started_at >= ?
           GROUP BY model""",
        (today_start,),
    )
    today_total_inr = 0.0
    today_count = 0
    for model, t_in, t_out, cnt in today_rows:
        today_total_inr += cost_inr(model, t_in or 0, t_out or 0, fx)
        today_count += cnt or 0
    avg_today_inr = (today_total_inr / today_count) if today_count else 0.0

    # ── This month: per-model aggregates, summed to INR + model split ──────
    month_rows = await memory.fetchall(
        """SELECT COALESCE(model, ''),
                  COALESCE(SUM(tokens_in), 0),
                  COALESCE(SUM(tokens_out), 0),
                  COUNT(*)
           FROM investigations
           WHERE started_at >= ?
           GROUP BY model""",
        (month_start,),
    )
    month_total_inr = 0.0
    month_count = 0
    model_split: list[dict] = []
    for model, t_in, t_out, cnt in month_rows:
        c = cost_inr(model, t_in or 0, t_out or 0, fx)
        month_total_inr += c
        month_count += cnt or 0
        model_split.append({
            "model": model or "(unknown)",
            "tokens_in": t_in or 0,
            "tokens_out": t_out or 0,
            "count": cnt or 0,
            "inr": c,
        })
    # Sort largest spend first, compute percentage for bar widths.
    model_split.sort(key=lambda r: r["inr"], reverse=True)
    if month_total_inr > 0:
        for r in model_split:
            r["pct"] = round(100.0 * r["inr"] / month_total_inr, 1)
    else:
        for r in model_split:
            r["pct"] = 0.0

    # ── 30-day daily breakdown: aggregate (date x model) and roll up by day ─
    daily_rows = await memory.fetchall(
        """SELECT date(started_at) AS d,
                  COALESCE(model, ''),
                  COALESCE(SUM(tokens_in), 0),
                  COALESCE(SUM(tokens_out), 0),
                  COUNT(*)
           FROM investigations
           WHERE started_at >= ?
           GROUP BY d, model
           ORDER BY d""",
        (thirty_days_start,),
    )
    by_day: dict[str, dict] = defaultdict(
        lambda: {"inr": 0.0, "count": 0}
    )
    for d, model, t_in, t_out, cnt in daily_rows:
        if not d:
            continue
        by_day[d]["inr"] += cost_inr(model, t_in or 0, t_out or 0, fx)
        by_day[d]["count"] += cnt or 0
    daily = [
        {"date": d, "inr": v["inr"], "count": v["count"]}
        for d, v in sorted(by_day.items())
    ]
    max_daily_inr = max((d["inr"] for d in daily), default=0.0)
    for d in daily:
        d["pct"] = (
            round(100.0 * d["inr"] / max_daily_inr, 1)
            if max_daily_inr > 0 else 0.0
        )

    # ── Top 10 most expensive investigations ───────────────────────────────
    inv_rows = await memory.fetchall(
        """SELECT id, started_at, hostname, COALESCE(model, ''),
                  COALESCE(tokens_in, 0), COALESCE(tokens_out, 0)
           FROM investigations
           ORDER BY (COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)) DESC
           LIMIT 10""",
    )
    top_investigations = [
        {
            "id": r[0],
            "started_at": r[1],
            "hostname": r[2] or "—",
            "model": r[3] or "—",
            "tokens_in": r[4],
            "tokens_out": r[5],
            "inr": cost_inr(r[3], r[4], r[5], fx),
        }
        for r in inv_rows
    ]

    # ── Top 10 hosts by spend ──────────────────────────────────────────────
    host_rows = await memory.fetchall(
        """SELECT hostid, hostname, COALESCE(model, ''),
                  COALESCE(SUM(tokens_in), 0),
                  COALESCE(SUM(tokens_out), 0),
                  COUNT(*)
           FROM investigations
           WHERE hostid IS NOT NULL
           GROUP BY hostid, hostname, model""",
    )
    host_agg: dict[tuple, dict] = {}
    for hostid, hostname, model, t_in, t_out, cnt in host_rows:
        key = (hostid, hostname)
        agg = host_agg.setdefault(
            key,
            {"hostid": hostid, "hostname": hostname or "—",
             "tokens": 0, "inr": 0.0, "count": 0},
        )
        agg["tokens"] += (t_in or 0) + (t_out or 0)
        agg["inr"] += cost_inr(model, t_in or 0, t_out or 0, fx)
        agg["count"] += cnt or 0
    top_hosts = sorted(host_agg.values(), key=lambda r: r["inr"], reverse=True)[:10]

    # ── Top 10 sources by spend ────────────────────────────────────────────
    src_rows = await memory.fetchall(
        """SELECT COALESCE(source, ''), COALESCE(model, ''),
                  COALESCE(SUM(tokens_in), 0),
                  COALESCE(SUM(tokens_out), 0),
                  COUNT(*)
           FROM investigations
           GROUP BY source, model""",
    )
    src_agg: dict[str, dict] = {}
    for source, model, t_in, t_out, cnt in src_rows:
        agg = src_agg.setdefault(
            source or "(none)",
            {"source": source or "(none)", "inr": 0.0, "count": 0,
             "tokens": 0},
        )
        agg["tokens"] += (t_in or 0) + (t_out or 0)
        agg["inr"] += cost_inr(model, t_in or 0, t_out or 0, fx)
        agg["count"] += cnt or 0
    top_sources = sorted(src_agg.values(), key=lambda r: r["inr"], reverse=True)[:10]

    pricing_summary = [
        {"model": m, "input": p.input_usd_per_mtok, "output": p.output_usd_per_mtok}
        for m, p in MODEL_PRICING.items()
    ]

    # Budget headline. ``budget`` is always present on Settings — when the
    # cap is 0 we still return a dict (status="disabled") so the template
    # can render a uniform line without branching on ``None``.
    settings = request.app.state.settings
    budget_summary = None
    try:
        budget_summary = await remaining_budget_inr(memory, settings)
    except Exception:
        # Defensive: the cost dashboard must not 500 if a future
        # settings refactor breaks the budget service.
        budget_summary = None

    return request.app.state.templates.TemplateResponse(
        request, "admin/cost.html",
        {
            "user": user,
            "flashes": [],
            "active": "cost",
            "today_total_inr": today_total_inr,
            "month_total_inr": month_total_inr,
            "today_count": today_count,
            "month_count": month_count,
            "avg_today_inr": avg_today_inr,
            "daily": daily,
            "model_split": model_split,
            "top_investigations": top_investigations,
            "top_hosts": top_hosts,
            "top_sources": top_sources,
            "fx_rate": fx,
            "pricing_summary": pricing_summary,
            "budget": budget_summary,
        },
    )


@router.get("/admin/cost/export.csv")
@limiter.limit("30/minute")
async def cost_export_csv(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> StreamingResponse:
    memory = request.app.state.memory
    fx = _fx_rate(request)

    rows = await memory.fetchall(
        """SELECT id, started_at, COALESCE(source, ''),
                  COALESCE(hostname, ''), COALESCE(model, ''),
                  COALESCE(tokens_in, 0), COALESCE(tokens_out, 0)
           FROM investigations
           ORDER BY id DESC""",
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "started_at", "source", "hostname", "model",
        "tokens_in", "tokens_out", "cost_inr",
    ])
    for inv_id, started_at, source, hostname, model, t_in, t_out in rows:
        c = cost_inr(model, t_in, t_out, fx)
        writer.writerow([
            inv_id, started_at or "", source, hostname, model,
            t_in, t_out, f"{c:.4f}",
        ])

    body = buf.getvalue()
    return StreamingResponse(
        iter([body]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cost-export.csv"'},
    )
