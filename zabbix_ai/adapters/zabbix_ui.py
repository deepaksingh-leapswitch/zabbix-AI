from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from zabbix_ai.config import Settings
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.renderers.html import render_investigate_page
from zabbix_ai.services.investigation_runner import InvestigationRunner
from zabbix_ai.url_signing import UrlSignatureError, verify_url_token


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    async def stream(request: Request, token: str = "") -> EventSourceResponse:
        payload = _verify(token)
        jti = payload.get("jti")
        eventid = payload.get("eventid")
        instance = payload.get("instance", "")
        hostid = payload.get("hostid")

        # ── Single-use token enforcement (#2, #7) ────────────────────────────
        # Use the shared app.state.memory (migrations already applied in
        # lifespan). Falls back to a no-op if memory isn't configured.
        mem = getattr(request.app.state, "memory", None)
        if jti and mem is not None:
            existing = await mem.fetchone(
                "SELECT jti FROM used_tokens WHERE jti=?", (jti,)
            )
            if existing:
                raise HTTPException(status_code=401, detail="token already used")
            exp_ts = int(time.time()) + settings.zabbix_ui.link_ttl_seconds
            exp_iso = datetime.fromtimestamp(exp_ts, UTC).isoformat()
            await mem.execute(
                "INSERT INTO used_tokens (jti, used_at, expires_at) VALUES (?,?,?)",
                (jti, _now_iso(), exp_iso),
            )
            # Prune expired tokens opportunistically
            await mem.execute(
                "DELETE FROM used_tokens WHERE expires_at < ?",
                (_now_iso(),),
            )

        async def event_gen():
            async with InvestigationRunner(settings) as runner:
                ctx = InvestigationContext(
                    source="zabbix_ui",
                    instance=instance,
                    eventid=int(eventid) if eventid is not None else None,
                    hostid=int(hostid) if hostid is not None else None,
                )
                final_summary = ""
                async for ev in runner.investigate_streaming(ctx):
                    if ev.get("event") == "final":
                        data = ev.get("data") or {}
                        final_summary = (data.get("summary")
                                          or data.get("text")
                                          or "")
                    yield {"event": ev["event"],
                           "data": json.dumps(ev["data"], default=str)}

                # After the SSE stream completes, post the summary back
                # to the Zabbix event as a comment so the right-click
                # flow also leaves a paper trail in the Zabbix UI.
                if eventid is not None and final_summary:
                    zclients = getattr(runner, "_zabbix_clients", {}) or {}
                    zc = zclients.get(instance)
                    if zc is not None:
                        from zabbix_ai.services.zabbix_writeback import (
                            post_summary_to_event,
                        )
                        await post_summary_to_event(
                            zc, eventid=eventid,
                            summary=final_summary, source="manual",
                        )

        return EventSourceResponse(event_gen())

    return router
