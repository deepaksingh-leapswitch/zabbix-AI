from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from zabbix_ai.admin.auth import login_required

router = APIRouter()
_PAGE_SIZE = 100


@router.get("/admin/audit", response_class=HTMLResponse)
async def audit_list(
    request: Request,
    user: dict = Depends(login_required()),
    investigation_id: str = Query(""),
    event_type: str = Query(""),
    since: str = Query(""),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    memory = request.app.state.memory
    where: list[str] = []
    params: list = []

    if investigation_id:
        where.append("investigation_id = ?")
        params.append(investigation_id)
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if since:
        where.append("ts >= ?")
        params.append(since)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * _PAGE_SIZE

    rows = await memory.fetchall(
        f"""SELECT id, ts, investigation_id, event_type, tool_name,
                   SUBSTR(COALESCE(tool_input,''),1,120),
                   user, source
            FROM audit_log {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        (*tuple(params), _PAGE_SIZE, offset),
    )
    count_row = await memory.fetchone(
        f"SELECT COUNT(*) FROM audit_log {where_sql}", tuple(params),
    )
    total = count_row[0] if count_row else 0
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    # Distinct event types for filter dropdown
    et_rows = await memory.fetchall("SELECT DISTINCT event_type FROM audit_log ORDER BY 1")
    event_types = [r[0] for r in et_rows]

    return request.app.state.templates.TemplateResponse(
        request, "admin/audit_list.html",
        {
            "user": user,
            "flashes": [],
            "active": "audit",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "investigation_id": investigation_id,
            "event_type": event_type,
            "since": since,
            "event_types": event_types,
        },
    )
