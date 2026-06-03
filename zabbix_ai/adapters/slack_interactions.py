"""Slack interactive-components endpoint (ticket-flow, migration 008).

Slack POSTs button clicks to ``/slack/interactions`` as
``application/x-www-form-urlencoded`` with a single ``payload`` field holding
JSON. We verify the Slack signature over the RAW body (reusing the events
adapter's verifier), then dispatch ``block_actions``.

The real work (HostBill ticket create) runs in a background task so we ACK
within Slack's 3-second window; the original draft message is then updated via
the interaction's ``response_url``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from zabbix_ai.adapters.slack import SlackSignatureError, verify_slack_signature
from zabbix_ai.config import Settings
from zabbix_ai.services import ticket_flow

_log = logging.getLogger(__name__)


def build_router(settings: Settings) -> APIRouter:
    if settings.slack is None:
        raise RuntimeError("slack_interactions build_router without Slack settings")
    signing_secret = settings.slack.signing_secret.get_secret_value()
    cfg = settings.ticket_flow
    approvers = set(cfg.approver_slack_user_ids) if cfg else set()

    router = APIRouter()

    @router.post("/slack/interactions")
    async def interactions(request: Request) -> Any:
        body = await request.body()
        ts = request.headers.get("X-Slack-Request-Timestamp", "")
        sig = request.headers.get("X-Slack-Signature", "")
        try:
            verify_slack_signature(body, ts, sig, signing_secret)
        except SlackSignatureError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

        form = parse_qs(body.decode())
        raw = (form.get("payload") or [None])[0]
        if not raw:
            raise HTTPException(status_code=400, detail="missing payload")
        payload = json.loads(raw)
        if payload.get("type") != "block_actions":
            return PlainTextResponse("")  # ignore other interaction types

        actions = payload.get("actions") or []
        if not actions:
            return PlainTextResponse("")
        action_id = actions[0].get("action_id", "")
        value = actions[0].get("value", "")
        user_id = (payload.get("user") or {}).get("id", "")
        response_url = payload.get("response_url", "")

        # Approver allowlist (empty ⇒ anyone in the channel may approve).
        if approvers and user_id not in approvers:
            return JSONResponse({
                "response_type": "ephemeral", "replace_original": False,
                "text": ":no_entry: You're not on the approver allowlist."})

        try:
            incident_id = int(value)
        except (TypeError, ValueError):
            return PlainTextResponse("")

        # Do the work async so we ACK fast; the message updates via response_url.
        asyncio.create_task(_handle(
            settings=settings, app=request.app, action_id=action_id,
            incident_id=incident_id, user_id=user_id, response_url=response_url))
        return JSONResponse({"response_type": "ephemeral",
                             "text": ":hourglass_flowing_sand: Working on it…"})

    return router


async def _handle(*, settings: Settings, app, action_id: str,
                  incident_id: int, user_id: str, response_url: str) -> None:
    memory = getattr(app.state, "memory", None)
    if memory is None:
        return
    try:
        if action_id == "ticket_approve":
            res = await ticket_flow.approve_and_create_ticket(
                settings=settings, memory=memory,
                incident_id=incident_id, approver=user_id)
            if not res.get("ok"):
                msg = f":warning: Could not raise ticket: {res.get('msg')}"
            elif res.get("dry_run"):
                msg = (f":white_check_mark: *Approved* by <@{user_id}> — "
                       "dry-run, no ticket created. Follow-up loop armed.")
            elif res.get("ticket_id"):
                msg = (f":white_check_mark: *Ticket #{res['ticket_id']} raised* "
                       f"({res.get('ticket_kind')}) — approved by <@{user_id}>. "
                       "Following up automatically until a reply or recovery.")
            else:
                msg = (f":white_check_mark: Approved by <@{user_id}> "
                       "(HostBill not configured — no ticket id).")
        elif action_id == "ticket_discard":
            await ticket_flow.discard_incident(memory, incident_id, by=user_id)
            msg = f":wastebasket: Draft discarded by <@{user_id}>."
        elif action_id == "disable_approve":
            from zabbix_ai.services import zabbix_write as zw
            res = await zw.perform_disable(
                settings=settings, memory=memory,
                incident_id=incident_id, approver=user_id)
            msg = (res["msg"] if res.get("ok")
                   else f":warning: Could not disable: {res.get('msg')}")
        elif action_id == "disable_dismiss":
            from zabbix_ai.services import zabbix_write as zw
            await zw.dismiss_disable(memory, incident_id, by=user_id)
            msg = f":no_entry_sign: Monitoring-disable dismissed by <@{user_id}>."
        else:
            return  # unknown action — leave the message untouched
        if response_url:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(response_url,
                             json={"replace_original": True, "text": msg})
    except Exception as e:  # noqa: BLE001 — report failure back to Slack
        _log.exception("slack interaction handler failed")
        if response_url:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(response_url, json={
                    "replace_original": False,
                    "text": f":x: Action failed: {e}"})
