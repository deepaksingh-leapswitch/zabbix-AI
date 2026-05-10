from __future__ import annotations

import io

import bcrypt
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from zabbix_ai.admin import auth, users
from zabbix_ai.admin.admin_audit import log_admin_event
from zabbix_ai.admin.csrf import get_csrf_token
from zabbix_ai.admin.rate_limit import limiter


def _qr_svg(text: str) -> str:
    """Return an inline SVG <svg>...</svg> string for a QR of `text`."""
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(text, image_factory=factory, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


# Dummy hash used for constant-time comparison when user doesn't exist (#6)
_DUMMY_HASH = bcrypt.hashpw(b"never-used-dummy", bcrypt.gensalt(rounds=12)).decode()

router = APIRouter()


def _real_ip(request: Request) -> str:
    """Return real client IP, honouring X-Forwarded-For from localhost (#13)."""
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    google_sso_enabled = settings.oauth_google is not None
    return request.app.state.templates.TemplateResponse(
        request, "admin/login.html",
        {"flashes": [], "user": None, "active": "login",
         "google_sso_enabled": google_sso_enabled,
         "csrf_token": get_csrf_token(request)},
    )


@router.post("/admin/login")
@limiter.limit("5/minute")
async def login_submit(request: Request, username: str = Form(...),
                       password: str = Form(...),
                       totp_code: str = Form("")) -> RedirectResponse:
    memory = request.app.state.memory
    secret = request.app.state.session_secret
    ttl = request.app.state.session_ttl
    ip = _real_ip(request)

    user = await users.get_user_by_username(memory, username)

    # Always run bcrypt to prevent timing-based username enumeration (#6)
    candidate_hash = (
        user["password_hash"] if user and user.get("password_hash") else _DUMMY_HASH
    )
    password_ok = users.verify_password(password, candidate_hash)

    if not user or user["disabled"] or not password_ok:
        await log_admin_event(
            memory, event_type="login_failure",
            target=username, ip=ip,
            details={"reason": "invalid credentials"},
        )
        return _login_error(request, "invalid credentials")

    # First-time login: enrollment flow handled via separate page
    if not user["totp_enrolled"]:
        # Stash a short-lived "pre-totp" cookie
        token = auth._serializer(secret).dumps({"pre": user["id"]})
        resp = RedirectResponse(url="/admin/enroll-totp", status_code=303)
        resp.set_cookie("zai_pretotp", token, max_age=300,
                         httponly=True, secure=request.app.state.cookie_secure,
                         samesite="strict")
        return resp

    totp_valid = await users.verify_totp_with_replay_check(
        memory, user["id"], user["totp_secret"], totp_code
    )
    if not totp_code or not totp_valid:
        await log_admin_event(
            memory, event_type="login_failure",
            by_user=username, ip=ip,
            details={"reason": "TOTP required or invalid"},
        )
        return _login_error(request, "TOTP required or invalid")

    cookie = await auth.create_session(
        memory, user_id=user["id"], secret=secret, ttl_seconds=ttl,
        user_agent=request.headers.get("user-agent", ""),
        ip=ip,
    )
    await users.update_last_login(memory, user["id"])
    await log_admin_event(
        memory, event_type="login_success",
        by_user=username, ip=ip,
        details={"method": "password+totp"},
    )
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie("zai_session", cookie, max_age=ttl,
                     httponly=True, secure=request.app.state.cookie_secure,
                     samesite="strict")
    return resp


@router.get("/admin/enroll-totp", response_class=HTMLResponse)
@limiter.limit("10/minute")
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
        "SELECT username, totp_secret, totp_enrolled FROM users WHERE id=?",
        (user_id,),
    )
    if not row:
        return RedirectResponse("/admin/login", status_code=303)
    username, totp_secret, totp_enrolled = row

    # #9: If user is already enrolled, don't re-expose the TOTP secret
    if totp_enrolled:
        return RedirectResponse("/admin/login", status_code=303)

    # #9: Render QR only — no plaintext secret or provisioning URI in context
    uri = users.totp_provisioning_uri(username, totp_secret)
    totp_qr_svg = _qr_svg(uri)
    return request.app.state.templates.TemplateResponse(
        request, "admin/enroll_totp.html",
        {"flashes": [], "user": None, "active": "enroll",
         "username": username,
         "totp_qr_svg": totp_qr_svg,
         "csrf_token": get_csrf_token(request)},
    )


@router.post("/admin/enroll-totp")
@limiter.limit("10/minute")
async def enroll_submit(request: Request, totp_code: str = Form(...),
                         zai_pretotp: str | None = Cookie(default=None),
                         ) -> RedirectResponse:
    if not zai_pretotp:
        return RedirectResponse("/admin/login", status_code=303)
    secret = request.app.state.session_secret
    ttl = request.app.state.session_ttl
    ip = _real_ip(request)
    try:
        payload = auth._serializer(secret).loads(zai_pretotp)
    except Exception:
        return RedirectResponse("/admin/login", status_code=303)
    user_id = payload["pre"]
    memory = request.app.state.memory
    row = await memory.fetchone(
        "SELECT totp_secret, username FROM users WHERE id=?", (user_id,),
    )
    if not row:
        return _login_error(request, "TOTP code didn't match — try again")
    totp_secret, username = row
    totp_valid = await users.verify_totp_with_replay_check(
        memory, user_id, totp_secret, totp_code
    )
    if not totp_valid:
        return _login_error(request, "TOTP code didn't match — try again")
    await users.set_totp_enrolled(memory, user_id)
    await log_admin_event(
        memory, event_type="totp_enroll",
        by_user=username, ip=ip,
    )
    cookie = await auth.create_session(
        memory, user_id=user_id, secret=secret, ttl_seconds=ttl,
    )
    await users.update_last_login(memory, user_id)
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie("zai_session", cookie, max_age=ttl,
                     httponly=True, secure=request.app.state.cookie_secure,
                     samesite="strict")
    resp.delete_cookie("zai_pretotp")
    return resp


@router.post("/admin/logout")
async def logout(request: Request,
                  zai_session: str | None = Cookie(default=None),
                  ) -> RedirectResponse:
    """POST-only logout (#1). The GET endpoint is removed to prevent CSRF."""
    if zai_session:
        secret = request.app.state.session_secret
        memory = request.app.state.memory
        ip = _real_ip(request)
        try:
            payload = auth._serializer(secret).loads(zai_session)
            sid = payload.get("sid", "")
            # Fetch username for audit log before destroying
            row = await memory.fetchone(
                "SELECT u.username FROM sessions s JOIN users u ON u.id=s.user_id"
                " WHERE s.sid=?", (sid,),
            )
            username = row[0] if row else None
            await auth.destroy_session(memory, sid)
            await log_admin_event(
                memory, event_type="logout",
                by_user=username, ip=ip,
            )
        except Exception:
            pass
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("zai_session")
    return resp


def _login_error(request: Request, msg: str) -> HTMLResponse:
    settings = request.app.state.settings
    google_sso_enabled = settings.oauth_google is not None
    return request.app.state.templates.TemplateResponse(
        request, "admin/login.html",
        {"flashes": [{"kind": "err", "text": msg}],
         "user": None, "active": "login",
         "google_sso_enabled": google_sso_enabled,
         "csrf_token": get_csrf_token(request)},
        status_code=400,
    )
