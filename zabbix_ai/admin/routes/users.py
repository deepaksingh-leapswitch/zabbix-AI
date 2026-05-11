"""Admin user management routes (v1.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from zabbix_ai.admin import users as users_mod
from zabbix_ai.admin.admin_audit import log_admin_event
from zabbix_ai.admin.auth import login_required
from zabbix_ai.admin.csrf import get_csrf_token
from zabbix_ai.admin.rate_limit import limiter

router = APIRouter()

_VALID_ROLES = {"admin", "operator", "viewer"}


def _memory(request: Request):
    return request.app.state.memory


def _tmpl(request: Request, name: str, ctx: dict) -> HTMLResponse:
    ctx.setdefault("csrf_token", get_csrf_token(request))
    return request.app.state.templates.TemplateResponse(request, name, ctx)


async def _require_target(request: Request, user_id: int) -> dict:
    target = await users_mod.get_user_by_id(_memory(request), user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    return target


# ─── list ────────────────────────────────────────────────────────────────────

@router.get("/admin/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    rows = await users_mod.list_users(_memory(request))
    return _tmpl(request, "admin/users/list.html", {
        "user": user, "flashes": [], "active": "users", "rows": rows,
    })


# ─── create ──────────────────────────────────────────────────────────────────

@router.get("/admin/users/new", response_class=HTMLResponse)
async def users_new(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    return _tmpl(request, "admin/users/new.html", {
        "user": user, "flashes": [], "active": "users",
        "valid_roles": sorted(_VALID_ROLES),
    })


@router.post("/admin/users/create", response_model=None)
@limiter.limit("30/minute")
async def users_create(
    request: Request,
    user: dict = Depends(login_required("admin")),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
) -> RedirectResponse | HTMLResponse:
    memory = _memory(request)
    username = username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if not password:
        raise HTTPException(status_code=400, detail="password required")
    if role not in _VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid role '{role}'; must be one of "
                   f"{sorted(_VALID_ROLES)}",
        )

    existing = await users_mod.get_user_by_username(memory, username)
    if existing:
        return _tmpl(request, "admin/users/new.html", {
            "user": user, "active": "users",
            "valid_roles": sorted(_VALID_ROLES),
            "flashes": [{"kind": "err",
                         "text": f"username '{username}' already exists"}],
            "form": {"username": username, "role": role},
        })

    created = await users_mod.create_user(
        memory, username=username, password=password, role=role,
    )
    await log_admin_event(
        memory, event_type="user_create",
        by_user=user["username"],
        target=f"user:{username}",
        details={"role": role, "user_id": created["id"]},
    )
    return RedirectResponse("/admin/users", status_code=303)


# ─── edit ────────────────────────────────────────────────────────────────────

@router.get("/admin/users/{user_id}/edit", response_class=HTMLResponse)
async def users_edit(
    request: Request,
    user_id: int,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    target = await _require_target(request, user_id)
    return _tmpl(request, "admin/users/edit.html", {
        "user": user, "flashes": [], "active": "users",
        "target": target,
        "valid_roles": sorted(_VALID_ROLES),
        "is_self": (user_id == user["user_id"]),
    })


# ─── role change ─────────────────────────────────────────────────────────────

@router.post("/admin/users/{user_id}/role")
@limiter.limit("30/minute")
async def users_set_role(
    request: Request,
    user_id: int,
    user: dict = Depends(login_required("admin")),
    role: str = Form(...),
) -> RedirectResponse:
    memory = _memory(request)
    target = await _require_target(request, user_id)
    if role not in _VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid role '{role}'; must be one of "
                   f"{sorted(_VALID_ROLES)}",
        )
    # self-demotion guard
    if user_id == user["user_id"] and role != target["role"]:
        raise HTTPException(
            status_code=400,
            detail="you cannot change your own role",
        )
    # last-admin guard: refuse to demote the last enabled admin
    if (target["role"] == "admin" and role != "admin"
            and await users_mod.count_admins(memory) <= 1):
        raise HTTPException(
            status_code=400,
            detail="cannot demote the last admin",
        )

    old_role = target["role"]
    await users_mod.set_role(memory, user_id, role)
    await log_admin_event(
        memory, event_type="user_role_change",
        by_user=user["username"],
        target=f"user:{target['username']}",
        details={"from": old_role, "to": role},
    )
    return RedirectResponse(f"/admin/users/{user_id}/edit", status_code=303)


# ─── password ────────────────────────────────────────────────────────────────

@router.post("/admin/users/{user_id}/password")
@limiter.limit("30/minute")
async def users_set_password(
    request: Request,
    user_id: int,
    user: dict = Depends(login_required("admin")),
    new_password: str = Form(...),
) -> RedirectResponse:
    if not new_password:
        raise HTTPException(status_code=400, detail="password required")
    memory = _memory(request)
    target = await _require_target(request, user_id)
    await users_mod.set_password(memory, user_id, new_password)
    await log_admin_event(
        memory, event_type="user_password_reset",
        by_user=user["username"],
        target=f"user:{target['username']}",
    )
    return RedirectResponse(f"/admin/users/{user_id}/edit", status_code=303)


# ─── reset TOTP ──────────────────────────────────────────────────────────────

@router.post("/admin/users/{user_id}/reset-totp")
@limiter.limit("30/minute")
async def users_reset_totp(
    request: Request,
    user_id: int,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    target = await _require_target(request, user_id)
    new_secret = await users_mod.reset_totp(memory, user_id)
    await log_admin_event(
        memory, event_type="user_totp_reset",
        by_user=user["username"],
        target=f"user:{target['username']}",
    )
    # Re-fetch target so the form reflects the cleared enrolled flag.
    target = await users_mod.get_user_by_id(memory, user_id)
    uri = users_mod.totp_provisioning_uri(target["username"], new_secret)
    return _tmpl(request, "admin/users/edit.html", {
        "user": user, "active": "users",
        "target": target,
        "valid_roles": sorted(_VALID_ROLES),
        "is_self": (user_id == user["user_id"]),
        "flash_new_totp_secret": new_secret,
        "flash_new_totp_uri": uri,
        "flashes": [{"kind": "ok",
                     "text": "TOTP secret reset — share the URI below "
                             "with the user before navigating away."}],
    })


# ─── lock / unlock ───────────────────────────────────────────────────────────

@router.post("/admin/users/{user_id}/lock")
@limiter.limit("30/minute")
async def users_lock(
    request: Request,
    user_id: int,
    user: dict = Depends(login_required("admin")),
) -> RedirectResponse:
    memory = _memory(request)
    target = await _require_target(request, user_id)
    if user_id == user["user_id"]:
        raise HTTPException(
            status_code=400, detail="you cannot disable your own account",
        )
    new_disabled = not target["disabled"]
    # last-admin guard: refuse to disable last enabled admin
    if (new_disabled and target["role"] == "admin"
            and await users_mod.count_admins(memory) <= 1):
        raise HTTPException(
            status_code=400,
            detail="cannot disable the last admin",
        )
    await users_mod.set_disabled(memory, user_id, new_disabled)
    await log_admin_event(
        memory, event_type="user_lock",
        by_user=user["username"],
        target=f"user:{target['username']}",
        details={"disabled": new_disabled},
    )
    return RedirectResponse(f"/admin/users/{user_id}/edit", status_code=303)


# ─── delete ──────────────────────────────────────────────────────────────────

@router.post("/admin/users/{user_id}/delete")
@limiter.limit("30/minute")
async def users_delete(
    request: Request,
    user_id: int,
    user: dict = Depends(login_required("admin")),
) -> RedirectResponse:
    memory = _memory(request)
    target = await _require_target(request, user_id)
    if user_id == user["user_id"]:
        raise HTTPException(
            status_code=400, detail="you cannot delete your own account",
        )
    if (target["role"] == "admin"
            and await users_mod.count_admins(memory) <= 1):
        raise HTTPException(
            status_code=400, detail="cannot delete the last admin",
        )
    await users_mod.delete_user(memory, user_id)
    await log_admin_event(
        memory, event_type="user_delete",
        by_user=user["username"],
        target=f"user:{target['username']}",
        details={"user_id": user_id, "role": target["role"]},
    )
    return RedirectResponse("/admin/users", status_code=303)
