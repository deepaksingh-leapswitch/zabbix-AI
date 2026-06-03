"""System status page (v1.4).

Aggregates app metadata, DB stats, connection health, secrets and
investigation counts into a single view. Available as HTML at
``/admin/status`` and as JSON at ``/admin/status.json`` for external
monitoring (e.g. uptime probes).

Both endpoints are admin-only and rate-limited at 30/minute.
"""
from __future__ import annotations

import os
import platform
import sys
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from zabbix_ai import __version__
from zabbix_ai.admin.auth import login_required
from zabbix_ai.admin.rate_limit import limiter
from zabbix_ai.services.connection_health import get_health

router = APIRouter()

# Tracked at import time so the status page can show how long the process
# has been running. Re-imports (e.g. test fixtures) reset this — acceptable
# because the live process imports exactly once.
_PROCESS_START = datetime.now(UTC)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ago(value: str | None, *, now: datetime | None = None) -> str:
    """Render an ISO-8601 timestamp as a human 'N seconds/minutes ago' phrase.

    Returns an empty string when ``value`` is empty/unparseable so callers
    can safely concatenate.
    """
    dt = _parse_iso(value)
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "in the future"
    if secs < 60:
        return f"{secs} second{'s' if secs != 1 else ''} ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


async def _count(memory: Any, sql: str, params: tuple = ()) -> int:
    try:
        row = await memory.fetchone(sql, params)
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


async def _scalar(memory: Any, sql: str, params: tuple = ()) -> Any:
    try:
        row = await memory.fetchone(sql, params)
        return row[0] if row else None
    except Exception:
        return None


async def _gather_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    memory = request.app.state.memory
    now = datetime.now(UTC)

    # ── App / process ─────────────────────────────────────────────────────
    uptime_seconds = int((now - _PROCESS_START).total_seconds())
    app_info = {
        "version": __version__,
        "process_start_at": _PROCESS_START.isoformat(),
        "uptime_seconds": uptime_seconds,
        "uptime_pretty": _ago(_PROCESS_START.isoformat(), now=now),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }

    # ── Database ──────────────────────────────────────────────────────────
    db_path = settings.sqlite_path
    db_size = None
    try:
        db_size = os.path.getsize(db_path)
    except OSError:
        db_size = None
    schema_version = await _scalar(memory, "SELECT MAX(version) FROM schema_version")
    last_admin_audit = await _scalar(
        memory, "SELECT MAX(ts) FROM admin_audit_log"
    )
    db_info = {
        "path": db_path,
        "size_bytes": db_size,
        "schema_version": int(schema_version) if schema_version is not None else None,
        "last_admin_audit_at": last_admin_audit,
        "last_admin_audit_ago": _ago(last_admin_audit, now=now),
    }

    # ── Connection health ─────────────────────────────────────────────────
    health = await get_health(memory)

    def _health_for(kind: str, name: str) -> dict[str, Any]:
        h = health.get((kind, name), {})
        succ = h.get("last_success_at")
        err_at = h.get("last_error_at")
        err = h.get("last_error", "")
        # "ok" is True if we have a success that is no older than the most
        # recent failure (or there has never been a failure).
        s_dt = _parse_iso(succ)
        e_dt = _parse_iso(err_at)
        if s_dt is None and e_dt is None:
            state = "unknown"
        elif e_dt is None:
            state = "ok"
        elif s_dt is None:
            state = "error"
        else:
            state = "ok" if s_dt >= e_dt else "error"
        return {
            "kind": kind,
            "name": name,
            "state": state,
            "last_success_at": succ,
            "last_success_ago": _ago(succ, now=now),
            "last_error_at": err_at,
            "last_error_ago": _ago(err_at, now=now),
            "last_error": err,
        }

    zabbix_health = [
        _health_for("zabbix", inst.name)
        for inst in settings.zabbix_instances
    ]
    slack_health = _health_for("slack", "primary") if settings.slack else None
    anthropic_health = _health_for("anthropic", "primary")

    # ── Secrets ──────────────────────────────────────────────────────────
    secrets_count = await _count(memory, "SELECT COUNT(*) FROM secrets_kv")
    connections_count = await _count(memory, "SELECT COUNT(*) FROM connections")

    # ── Investigations ───────────────────────────────────────────────────
    inv_total = await _count(memory, "SELECT COUNT(*) FROM investigations")
    inv_running = await _count(
        memory,
        "SELECT COUNT(*) FROM investigations WHERE status='running'",
    )

    # ── Memory tables (best-effort) ──────────────────────────────────────
    table_counts: dict[str, int | None] = {}
    for tbl in ("patterns", "host_facts", "used_tokens"):
        try:
            row = await memory.fetchone(f"SELECT COUNT(*) FROM {tbl}")
            table_counts[tbl] = int(row[0]) if row and row[0] is not None else 0
        except Exception:
            table_counts[tbl] = None

    # ── Background tasks ─────────────────────────────────────────────────
    bg_task = getattr(request.app.state, "bootstrap_warning_task", None)
    bg_tasks: dict[str, Any] = {}
    if bg_task is not None:
        bg_tasks["bootstrap_warning_task"] = {
            "done": bool(bg_task.done()),
            "cancelled": bool(getattr(bg_task, "cancelled", lambda: False)()),
        }

    return {
        "generated_at": now.isoformat(),
        "app": app_info,
        "database": db_info,
        "zabbix": zabbix_health,
        "slack": slack_health,
        "anthropic": anthropic_health,
        "secrets_count": secrets_count,
        "connections_count": connections_count,
        "investigations": {"total": inv_total, "running": inv_running},
        "tables": table_counts,
        "background_tasks": bg_tasks,
        "workers": getattr(request.app.state, "worker_health", {}),
    }


@router.get("/admin/status", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def status_page(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    data = await _gather_status(request)
    return request.app.state.templates.TemplateResponse(
        request, "admin/status.html",
        {
            "user": user,
            "flashes": [],
            "active": "status",
            "data": data,
        },
    )


@router.get("/admin/status.json")
@limiter.limit("30/minute")
async def status_json(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> JSONResponse:
    data = await _gather_status(request)
    return JSONResponse(data)
