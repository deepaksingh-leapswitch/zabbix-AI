from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response

from zabbix_ai.admin.auth import login_required

router = APIRouter()
_PAGE_SIZE = 50


@router.get("/admin/patterns", response_class=HTMLResponse)
async def patterns_list(
    request: Request,
    user: dict = Depends(login_required()),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    memory = request.app.state.memory
    offset = (page - 1) * _PAGE_SIZE
    rows = await memory.fetchall(
        """SELECT signature, occurrences, first_seen, last_seen,
                  SUBSTR(COALESCE(typical_root_cause,''),1,80),
                  SUBSTR(COALESCE(typical_fix,''),1,80)
           FROM patterns ORDER BY occurrences DESC LIMIT ? OFFSET ?""",
        (_PAGE_SIZE, offset),
    )
    count_row = await memory.fetchone("SELECT COUNT(*) FROM patterns")
    total = count_row[0] if count_row else 0
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    return request.app.state.templates.TemplateResponse(
        request, "admin/patterns_list.html",
        {
            "user": user,
            "flashes": [],
            "active": "patterns",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@router.get("/admin/patterns/{signature}", response_class=HTMLResponse)
async def pattern_detail(
    request: Request,
    signature: str,
    user: dict = Depends(login_required()),
) -> HTMLResponse:
    memory = request.app.state.memory
    row = await memory.fetchone(
        """SELECT signature, occurrences, first_seen, last_seen,
                  typical_root_cause, typical_fix, confidence_score
           FROM patterns WHERE signature=?""",
        (signature,),
    )
    if not row:
        return Response(status_code=404, content="Pattern not found")

    keys = ("signature", "occurrences", "first_seen", "last_seen",
            "typical_root_cause", "typical_fix", "confidence_score")
    pattern = dict(zip(keys, row, strict=False))

    # Related investigations
    inv_rows = await memory.fetchall(
        """SELECT id, started_at, source, hostname, confidence
           FROM investigations WHERE pattern_signature=?
           ORDER BY id DESC LIMIT 50""",
        (signature,),
    )

    return request.app.state.templates.TemplateResponse(
        request, "admin/pattern_detail.html",
        {
            "user": user,
            "flashes": [],
            "active": "patterns",
            "pattern": pattern,
            "inv_rows": inv_rows,
        },
    )


@router.get("/admin/host-facts", response_class=HTMLResponse)
async def host_facts_list(
    request: Request,
    user: dict = Depends(login_required()),
    hostid: str = Query(""),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    memory = request.app.state.memory
    where: list[str] = []
    params: list = []
    if hostid:
        where.append("hostid = ?")
        params.append(hostid)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * _PAGE_SIZE

    rows = await memory.fetchall(
        f"""SELECT hostid, key, value, source_investigation_id, learned_at
            FROM host_facts {where_sql}
            ORDER BY hostid, key LIMIT ? OFFSET ?""",
        (*tuple(params), _PAGE_SIZE, offset),
    )
    count_row = await memory.fetchone(
        f"SELECT COUNT(*) FROM host_facts {where_sql}", tuple(params),
    )
    total = count_row[0] if count_row else 0
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    return request.app.state.templates.TemplateResponse(
        request, "admin/host_facts_list.html",
        {
            "user": user,
            "flashes": [],
            "active": "host-facts",
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "hostid": hostid,
        },
    )
