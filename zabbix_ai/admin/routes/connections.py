from __future__ import annotations

import secrets

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from zabbix_ai.admin import connections_store as cs
from zabbix_ai.admin.auth import login_required

router = APIRouter()


def _crypto_key(request: Request) -> bytes:
    return request.app.state.crypto_key


def _memory(request: Request):
    return request.app.state.memory


def _tmpl(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, name, ctx)


# ─── overview ───────────────────────────────────────────────────────────────

@router.get("/admin/connections", response_class=HTMLResponse)
async def connections_overview(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)

    zabbix_rows = await cs.conn_list(memory, type_filter="zabbix")
    hb = await cs.conn_get(memory, type_="hostbill", name="primary")
    slack = await cs.conn_get(memory, type_="slack", name="primary")
    anthropic_key = await cs.secret_get(memory, key="anthropic:primary:api_key",
                                         crypto_key=crypto_key)
    og = await cs.conn_get(memory, type_="oauth_google", name="primary")
    zui_key = await cs.secret_get(memory, key="zabbix_ui:primary:signing_key",
                                   crypto_key=crypto_key)

    settings = request.app.state.settings

    cards = [
        {
            "type": "zabbix",
            "label": "Zabbix instances",
            "href": "/admin/connections/zabbix",
            "configured": bool(zabbix_rows),
            "detail": f"{len(zabbix_rows)} instance(s)" if zabbix_rows else "none",
        },
        {
            "type": "hostbill",
            "label": "HostBill",
            "href": "/admin/connections/hostbill",
            "configured": bool(hb),
            "detail": hb["config"].get("api_url", "") if hb else (
                str(settings.hostbill.api_url) if settings.hostbill else "not set"
            ),
        },
        {
            "type": "slack",
            "label": "Slack",
            "href": "/admin/connections/slack",
            "configured": bool(slack),
            "detail": "configured" if slack else (
                "from file" if settings.slack else "not set"
            ),
        },
        {
            "type": "anthropic",
            "label": "Anthropic API key",
            "href": "/admin/connections/anthropic",
            "configured": bool(anthropic_key),
            "detail": "stored in DB" if anthropic_key else (
                "from env" if settings.anthropic_api_key.get_secret_value() else "not set"
            ),
        },
        {
            "type": "oauth_google",
            "label": "Google SSO",
            "href": "/admin/connections/oauth-google",
            "configured": bool(og),
            "detail": og["config"].get("client_id", "") if og else (
                settings.oauth_google.client_id if settings.oauth_google else "not set"
            ),
        },
        {
            "type": "zabbix_ui",
            "label": "Zabbix UI signing key",
            "href": "/admin/connections/zabbix-ui",
            "configured": bool(zui_key),
            "detail": "stored in DB" if zui_key else (
                "from env" if (settings.zabbix_ui and
                               settings.zabbix_ui.signing_key.get_secret_value())
                else "not set"
            ),
        },
        {
            "type": "system",
            "label": "Models & limits",
            "href": "/admin/connections/system",
            "configured": True,
            "detail": f"reasoning: {settings.default_model} · "
                      f"summary: {settings.summary_model} · "
                      f"max tool calls: {settings.max_tool_calls}",
        },
    ]

    return _tmpl(request, "admin/connections.html", {
        "user": user, "flashes": [], "active": "connections", "cards": cards,
    })


# ─── Zabbix instances ────────────────────────────────────────────────────────

@router.get("/admin/connections/zabbix", response_class=HTMLResponse)
async def zabbix_list(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    rows = await cs.conn_list(memory, type_filter="zabbix")
    return _tmpl(request, "admin/connections/zabbix_list.html", {
        "user": user, "flashes": [], "active": "connections", "rows": rows,
    })


@router.get("/admin/connections/zabbix/new", response_class=HTMLResponse)
async def zabbix_new(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    return _tmpl(request, "admin/connections/zabbix_form.html", {
        "user": user, "flashes": [], "active": "connections",
        "conn": None, "editing": False,
    })


@router.get("/admin/connections/zabbix/{name}/edit", response_class=HTMLResponse)
async def zabbix_edit(
    request: Request,
    name: str,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    conn = await cs.conn_get(memory, type_="zabbix", name=name)
    return _tmpl(request, "admin/connections/zabbix_form.html", {
        "user": user, "flashes": [], "active": "connections",
        "conn": conn, "editing": True,
    })


@router.post("/admin/connections/zabbix/save")
async def zabbix_save(
    request: Request,
    user: dict = Depends(login_required("admin")),
    name: str = Form(...),
    original_name: str = Form(""),
    url: str = Form(...),
    token: str = Form(""),
    enabled: str = Form("on"),
) -> RedirectResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    name = name.strip()
    original_name = original_name.strip()

    # Validate name (lowercase, alphanumeric + dash/underscore — fits in URLs
    # and CLI args without quoting, and avoids the "instance=LS Zabbix" gotcha).
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        # bounce back with the form re-rendered + a flash error
        return _tmpl(request, "admin/connections/zabbix_form.html", {
            "user": user, "active": "connections",
            "flashes": [{"kind": "err",
                         "text": "Instance name must contain only "
                                 "letters, digits, '-' or '_' (no spaces)."}],
            "conn": {"name": original_name or name,
                      "config": {"url": url},
                      "enabled": (enabled == "on")},
            "editing": bool(original_name),
        })

    if original_name and original_name != name:
        # Rename: re-key the secret + drop the old connection row
        old_token = await cs.secret_get(
            memory, key=f"zabbix:{original_name}:token",
            crypto_key=crypto_key,
        )
        await cs.conn_delete(memory, type_="zabbix", name=original_name)
        await cs.secret_delete(memory, key=f"zabbix:{original_name}:token")
        if old_token and not token:
            # Carry the existing token over to the new name unchanged.
            await cs.secret_set(
                memory, key=f"zabbix:{name}:token",
                value=old_token, crypto_key=crypto_key,
                updated_by=user["username"],
            )

    await cs.conn_upsert(
        memory, type_="zabbix", name=name,
        config={"url": url},
        enabled=(enabled == "on"),
        updated_by=user["username"],
    )
    if token:
        await cs.secret_set(
            memory, key=f"zabbix:{name}:token",
            value=token, crypto_key=crypto_key,
            updated_by=user["username"],
        )
    return RedirectResponse("/admin/connections/zabbix", status_code=303)


@router.post("/admin/connections/zabbix/{name}/delete")
async def zabbix_delete(
    request: Request,
    name: str,
    user: dict = Depends(login_required("admin")),
) -> RedirectResponse:
    memory = _memory(request)
    await cs.conn_delete(memory, type_="zabbix", name=name)
    await cs.secret_delete(memory, key=f"zabbix:{name}:token")
    return RedirectResponse("/admin/connections/zabbix", status_code=303)


@router.post("/admin/connections/zabbix/{name}/test")
async def zabbix_test(
    request: Request,
    name: str,
    user: dict = Depends(login_required("admin")),
) -> JSONResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    conn = await cs.conn_get(memory, type_="zabbix", name=name)
    if not conn:
        return JSONResponse({"ok": False, "message": "Connection not found", "sample": None})
    tok = await cs.secret_get(memory, key=f"zabbix:{name}:token", crypto_key=crypto_key)
    if not tok:
        return JSONResponse({"ok": False, "message": "No token stored", "sample": None})
    url = conn["config"]["url"].rstrip("/") + "/api_jsonrpc.php"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={
                "jsonrpc": "2.0", "method": "host.get",
                "params": {"output": ["host"], "limit": 1}, "id": 1,
            }, headers={"Authorization": f"Bearer {tok}",
                        "Content-Type": "application/json-rpc"})
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                return JSONResponse({"ok": False,
                                     "message": str(data["error"]), "sample": None})
            return JSONResponse({"ok": True, "message": "Connected",
                                 "sample": data.get("result", [])[:1]})
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc), "sample": None})


# ─── HostBill ────────────────────────────────────────────────────────────────

@router.get("/admin/connections/hostbill", response_class=HTMLResponse)
async def hostbill_form(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    conn = await cs.conn_get(memory, type_="hostbill", name="primary")
    return _tmpl(request, "admin/connections/hostbill_form.html", {
        "user": user, "flashes": [], "active": "connections", "conn": conn,
    })


@router.post("/admin/connections/hostbill/save")
async def hostbill_save(
    request: Request,
    user: dict = Depends(login_required("admin")),
    api_url: str = Form(...),
    api_id: str = Form(""),
    api_key: str = Form(""),
    enabled: str = Form("on"),
) -> RedirectResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    await cs.conn_upsert(
        memory, type_="hostbill", name="primary",
        config={"api_url": api_url},
        enabled=(enabled == "on"),
        updated_by=user["username"],
    )
    if api_id:
        await cs.secret_set(memory, key="hostbill:primary:api_id", value=api_id,
                             crypto_key=crypto_key, updated_by=user["username"])
    if api_key:
        await cs.secret_set(memory, key="hostbill:primary:api_key", value=api_key,
                             crypto_key=crypto_key, updated_by=user["username"])
    return RedirectResponse("/admin/connections/hostbill", status_code=303)


@router.post("/admin/connections/hostbill/test")
async def hostbill_test(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> JSONResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    conn = await cs.conn_get(memory, type_="hostbill", name="primary")
    if not conn:
        return JSONResponse({"ok": False, "message": "Not configured", "sample": None})
    api_id = await cs.secret_get(memory, key="hostbill:primary:api_id",
                                  crypto_key=crypto_key)
    api_key = await cs.secret_get(memory, key="hostbill:primary:api_key",
                                   crypto_key=crypto_key)
    if not api_id or not api_key:
        return JSONResponse({"ok": False, "message": "API credentials missing",
                             "sample": None})
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(conn["config"]["api_url"], data={
                "api_id": api_id, "api_key": api_key,
                "call": "getTickets", "limit": "1",
            })
            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                return JSONResponse({"ok": False,
                                     "message": data.get("error", "API error"),
                                     "sample": None})
            return JSONResponse({"ok": True, "message": "Connected", "sample": None})
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc), "sample": None})


# ─── Slack ───────────────────────────────────────────────────────────────────

@router.get("/admin/connections/slack", response_class=HTMLResponse)
async def slack_form(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    conn = await cs.conn_get(memory, type_="slack", name="primary")
    return _tmpl(request, "admin/connections/slack_form.html", {
        "user": user, "flashes": [], "active": "connections", "conn": conn,
    })


@router.post("/admin/connections/slack/save")
async def slack_save(
    request: Request,
    user: dict = Depends(login_required("admin")),
    default_instance: str = Form(""),
    channel_allowlist: str = Form(""),
    bot_token: str = Form(""),
    signing_secret: str = Form(""),
    enabled: str = Form("on"),
) -> RedirectResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    channels = [c.strip() for c in channel_allowlist.split(",") if c.strip()]
    await cs.conn_upsert(
        memory, type_="slack", name="primary",
        config={"default_instance": default_instance, "channel_allowlist": channels},
        enabled=(enabled == "on"),
        updated_by=user["username"],
    )
    if bot_token:
        await cs.secret_set(memory, key="slack:primary:bot_token", value=bot_token,
                             crypto_key=crypto_key, updated_by=user["username"])
    if signing_secret:
        await cs.secret_set(memory, key="slack:primary:signing_secret",
                             value=signing_secret, crypto_key=crypto_key,
                             updated_by=user["username"])
    return RedirectResponse("/admin/connections/slack", status_code=303)


@router.post("/admin/connections/slack/test")
async def slack_test(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> JSONResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    tok = await cs.secret_get(memory, key="slack:primary:bot_token",
                               crypto_key=crypto_key)
    if not tok:
        return JSONResponse({"ok": False, "message": "Bot token not stored",
                             "sample": None})
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post("https://slack.com/api/auth.test",
                                  headers={"Authorization": f"Bearer {tok}"},
                                  json={})
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                return JSONResponse({"ok": False,
                                     "message": data.get("error", "Slack error"),
                                     "sample": None})
            return JSONResponse({"ok": True, "message": "Connected",
                                 "sample": {"team": data.get("team"),
                                            "user": data.get("user")}})
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc), "sample": None})


# ─── Anthropic ───────────────────────────────────────────────────────────────

@router.get("/admin/connections/anthropic", response_class=HTMLResponse)
async def anthropic_form(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    has_key = bool(await cs.secret_get(memory, key="anthropic:primary:api_key",
                                        crypto_key=crypto_key))
    return _tmpl(request, "admin/connections/anthropic_form.html", {
        "user": user, "flashes": [], "active": "connections", "has_key": has_key,
    })


@router.post("/admin/connections/anthropic/save")
async def anthropic_save(
    request: Request,
    user: dict = Depends(login_required("admin")),
    api_key: str = Form(""),
) -> RedirectResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    if api_key:
        await cs.secret_set(memory, key="anthropic:primary:api_key", value=api_key,
                             crypto_key=crypto_key, updated_by=user["username"])
    return RedirectResponse("/admin/connections/anthropic", status_code=303)


@router.post("/admin/connections/anthropic/test")
async def anthropic_test(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> JSONResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    api_key = await cs.secret_get(memory, key="anthropic:primary:api_key",
                                   crypto_key=crypto_key)
    if not api_key:
        return JSONResponse({"ok": False, "message": "API key not stored", "sample": None})
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            r.raise_for_status()
            data = r.json()
            return JSONResponse({"ok": True, "message": "Connected",
                                 "sample": {"model": data.get("model")}})
    except httpx.HTTPStatusError as exc:
        msg = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        return JSONResponse({"ok": False, "message": msg, "sample": None})
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc), "sample": None})


# ─── OAuth Google ─────────────────────────────────────────────────────────────

@router.get("/admin/connections/oauth-google", response_class=HTMLResponse)
async def oauth_google_form(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    conn = await cs.conn_get(memory, type_="oauth_google", name="primary")
    return _tmpl(request, "admin/connections/oauth_google_form.html", {
        "user": user, "flashes": [], "active": "connections", "conn": conn,
    })


@router.post("/admin/connections/oauth-google/save")
async def oauth_google_save(
    request: Request,
    user: dict = Depends(login_required("admin")),
    client_id: str = Form(...),
    client_secret: str = Form(""),
    allowed_email_domain: str = Form(""),
    default_role: str = Form("viewer"),
    enabled: str = Form("on"),
) -> RedirectResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    await cs.conn_upsert(
        memory, type_="oauth_google", name="primary",
        config={
            "client_id": client_id,
            "allowed_email_domain": allowed_email_domain,
            "default_role": default_role,
        },
        enabled=(enabled == "on"),
        updated_by=user["username"],
    )
    if client_secret:
        await cs.secret_set(memory, key="oauth_google:primary:client_secret",
                             value=client_secret, crypto_key=crypto_key,
                             updated_by=user["username"])
    return RedirectResponse("/admin/connections/oauth-google", status_code=303)


# ─── Zabbix UI signing key ────────────────────────────────────────────────────

@router.get("/admin/connections/zabbix-ui", response_class=HTMLResponse)
async def zabbix_ui_form(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    conn = await cs.conn_get(memory, type_="zabbix_ui", name="primary")
    has_key = bool(await cs.secret_get(memory, key="zabbix_ui:primary:signing_key",
                                        crypto_key=crypto_key))
    return _tmpl(request, "admin/connections/zabbix_ui_form.html", {
        "user": user, "flashes": [], "active": "connections",
        "conn": conn, "has_key": has_key,
    })


@router.post("/admin/connections/zabbix-ui/save")
async def zabbix_ui_save(
    request: Request,
    user: dict = Depends(login_required("admin")),
    signing_key: str = Form(""),
    link_ttl_seconds: int = Form(300),
) -> RedirectResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    await cs.conn_upsert(
        memory, type_="zabbix_ui", name="primary",
        config={"link_ttl_seconds": link_ttl_seconds},
        updated_by=user["username"],
    )
    if signing_key:
        await cs.secret_set(memory, key="zabbix_ui:primary:signing_key",
                             value=signing_key, crypto_key=crypto_key,
                             updated_by=user["username"])
    return RedirectResponse("/admin/connections/zabbix-ui", status_code=303)


@router.post("/admin/connections/zabbix-ui/regenerate-key")
async def zabbix_ui_regenerate(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> RedirectResponse:
    memory = _memory(request)
    crypto_key = _crypto_key(request)
    new_key = secrets.token_hex(32)
    await cs.conn_upsert(
        memory, type_="zabbix_ui", name="primary",
        config={"link_ttl_seconds": 300},
        updated_by=user["username"],
    )
    await cs.secret_set(memory, key="zabbix_ui:primary:signing_key",
                         value=new_key, crypto_key=crypto_key,
                         updated_by=user["username"])
    return RedirectResponse("/admin/connections/zabbix-ui", status_code=303)


# ─── Models & limits (singleton, type=system, name=defaults) ───────────────

@router.get("/admin/connections/system", response_class=HTMLResponse)
async def system_form(
    request: Request,
    user: dict = Depends(login_required("admin")),
) -> HTMLResponse:
    memory = _memory(request)
    conn = await cs.conn_get(memory, type_="system", name="defaults")
    settings = request.app.state.settings
    # Pre-fill from DB if set, else from currently-loaded settings
    cfg = (conn or {}).get("config", {}) if conn else {}
    return _tmpl(request, "admin/connections/system_form.html", {
        "user": user, "flashes": [], "active": "connections",
        "conn": conn,
        "current": {
            "default_model": cfg.get("default_model")
                or settings.default_model,
            "summary_model": cfg.get("summary_model")
                or settings.summary_model,
            "max_tool_calls": cfg.get("max_tool_calls")
                or settings.max_tool_calls,
            "max_input_tokens": cfg.get("max_input_tokens")
                or settings.max_input_tokens,
            "max_output_tokens": cfg.get("max_output_tokens")
                or settings.max_output_tokens,
            # Host briefing — fall back to Settings defaults if not in DB
            "host_briefing_enabled": cfg.get(
                "host_briefing_enabled",
                settings.host_briefing.enabled,
            ),
            "host_briefing_days": cfg.get(
                "host_briefing_days",
                settings.host_briefing.days,
            ),
            "host_briefing_max_tokens": cfg.get(
                "host_briefing_max_tokens",
                settings.host_briefing.max_tokens,
            ),
        },
    })


@router.post("/admin/connections/system/save")
async def system_save(
    request: Request,
    user: dict = Depends(login_required("admin")),
    default_model: str = Form(...),
    summary_model: str = Form(...),
    max_tool_calls: int = Form(...),
    max_input_tokens: int = Form(...),
    max_output_tokens: int = Form(...),
    host_briefing_enabled: str = Form(""),
    host_briefing_days: int = Form(30),
    host_briefing_max_tokens: int = Form(2000),
) -> RedirectResponse:
    memory = _memory(request)
    config = {
        "default_model": default_model.strip(),
        "summary_model": summary_model.strip(),
        "max_tool_calls": max(1, min(50, max_tool_calls)),
        "max_input_tokens": max(1000, min(200_000, max_input_tokens)),
        "max_output_tokens": max(256, min(64_000, max_output_tokens)),
        # Host briefing — checkbox sends "on" when checked, empty string when not
        "host_briefing_enabled": host_briefing_enabled == "on",
        "host_briefing_days": max(1, min(365, host_briefing_days)),
        "host_briefing_max_tokens": max(500, min(10_000, host_briefing_max_tokens)),
    }
    await cs.conn_upsert(
        memory, type_="system", name="defaults",
        config=config, updated_by=user["username"],
    )
    return RedirectResponse("/admin/connections/system", status_code=303)
