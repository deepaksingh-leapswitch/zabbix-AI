from __future__ import annotations

import os

from fastapi import FastAPI

from zabbix_ai import __version__
from zabbix_ai.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="zabbix-ai", version=__version__)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__}

    if settings is not None and settings.slack is not None:
        from zabbix_ai.adapters.slack import build_router
        app.include_router(build_router(settings))

    if settings is not None and settings.zabbix_ui is not None:
        from zabbix_ai.adapters.zabbix_ui import build_router as build_ui_router
        app.include_router(build_ui_router(settings))

    return app


def _default_app() -> FastAPI:
    cfg = os.environ.get("ZABBIX_AI_CONFIG", "/etc/zabbix-ai/config.yaml")
    if not os.path.exists(cfg):
        return create_app()
    try:
        settings = load_settings(cfg)
    except Exception:
        return create_app()
    return create_app(settings=settings)


app = _default_app()
