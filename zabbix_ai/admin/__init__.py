from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from zabbix_ai.admin import users
from zabbix_ai.admin.crypto import derive_key
from zabbix_ai.admin.routes import (
    audit_routes,
    auth_routes,
    connections,
    dashboard,
    investigations,
    memory_routes,
    oauth_google,
    zabbix_link,
)
from zabbix_ai.config import Settings
from zabbix_ai.memory import Memory

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


async def setup_admin(app: FastAPI, settings: Settings,
                      memory: Memory) -> None:
    if settings.admin is None:
        return

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
    app.state.cookie_secure = True
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # Derive encryption key for secrets store.
    secrets_key = os.environ.get("SECRETS_KEY") or os.environ.get("SESSION_SECRET", "")
    if not os.environ.get("SECRETS_KEY") and secrets_key:
        import logging
        logging.getLogger(__name__).warning(
            "SECRETS_KEY not set; falling back to SESSION_SECRET for secret encryption. "
            "Set SECRETS_KEY to allow independent session-secret rotation."
        )
    if secrets_key:
        app.state.crypto_key = derive_key(secrets_key)
    else:
        # Provide a zero key so routes don't crash; secrets just won't decrypt.
        app.state.crypto_key = b"\x00" * 32

    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(investigations.router)
    app.include_router(audit_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(connections.router)
    if settings.zabbix_ui is not None:
        app.include_router(zabbix_link.router)
    if settings.oauth_google is not None:
        app.include_router(oauth_google.router)
