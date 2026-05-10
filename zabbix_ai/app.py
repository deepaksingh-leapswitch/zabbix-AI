from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from zabbix_ai import __version__
from zabbix_ai.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings is None:
            yield
            return

        from pathlib import Path

        from zabbix_ai.memory import Memory
        mem = Memory(settings.sqlite_path)
        await mem.connect()
        await mem.run_migrations(
            Path(__file__).resolve().parent.parent / "migrations"
        )
        app.state.memory = mem
        try:
            if settings.admin is not None:
                from zabbix_ai.admin import setup_admin
                await setup_admin(app, settings, mem)
            yield
        finally:
            await mem.close()

    app = FastAPI(title="zabbix-ai", version=__version__, lifespan=lifespan)

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
