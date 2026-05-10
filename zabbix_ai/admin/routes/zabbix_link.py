"""Admin route that signs a Zabbix-UI investigation URL on demand.

Wired to a Zabbix Frontend Script of type URL with the URL pattern:
  https://zabbix-ai.lsnw.io/admin/zabbix-link?eventid={EVENT.ID}&instance=monitoring

When an authenticated NOC user right-clicks a problem and picks the
script, Zabbix opens this URL in a new tab. We verify their admin
session, build the HMAC-signed token, and 302 to /investigate?token=…
which streams the AI investigation.

Why bounce through /admin instead of registering a direct /investigate
URL? The signed token requires the URL_SIGNING_KEY which lives only on
the AI service. We can't ask the Zabbix server to compute it without a
signing-side wrapper. The bounce keeps the signing key entirely
server-side.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from zabbix_ai.admin.auth import login_required
from zabbix_ai.url_signing import sign_url_token

router = APIRouter()
_VIEWER_DEP = Depends(login_required("viewer"))


@router.get("/admin/zabbix-link")
async def zabbix_link(
    request: Request,
    eventid: int,
    instance: str,
    user: dict = _VIEWER_DEP,
) -> RedirectResponse:
    settings = request.app.state.settings
    if settings.zabbix_ui is None:
        raise HTTPException(
            status_code=503,
            detail="zabbix_ui adapter not configured on this server",
        )
    known = {i.name for i in settings.zabbix_instances}
    if instance not in known:
        raise HTTPException(
            status_code=400,
            detail=f"unknown Zabbix instance '{instance}'",
        )
    signing_key = settings.zabbix_ui.signing_key.get_secret_value()
    ttl = settings.zabbix_ui.link_ttl_seconds
    token = sign_url_token(
        {"eventid": eventid, "instance": instance,
         "issued_by": user["username"]},
        ttl_seconds=ttl,
        signing_key=signing_key,
    )
    return RedirectResponse(
        url=f"/investigate?token={token}",
        status_code=303,
    )
