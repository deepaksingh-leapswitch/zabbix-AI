"""Application-level rate limiting via slowapi.

Import `limiter` and apply `@limiter.limit("N/period")` decorators to
individual route handlers.  Register `app.state.limiter = limiter` and
`app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)` in
app setup (done in `setup_admin`).
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded


def _real_ip(request: Request) -> str:
    """Return the real client IP, honouring X-Forwarded-For from localhost."""
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=_real_ip, default_limits=[])


async def _rate_limit_handler(request: Request,
                               exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
        headers={"Retry-After": "60"},
    )
