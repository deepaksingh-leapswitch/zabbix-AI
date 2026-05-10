from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from zabbix_ai.config import Settings
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.renderers.html import render_investigate_page
from zabbix_ai.services.investigation_runner import InvestigationRunner
from zabbix_ai.url_signing import UrlSignatureError, verify_url_token


def build_router(settings: Settings) -> APIRouter:
    if settings.zabbix_ui is None:
        raise RuntimeError("build_router called without zabbix_ui settings")
    signing_key = settings.zabbix_ui.signing_key.get_secret_value()
    router = APIRouter()

    def _verify(token: str) -> dict[str, Any]:
        try:
            return verify_url_token(token, signing_key=signing_key)
        except UrlSignatureError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

    @router.get("/investigate", response_class=HTMLResponse)
    async def page(token: str = "") -> HTMLResponse:
        payload = _verify(token)
        eventid = payload.get("eventid")
        hostid = payload.get("hostid")
        instance = payload.get("instance", "")
        # Show whichever identifier we have. Both: prefer eventid (more specific).
        if eventid is not None:
            heading_id = f"event {eventid}"
        elif hostid is not None:
            heading_id = f"host {hostid}"
        else:
            heading_id = "(no event or host)"
        sse_path = f"/investigate/stream?token={token}"
        return HTMLResponse(render_investigate_page(
            eventid=heading_id, instance=instance, sse_path=sse_path,
        ))

    @router.get("/investigate/stream")
    async def stream(token: str = "") -> EventSourceResponse:
        payload = _verify(token)
        eventid = payload.get("eventid")
        instance = payload.get("instance", "")
        hostid = payload.get("hostid")

        async def event_gen():
            async with InvestigationRunner(settings) as runner:
                ctx = InvestigationContext(
                    source="zabbix_ui",
                    instance=instance,
                    eventid=int(eventid) if eventid is not None else None,
                    hostid=int(hostid) if hostid is not None else None,
                )
                async for ev in runner.investigate_streaming(ctx):
                    yield {"event": ev["event"],
                           "data": json.dumps(ev["data"], default=str)}

        return EventSourceResponse(event_gen())

    return router
