from __future__ import annotations

import secrets
import warnings
from urllib.parse import urlencode

# authlib.jose is deprecated; suppress the warning until we migrate to joserfc.
# Tracked: migrate in v1.4 — see docs/SECURITY.md.
warnings.filterwarnings("ignore", message="authlib.jose module is deprecated",
                        category=DeprecationWarning)

import httpx  # noqa: E402
from authlib.jose import jwt  # noqa: E402
from fastapi import APIRouter, HTTPException, Request  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402

from zabbix_ai.admin import auth, users  # noqa: E402
from zabbix_ai.admin.admin_audit import log_admin_event  # noqa: E402
from zabbix_ai.admin.rate_limit import limiter  # noqa: E402

router = APIRouter()

# Google's OpenID Connect endpoint URLs
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"

_VALID_ISS = {"accounts.google.com", "https://accounts.google.com"}


def _real_ip(request: Request) -> str:
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("/admin/oauth/google/start")
async def start(request: Request) -> RedirectResponse:
    settings = request.app.state.settings
    if settings.oauth_google is None:
        raise HTTPException(status_code=503, detail="Google SSO not configured")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    secret = request.app.state.session_secret
    cookie = auth._serializer(secret).dumps({"state": state, "nonce": nonce})
    redirect_uri = str(request.url_for("oauth_google_callback"))
    params = {
        "client_id": settings.oauth_google.client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    if settings.oauth_google.allowed_email_domain:
        params["hd"] = settings.oauth_google.allowed_email_domain
    resp = RedirectResponse(
        url=f"{_GOOGLE_AUTH}?{urlencode(params)}",
        status_code=303,
    )
    resp.set_cookie(
        "zai_oauth_pkce", cookie, max_age=600,
        httponly=True, secure=request.app.state.cookie_secure,
        samesite="lax",
    )
    return resp


@router.get("/admin/oauth/google/callback", name="oauth_google_callback")
@limiter.limit("30/minute")
async def callback(request: Request, code: str = "",
                   state: str = "") -> RedirectResponse:
    settings = request.app.state.settings
    ip = _real_ip(request)
    if settings.oauth_google is None:
        raise HTTPException(status_code=503, detail="Google SSO not configured")
    secret = request.app.state.session_secret
    raw = request.cookies.get("zai_oauth_pkce")
    if not raw or not code or not state:
        raise HTTPException(status_code=400, detail="bad oauth callback")
    try:
        stash = auth._serializer(secret).loads(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail="bad oauth state") from e
    if stash.get("state") != state:
        raise HTTPException(status_code=400, detail="state mismatch")

    redirect_uri = str(request.url_for("oauth_google_callback"))
    async with httpx.AsyncClient(timeout=15) as h:
        token_resp = await h.post(_GOOGLE_TOKEN, data={
            "code": code,
            "client_id": settings.oauth_google.client_id,
            "client_secret": settings.oauth_google.client_secret.get_secret_value(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        token_resp.raise_for_status()
        tok = token_resp.json()
        certs_resp = await h.get(_GOOGLE_CERTS)
        certs_resp.raise_for_status()
        jwks = certs_resp.json()

    id_token = tok.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="no id_token from Google")

    # Authlib JWT verification with Google's JWKS
    claims = jwt.decode(id_token, jwks)
    claims.validate()

    # #5: Validate iss claim explicitly
    iss = claims.get("iss", "")
    if iss not in _VALID_ISS:
        memory = request.app.state.memory
        await log_admin_event(
            memory, event_type="oauth_callback_failure",
            ip=ip, details={"reason": "invalid iss", "iss": iss},
        )
        raise HTTPException(status_code=400, detail="invalid token issuer")

    if claims.get("nonce") != stash.get("nonce"):
        raise HTTPException(status_code=400, detail="nonce mismatch")
    aud = claims.get("aud")
    if aud != settings.oauth_google.client_id:
        raise HTTPException(status_code=400, detail="audience mismatch")

    email = claims.get("email", "")
    if not email or not claims.get("email_verified"):
        raise HTTPException(status_code=400, detail="email not verified")
    if settings.oauth_google.allowed_email_domain:
        domain = settings.oauth_google.allowed_email_domain.lower()
        if not email.lower().endswith("@" + domain):
            raise HTTPException(
                status_code=403,
                detail=f"email must be @{domain}",
            )

    sub = str(claims["sub"])
    memory = request.app.state.memory
    user = await users.get_user_by_oauth(memory, provider="google", subject=sub)
    if user is None:
        # Auto-provision on first SSO sign-in
        user = await users.create_oauth_user(
            memory, username=email, provider="google", subject=sub,
            role=settings.oauth_google.default_role,
        )
    if user.get("disabled"):
        raise HTTPException(status_code=403, detail="user disabled")

    ttl = request.app.state.session_ttl
    cookie = await auth.create_session(
        memory, user_id=user["id"], secret=secret, ttl_seconds=ttl,
        user_agent=request.headers.get("user-agent", ""),
        ip=ip,
    )
    await users.update_last_login(memory, user["id"])
    await log_admin_event(
        memory, event_type="login_success",
        by_user=user["username"], ip=ip,
        details={"method": "google_sso"},
    )
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        "zai_session", cookie, max_age=ttl,
        httponly=True, secure=request.app.state.cookie_secure,
        samesite="lax",
    )
    resp.delete_cookie("zai_oauth_pkce")
    return resp
