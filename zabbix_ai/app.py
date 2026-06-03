from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from zabbix_ai import __version__
from zabbix_ai.config import Settings, load_settings

_log = logging.getLogger(__name__)


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

            # v1.5 background workers — best-effort (never break startup),
            # but log failures and record health on app.state for /admin/status.
            # Build Zabbix clients once so the pollers actually have
            # clients. Previously app.state.zabbix_clients was never set,
            # so resolution_poller / outcome_inference ran as no-ops.
            from zabbix_ai.clients.zabbix import ZabbixClient
            _zclients = {}
            for _inst in settings.zabbix_instances:
                try:
                    _zclients[_inst.name] = ZabbixClient(
                        _inst.name, str(_inst.url),
                        _inst.token.get_secret_value(), memory=mem)
                except Exception:
                    _log.exception("failed to build zabbix client %s", _inst.name)
            app.state.zabbix_clients = _zclients

            app.state.worker_health = {}

            def _start_worker(name, thunk):
                try:
                    thunk()
                    app.state.worker_health[name] = {"started": True, "error": None}
                except Exception as e:  # noqa: BLE001
                    _log.exception("background worker %s failed to start", name)
                    app.state.worker_health[name] = {"started": False, "error": str(e)}

            def _start_resolution():
                from zabbix_ai.services.resolution_notes import (
                    start_resolution_poller,
                )
                start_resolution_poller(
                    app, settings, mem, getattr(app.state, "zabbix_clients", {}))
            _start_worker("resolution_poller", _start_resolution)

            def _start_outcome():
                from zabbix_ai.services.outcome_inference import (
                    start_outcome_inference,
                )
                # Signature differs from the other workers: (memory, settings,
                # *, clients). It returns the task — stash it so it isn't GC'd.
                app.state.outcome_inference_task = start_outcome_inference(
                    mem, settings,
                    clients=getattr(app.state, "zabbix_clients", {}))
            _start_worker("outcome_inference", _start_outcome)

            def _start_hbsync():
                from zabbix_ai.services.hostbill_link import start_hostbill_sync
                start_hostbill_sync(app, settings, mem)
            _start_worker("hostbill_sync", _start_hbsync)

            if settings.ticket_flow is not None and settings.ticket_flow.enabled:
                def _start_followup():
                    from zabbix_ai.services.followup_worker import (
                        start_followup_worker,
                    )
                    start_followup_worker(app, settings, mem)
                _start_worker("followup_worker", _start_followup)
            else:
                app.state.worker_health["followup_worker"] = {
                    "started": False, "error": None,
                    "skipped": "ticket_flow disabled"}

            yield
        finally:
            await mem.close()

    app = FastAPI(title="zabbix-ai", version=__version__, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__}

    # Admin middleware/routers/static must register synchronously (starlette
    # rejects add_middleware after the app has started).
    if settings is not None and settings.admin is not None:
        from zabbix_ai.admin import register_admin_components
        register_admin_components(app, settings)

    if settings is not None and settings.slack is not None:
        from zabbix_ai.adapters.slack import build_router
        app.include_router(build_router(settings))

        # Slack interactive components (ticket-flow approve/discard buttons).
        from zabbix_ai.adapters.slack_interactions import (
            build_router as build_interactions_router,
        )
        app.include_router(build_interactions_router(settings))

    if settings is not None and settings.zabbix_ui is not None:
        from zabbix_ai.adapters.zabbix_ui import build_router as build_ui_router
        app.include_router(build_ui_router(settings))

    # Auto-investigate webhook (always mounted; returns 503 when settings.
    # auto_investigate is unset, so it's safe to leave on).
    if settings is not None:
        from zabbix_ai.adapters.zabbix_webhook import (
            build_router as build_webhook_router,
        )
        app.include_router(build_webhook_router(settings))

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
