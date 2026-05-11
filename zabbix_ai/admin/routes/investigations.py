from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from zabbix_ai.admin.admin_audit import log_admin_event
from zabbix_ai.admin.auth import login_required

router = APIRouter()
_PAGE_SIZE = 50


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
                  pattern_signature,
                  resolution_notes, resolution_at, resolution_by,
                  resolution_source, outcome_inferred
           FROM investigations WHERE id=?""",
        (inv_id,),
    )
    if not row:
        return Response(status_code=404, content="Investigation not found")

    keys = ("id", "started_at", "source", "eventid", "hostid", "hostname",
            "model", "duration_ms", "tokens_in", "tokens_out",
            "summary", "root_cause", "suggested_actions", "confidence",
            "pattern_signature",
            "resolution_notes", "resolution_at", "resolution_by",
            "resolution_source", "outcome_inferred")
    inv = dict(zip(keys, row, strict=False))
    # Decode outcome_inferred JSON for the template. We keep the raw text
    # under outcome_inferred_raw so a future debug view can show it
    # verbatim even if parsing fails.
    inv["outcome_inferred_raw"] = inv.get("outcome_inferred")
    if inv.get("outcome_inferred"):
        import json as _json
        try:
            inv["outcome_inferred"] = _json.loads(inv["outcome_inferred"])
        except (TypeError, ValueError):
            inv["outcome_inferred"] = None

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


@router.post("/admin/investigations/{inv_id}/resolution")
async def investigation_set_resolution(
    request: Request,
    inv_id: int,
    user: dict = Depends(login_required("operator")),
    resolution_notes: str = Form(""),
) -> Response:
    """Operator/admin manual override for resolution_notes.

    Records the override in admin_audit_log. Empty submissions clear
    the resolution back to NULL (acts as a "remove" button).
    """
    memory = request.app.state.memory
    existing = await memory.fetchone(
        "SELECT id FROM investigations WHERE id=?", (inv_id,),
    )
    if not existing:
        return Response(status_code=404, content="Investigation not found")

    notes = (resolution_notes or "").strip()
    if not notes:
        await memory.execute(
            """UPDATE investigations
               SET resolution_notes=NULL,
                   resolution_at=NULL,
                   resolution_by=NULL,
                   resolution_source=NULL
               WHERE id=?""",
            (inv_id,),
        )
        action = "cleared"
    else:
        await memory.execute(
            """UPDATE investigations
               SET resolution_notes=?,
                   resolution_at=?,
                   resolution_by=?,
                   resolution_source=?
               WHERE id=?""",
            (notes, _now_iso(), user["username"], "manual", inv_id),
        )
        action = "set"

    await log_admin_event(
        memory,
        event_type="investigation_resolution",
        by_user=user["username"],
        target=f"investigation:{inv_id}",
        ip=getattr(request.client, "host", None) if request.client else None,
        details={"action": action,
                 "length": len(notes)},
    )
    return RedirectResponse(
        f"/admin/investigations/{inv_id}", status_code=303,
    )
