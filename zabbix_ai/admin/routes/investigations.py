from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from zabbix_ai.admin.auth import login_required

router = APIRouter()
_PAGE_SIZE = 50


@router.get("/admin/investigations", response_class=HTMLResponse)
async def investigations_list(
    request: Request,
    user: dict = Depends(login_required()),
    source: str = Query(""),
    hostid: str = Query(""),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    memory = request.app.state.memory
    where: list[str] = []
    params: list = []
    if source:
        where.append("source = ?")
        params.append(source)
    if hostid:
        where.append("hostid = ?")
        params.append(hostid)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * _PAGE_SIZE

    rows = await memory.fetchall(
        f"""SELECT id, started_at, source, eventid, hostid, hostname,
                   model, duration_ms,
                   COALESCE(tokens_in,0)+COALESCE(tokens_out,0),
                   SUBSTR(COALESCE(summary,''),1,80)
            FROM investigations {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        (*tuple(params), _PAGE_SIZE, offset),
    )
    count_row = await memory.fetchone(
        f"SELECT COUNT(*) FROM investigations {where_sql}", tuple(params),
    )
    total = count_row[0] if count_row else 0
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    return request.app.state.templates.TemplateResponse(
        request, "admin/investigations_list.html",
        {
            "user": user,
            "flashes": [],
            "active": "investigations",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "source": source,
            "hostid": hostid,
        },
    )


@router.get("/admin/investigations/{inv_id}", response_class=HTMLResponse)
async def investigation_detail(
    request: Request,
    inv_id: int,
    user: dict = Depends(login_required()),
) -> HTMLResponse:
    memory = request.app.state.memory
    row = await memory.fetchone(
        """SELECT id, started_at, source, eventid, hostid, hostname,
                  model, duration_ms, tokens_in, tokens_out,
                  summary, root_cause, suggested_actions, confidence,
                  pattern_signature
           FROM investigations WHERE id=?""",
        (inv_id,),
    )
    if not row:
        from fastapi.responses import Response
        return Response(status_code=404, content="Investigation not found")

    keys = ("id", "started_at", "source", "eventid", "hostid", "hostname",
            "model", "duration_ms", "tokens_in", "tokens_out",
            "summary", "root_cause", "suggested_actions", "confidence",
            "pattern_signature")
    inv = dict(zip(keys, row, strict=False))

    # Tool transcript from audit_log
    audit_rows = await memory.fetchall(
        """SELECT ts, event_type, tool_name, tool_input, tool_output
           FROM audit_log WHERE investigation_id=? ORDER BY id ASC""",
        (inv_id,),
    )

    return request.app.state.templates.TemplateResponse(
        request, "admin/investigation_detail.html",
        {
            "user": user,
            "flashes": [],
            "active": "investigations",
            "inv": inv,
            "audit_rows": audit_rows,
        },
    )
