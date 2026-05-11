from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from zabbix_ai.admin import users
from zabbix_ai.admin.crypto import derive_key
from zabbix_ai.admin.csrf import CSRFMiddleware
from zabbix_ai.admin.rate_limit import _rate_limit_handler, limiter
from zabbix_ai.admin.routes import (
    audit_routes,
    auth_routes,
    connections,
    cost,
    dashboard,
    investigations,
    memory_routes,
    oauth_google,
    status,
    zabbix_link,
)
from zabbix_ai.admin.routes import (
    users as users_routes,
)
from zabbix_ai.admin.security_headers import SecurityHeadersMiddleware
from zabbix_ai.config import Settings
from zabbix_ai.memory import Memory

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_log = logging.getLogger(__name__)

from slowapi.errors import RateLimitExceeded  # noqa: E402


async def _bootstrap_warning_task(settings: Settings, memory: Memory) -> None:
    """Log a high-priority warning every 5 minutes if BOOTSTRAP_ADMIN_PASSWORD
    is still set after at least one admin user exists (#21)."""
    env_var = (settings.admin.bootstrap_admin_password_env
               if settings.admin else "") or ""
    if not env_var:
        return
    while True:
        await asyncio.sleep(300)
        val = os.environ.get(env_var, "")
        if not val:
            return  # cleared — stop warning
        try:
            row = await memory.fetchone(
                "SELECT COUNT(*) FROM users WHERE role='admin'"
            )
            if row and row[0] > 0:
                _log.warning(
                    "SECURITY: %s is still set in env but admin users exist. "
                    "Remove it from /etc/zabbix-ai/env now.",
                    env_var,
                )
        except Exception:
            pass


def register_admin_components(app: FastAPI, settings: Settings) -> None:
    """Synchronous middleware/router/static registration.

    Must run *before* the app starts (i.e. inside ``create_app``), because
    starlette refuses ``add_middleware`` once the lifespan has begun.
    The async parts (DB connect, bootstrap user, background tasks) stay
    in :func:`setup_admin`, which runs from the lifespan handler.
    """
    if settings.admin is None:
        return

    cookie_secure = True
    app.state.cookie_secure = cookie_secure

    # ── Security middleware (#1, #3, #4) ─────────────────────────────────────
    # Order: SecurityHeaders → CSRF (outermost first in add_middleware, so
    # they execute in reverse order: CSRF first, then SecurityHeaders wraps).
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CSRFMiddleware, cookie_secure=cookie_secure)

    # Rate limiter state (#3)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)  # type: ignore[arg-type]

    # Static files for self-hosted htmx (#12)
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(investigations.router)
    app.include_router(audit_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(connections.router)
    app.include_router(users_routes.router)
    app.include_router(cost.router)
    app.include_router(status.router)
    if settings.zabbix_ui is not None:
        app.include_router(zabbix_link.router)
    if settings.oauth_google is not None:
        app.include_router(oauth_google.router)


async def setup_admin(app: FastAPI, settings: Settings,
                      memory: Memory) -> None:
    if settings.admin is None:
        return

    # #21: Warn at startup if BOOTSTRAP_ADMIN_PASSWORD is set and admin user exists
    if settings.admin.bootstrap_admin_password_env:
        bap = os.environ.get(settings.admin.bootstrap_admin_password_env, "")
        if bap:
            try:
                row = await memory.fetchone(
                    "SELECT COUNT(*) FROM users WHERE role='admin'"
                )
                if row and row[0] > 0:
                    _log.warning(
                        "SECURITY: %s is set in env but at least one admin user "
                        "already exists. Clear it from /etc/zabbix-ai/env: "
                        "sudo sed -i '/^%s=/d' /etc/zabbix-ai/env",
                        settings.admin.bootstrap_admin_password_env,
                        settings.admin.bootstrap_admin_password_env,
                    )
            except Exception:
                pass

    # bootstrap admin user if no users exist and a password was provided
    if settings.admin.bootstrap_admin_password.get_secret_value():
        await users.ensure_bootstrap_admin(
            memory, username="admin",
            password=settings.admin.bootstrap_admin_password.get_secret_value(),
        )

    app.state.memory = memory
    app.state.settings = settings
    app.state.session_secret = settings.admin.session_secret.get_secret_value()
    app.state.session_ttl = settings.admin.session_max_age_seconds
    # cookie_secure already set by register_admin_components, but keep it here
    # for backwards-compatibility with tests that call setup_admin directly.
    app.state.cookie_secure = getattr(app.state, "cookie_secure", True)
    _base_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # Wrap TemplateResponse to auto-inject csrf_token into every context.
    # This avoids having to thread it through every route explicitly.
    from zabbix_ai.admin.csrf import get_csrf_token as _get_csrf_token

    class _Templates:
        """Proxy that injects csrf_token into every TemplateResponse context."""
        def __init__(self, base: Jinja2Templates) -> None:
            self._base = base

        def TemplateResponse(self, request, name: str,  # noqa: N802
                             context: dict | None = None,
                             **kwargs):
            ctx = dict(context or {})
            ctx.setdefault("csrf_token", _get_csrf_token(request))
            return self._base.TemplateResponse(request, name, ctx, **kwargs)

    app.state.templates = _Templates(_base_templates)

    # Derive encryption key for secrets store.
    secrets_key = os.environ.get("SECRETS_KEY") or os.environ.get("SESSION_SECRET", "")
    if not os.environ.get("SECRETS_KEY") and secrets_key:
        logging.getLogger(__name__).warning(
            "SECRETS_KEY not set; falling back to SESSION_SECRET for secret encryption. "
            "Set SECRETS_KEY to allow independent session-secret rotation."
        )
    if secrets_key:
        app.state.crypto_key = derive_key(secrets_key)
    else:
        app.state.crypto_key = b"\x00" * 32

    # #21: Background task to warn about retained BOOTSTRAP_ADMIN_PASSWORD.
    # Stash the task on app.state so it isn't garbage-collected mid-loop.
    app.state.bootstrap_warning_task = asyncio.create_task(
        _bootstrap_warning_task(settings, memory)
    )
