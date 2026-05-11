"""Admin UI for managing the Zabbix host → HostBill service mapping.

The auto-linker (`services/hostbill_link.py`) writes one row per
Zabbix host on first investigation, attempting tag → IP → hostname
matchers in that order. When the linker can't find a match or finds
multiple low-confidence ones, the row lands here for an admin to
correct by hand.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from zabbix_ai.admin.admin_audit import log_admin_event
from zabbix_ai.admin.auth import login_required
from zabbix_ai.admin.csrf import get_csrf_token
from zabbix_ai.admin.rate_limit import limiter

router = APIRouter()


def _tmpl(request: Request, name: str, ctx: dict) -> HTMLResponse:
    ctx.setdefault("csrf_token", get_csrf_token(request))
    return request.app.state.templates.TemplateResponse(request, name, ctx)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@router.get("/admin/connections/hostbill/links", response_class=HTMLResponse)
async def hostbill_links(
    request: Request,
    show: str = "all",
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = request.app.state.memory
    where = ""
    params: tuple = ()
    if show == "unlinked":
        where = "WHERE linked_by='unlinked' OR confidence='low'"
    rows = await memory.fetchall(
        "SELECT zabbix_instance, zabbix_hostid, hostbill_service_id, "
        "       hostbill_client_id, hostbill_client_name, hostbill_domain, "
        "       linked_at, linked_by, confidence "
        "FROM host_hostbill_link " + where +
        " ORDER BY linked_at DESC LIMIT 500",
        params,
    )
    keys = ("zabbix_instance", "zabbix_hostid", "hostbill_service_id",
            "hostbill_client_id", "hostbill_client_name", "hostbill_domain",
            "linked_at", "linked_by", "confidence")
    links = [dict(zip(keys, r, strict=False)) for r in rows]
    return _tmpl(request, "admin/hostbill_links.html", {
        "user": user, "flashes": [], "active": "connections",
        "links": links, "show": show,
    })


@router.get(
    "/admin/connections/hostbill/links/{instance}/{hostid}/edit",
    response_class=HTMLResponse,
)
async def hostbill_link_edit(
    request: Request,
    instance: str,
    hostid: int,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = request.app.state.memory
    row = await memory.fetchone(
        "SELECT zabbix_instance, zabbix_hostid, hostbill_service_id, "
        "       hostbill_client_id, hostbill_client_name, hostbill_domain, "
        "       linked_at, linked_by, confidence "
        "FROM host_hostbill_link "
        "WHERE zabbix_instance=? AND zabbix_hostid=?",
        (instance, hostid),
    )
    link = None
    if row:
        keys = ("zabbix_instance", "zabbix_hostid", "hostbill_service_id",
                "hostbill_client_id", "hostbill_client_name",
                "hostbill_domain", "linked_at", "linked_by", "confidence")
        link = dict(zip(keys, row, strict=False))
    return _tmpl(request, "admin/hostbill_link_edit.html", {
        "user": user, "flashes": [], "active": "connections",
        "link": link, "instance": instance, "hostid": hostid,
    })


@router.post("/admin/connections/hostbill/links/{instance}/{hostid}")
@limiter.limit("30/minute")
async def hostbill_link_save(
    request: Request,
    instance: str,
    hostid: int,
    hostbill_service_id: str = Form(""),
    hostbill_client_id: str = Form(""),
    hostbill_client_name: str = Form(""),
    hostbill_domain: str = Form(""),
    user: dict = Depends(login_required("admin")),
) -> RedirectResponse:
    memory = request.app.state.memory

    def _maybe_int(s: str) -> int | None:
        s = s.strip()
        return int(s) if s.isdigit() else None

    service_id = _maybe_int(hostbill_service_id)
    client_id = _maybe_int(hostbill_client_id)
    client_name = hostbill_client_name.strip()
    domain = hostbill_domain.strip()

    await memory.execute(
        "INSERT INTO host_hostbill_link "
        "  (zabbix_instance, zabbix_hostid, hostbill_service_id, "
        "   hostbill_client_id, hostbill_client_name, hostbill_domain, "
        "   linked_at, linked_by, confidence) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(zabbix_instance, zabbix_hostid) DO UPDATE SET "
        "  hostbill_service_id=excluded.hostbill_service_id, "
        "  hostbill_client_id=excluded.hostbill_client_id, "
        "  hostbill_client_name=excluded.hostbill_client_name, "
        "  hostbill_domain=excluded.hostbill_domain, "
        "  linked_at=excluded.linked_at, "
        "  linked_by=excluded.linked_by, "
        "  confidence=excluded.confidence",
        (instance, hostid, service_id, client_id, client_name, domain,
         _now_iso(), "manual", "high"),
    )
    await log_admin_event(
        memory, event_type="hostbill_link_manual",
        by_user=user["username"],
        target=f"{instance}:{hostid}",
        details={"service_id": service_id, "client_id": client_id,
                 "client_name": client_name, "domain": domain},
    )
    return RedirectResponse(
        "/admin/connections/hostbill/links", status_code=303,
    )


@router.post("/admin/connections/hostbill/links/refresh")
@limiter.limit("3/minute")
async def hostbill_link_refresh(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> JSONResponse:
    """Trigger an immediate refresh of the link table for all hosts.

    The actual sync function lives in services/hostbill_link.py:refresh_all_links.
    Returns counts of linked / unlinked / errors. Audit-logged.
    """
    memory = request.app.state.memory
    # Lazy import to avoid pulling the HostBill client at module import time.
    try:
        from zabbix_ai.services import hostbill_link as hb
    except ImportError as exc:  # pragma: no cover
        return JSONResponse(
            {"ok": False, "message": f"hostbill_link service missing: {exc}"},
            status_code=500,
        )
    zabbix_clients = getattr(request.app.state, "zabbix_clients", {}) or {}
    hostbill_client = getattr(request.app.state, "hostbill_client", None)
    try:
        result = await hb.refresh_all_links(
            memory, hostbill_client, zabbix_clients,
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "message": str(exc)}, status_code=500,
        )
    await log_admin_event(
        memory, event_type="hostbill_link_refresh",
        by_user=user["username"],
        target="hostbill:primary",
        details=result,
    )
    return JSONResponse({"ok": True, **result})
