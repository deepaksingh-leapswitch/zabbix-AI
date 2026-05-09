from __future__ import annotations

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from zabbix_ai.admin import auth, users

router = APIRouter()


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "admin/login.html",
        {"flashes": [], "user": None, "active": "login"},
    )


@router.post("/admin/login")
async def login_submit(request: Request, username: str = Form(...),
                       password: str = Form(...),
                       totp_code: str = Form("")) -> RedirectResponse:
    memory = request.app.state.memory
    secret = request.app.state.session_secret
    ttl = request.app.state.session_ttl

    user = await users.get_user_by_username(memory, username)
    if not user or user["disabled"]:
        return _login_error(request, "invalid credentials")
    if not users.verify_password(password, user["password_hash"]):
        return _login_error(request, "invalid credentials")

    # First-time login: enrollment flow handled via separate page
    if not user["totp_enrolled"]:
        # Stash a short-lived "pre-totp" cookie
        token = auth._serializer(secret).dumps({"pre": user["id"]})
        resp = RedirectResponse(url="/admin/enroll-totp", status_code=303)
        resp.set_cookie("zai_pretotp", token, max_age=300,
                         httponly=True, secure=request.app.state.cookie_secure,
                         samesite="lax")
        return resp

    if not totp_code or not users.verify_totp(user["totp_secret"], totp_code):
        return _login_error(request, "TOTP required or invalid")

    cookie = await auth.create_session(
        memory, user_id=user["id"], secret=secret, ttl_seconds=ttl,
        user_agent=request.headers.get("user-agent", ""),
        ip=request.client.host if request.client else "",
    )
    await users.update_last_login(memory, user["id"])
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie("zai_session", cookie, max_age=ttl,
                     httponly=True, secure=request.app.state.cookie_secure,
                     samesite="lax")
    return resp


@router.get("/admin/enroll-totp", response_class=HTMLResponse)
async def enroll_page(request: Request,
                      zai_pretotp: str | None = Cookie(default=None)) -> HTMLResponse:
    if not zai_pretotp:
        return RedirectResponse("/admin/login", status_code=303)
    secret = request.app.state.session_secret
    try:
        payload = auth._serializer(secret).loads(zai_pretotp)
    except Exception:
        return RedirectResponse("/admin/login", status_code=303)
    user_id = payload.get("pre")
    memory = request.app.state.memory
    row = await memory.fetchone(
        "SELECT username, totp_secret FROM users WHERE id=?", (user_id,),
    )
    if not row:
        return RedirectResponse("/admin/login", status_code=303)
    username, totp_secret = row
    uri = users.totp_provisioning_uri(username, totp_secret)
    return request.app.state.templates.TemplateResponse(
        request, "admin/enroll_totp.html",
        {"flashes": [], "user": None, "active": "enroll",
         "totp_uri": uri, "totp_secret": totp_secret, "username": username},
    )


@router.post("/admin/enroll-totp")
async def enroll_submit(request: Request, totp_code: str = Form(...),
                         zai_pretotp: str | None = Cookie(default=None),
                         ) -> RedirectResponse:
    if not zai_pretotp:
        return RedirectResponse("/admin/login", status_code=303)
    secret = request.app.state.session_secret
    ttl = request.app.state.session_ttl
    try:
        payload = auth._serializer(secret).loads(zai_pretotp)
    except Exception:
        return RedirectResponse("/admin/login", status_code=303)
    user_id = payload["pre"]
    memory = request.app.state.memory
    row = await memory.fetchone(
        "SELECT totp_secret FROM users WHERE id=?", (user_id,),
    )
    if not row or not users.verify_totp(row[0], totp_code):
        return _login_error(request, "TOTP code didn't match — try again")
    await users.set_totp_enrolled(memory, user_id)
    cookie = await auth.create_session(
        memory, user_id=user_id, secret=secret, ttl_seconds=ttl,
    )
    await users.update_last_login(memory, user_id)
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie("zai_session", cookie, max_age=ttl,
                     httponly=True, secure=request.app.state.cookie_secure,
                     samesite="lax")
    resp.delete_cookie("zai_pretotp")
    return resp


@router.get("/admin/logout")
async def logout(request: Request,
                  zai_session: str | None = Cookie(default=None),
                  ) -> RedirectResponse:
    if zai_session:
        secret = request.app.state.session_secret
        try:
            payload = auth._serializer(secret).loads(zai_session)
            await auth.destroy_session(request.app.state.memory,
                                         payload["sid"])
        except Exception:
            pass
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("zai_session")
    return resp


def _login_error(request: Request, msg: str) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "admin/login.html",
        {"flashes": [{"kind": "err", "text": msg}],
         "user": None, "active": "login"},
        status_code=400,
    )
