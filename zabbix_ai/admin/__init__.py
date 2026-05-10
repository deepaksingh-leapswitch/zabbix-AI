from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from zabbix_ai.admin import users
from zabbix_ai.admin.routes import (
    audit_routes,
    auth_routes,
    dashboard,
    investigations,
    memory_routes,
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

    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(investigations.router)
    app.include_router(audit_routes.router)
    app.include_router(memory_routes.router)
    if settings.zabbix_ui is not None:
        app.include_router(zabbix_link.router)
