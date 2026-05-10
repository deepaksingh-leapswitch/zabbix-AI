"""CSRF protection — double-submit cookie pattern.

On every authenticated GET the middleware sets a `zai_csrf` cookie
(random 32-byte token) if one is absent.  Every state-changing POST to
/admin/* must present the same token in either:
  - the `X-CSRF-Token` request header, or
  - a hidden form field `csrf_token`.

The comparison uses `hmac.compare_digest` (constant-time).

Exempt paths (no session cookie, externally-driven POSTs):
  /slack/events
  /admin/oauth/google/callback  (OAuth redirect, GET)
  Any path outside /admin/*

"""
from __future__ import annotations

import hmac
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_COOKIE_NAME = "zai_csrf"
_TOKEN_BYTES = 32

# Paths whose POST is exempt from CSRF checking (external callers,
# no session cookie, HMAC-verified by other means).
_CSRF_EXEMPT = {"/slack/events"}


def _generate_token() -> str:
    return secrets.token_hex(_TOKEN_BYTES)


def get_csrf_token(request: Request) -> str:
    """Return the CSRF token for this request.

    Available in Jinja2 templates as ``{{ csrf_token }}``.
    The value is cached on the request state after the middleware sets it.
    """
    return getattr(request.state, "csrf_token", "")


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF middleware for /admin/* routes."""

    def __init__(self, app, *, cookie_secure: bool = True) -> None:
        super().__init__(app)
        self._secure = cookie_secure

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        path = request.url.path

        # ── Read or create the CSRF cookie ──────────────────────────────────
        token = request.cookies.get(_COOKIE_NAME) or _generate_token()
        request.state.csrf_token = token

        # ── Enforce on state-changing /admin/* POSTs ─────────────────────────
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and path.startswith("/admin/")
            and path not in _CSRF_EXEMPT
        ):
            # Read token from header or form body
            submitted = request.headers.get("X-CSRF-Token")
            if not submitted:
                # Parse form body (content-type application/x-www-form-urlencoded)
                try:
                    form = await request.form()
                    submitted = form.get("csrf_token", "")
                except Exception:
                    submitted = ""

            if not submitted or not hmac.compare_digest(submitted, token):
                return Response(
                    content="CSRF token missing or invalid",
                    status_code=403,
                    media_type="text/plain",
                )

        response = await call_next(request)

        # ── Set / refresh the CSRF cookie on every response ─────────────────
        if path.startswith("/admin/") or path == "/":
            response.set_cookie(
                _COOKIE_NAME,
                token,
                httponly=False,   # JS (HTMX) must be able to read it
                secure=self._secure,
                samesite="strict",
                max_age=86400,
                path="/",
            )

        return response
