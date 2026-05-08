from __future__ import annotations

from fastapi import FastAPI

from zabbix_ai import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="zabbix-ai", version=__version__)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__}

    return app

app = create_app()
