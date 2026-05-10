"""Admin route that signs a Zabbix-UI investigation URL on demand.

Wired to a Zabbix Frontend Script of type URL with the URL pattern:
  https://zabbix-ai.lsnw.io/admin/zabbix-link?eventid={EVENT.ID}&hostid={HOST.ID}&instance=monitoring

When invoked from a problem context (scope=4) Zabbix substitutes both
macros. From a host context (scope=2) only `{HOST.ID}` resolves and
`{EVENT.ID}` is left as the literal string `{EVENT.ID}` (or empty,
depending on Zabbix version). The endpoint must handle either case.

We verify the user's admin session, build an HMAC-signed token, and
303 to /investigate?token=… which streams the AI investigation.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from zabbix_ai.admin.auth import login_required
from zabbix_ai.url_signing import sign_url_token

router = APIRouter()
_VIEWER_DEP = Depends(login_required("viewer"))

_INT_RE = re.compile(r"^\d+$")


def _maybe_int(s: str | None) -> int | None:
    """Parse a query param as int, returning None if absent or non-numeric.

    Zabbix macros like `{EVENT.ID}` reach us as literal strings when
    invoked from a context where the macro isn't in scope; we treat
    those as missing rather than 400ing.
    """
    if not s or not _INT_RE.match(s):
        return None
    return int(s)


@router.get("/admin/zabbix-link")
async def zabbix_link(
    request: Request,
    instance: str,
    eventid: str = "",
    hostid: str = "",
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
            detail=f"unknown Zabbix instance '{instance}' "
                   f"(known: {sorted(known)})",
        )

    eid = _maybe_int(eventid)
    hid = _maybe_int(hostid)
    if eid is None and hid is None:
        raise HTTPException(
            status_code=400,
            detail="need either ?eventid=<n> or ?hostid=<n>; both were "
                   "missing or unsubstituted (Zabbix macro didn't resolve)",
        )

    payload: dict = {"instance": instance, "issued_by": user["username"]}
    if eid is not None:
        payload["eventid"] = eid
    if hid is not None:
        payload["hostid"] = hid

    signing_key = settings.zabbix_ui.signing_key.get_secret_value()
    ttl = settings.zabbix_ui.link_ttl_seconds
    token = sign_url_token(payload, ttl_seconds=ttl, signing_key=signing_key)
    return RedirectResponse(
        url=f"/investigate?token={token}",
        status_code=303,
    )
