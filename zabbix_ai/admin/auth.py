from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Cookie, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer

from zabbix_ai.memory import Memory

_COOKIE_NAME = "zai_session"


def _now() -> datetime:
    return datetime.now(UTC)


def _serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt="zai.session")


async def create_session(memory: Memory, *, user_id: int, secret: str,
                          ttl_seconds: int, user_agent: str = "",
                          ip: str = "") -> str:
    sid = secrets.token_urlsafe(32)
    now = _now()
    exp = now + timedelta(seconds=ttl_seconds)
    await memory.execute(
        """INSERT INTO sessions
           (sid, user_id, created_at, expires_at, last_seen_at,
            user_agent, ip)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, user_id, now.isoformat(), exp.isoformat(), now.isoformat(),
         user_agent[:255], ip[:64]),
    )
    return _serializer(secret).dumps({"sid": sid})


async def resolve_session(memory: Memory, *, signed_cookie: str,
                           secret: str) -> dict[str, Any] | None:
    try:
        payload = _serializer(secret).loads(signed_cookie)
    except BadSignature:
        return None
    sid = payload.get("sid")
    if not sid:
        return None
    row = await memory.fetchone(
        """SELECT s.user_id, s.expires_at, u.username, u.role, u.disabled
           FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.sid=?""",
        (sid,),
    )
    if not row:
        return None
    user_id, expires_at, username, role, disabled = row
    if disabled:
        return None
    if datetime.fromisoformat(expires_at) <= _now():
        return None
    await memory.execute(
        "UPDATE sessions SET last_seen_at=? WHERE sid=?",
        (_now().isoformat(), sid),
    )
    return {"sid": sid, "user_id": user_id, "username": username,
            "role": role}


async def destroy_session(memory: Memory, sid: str) -> None:
    await memory.execute("DELETE FROM sessions WHERE sid=?", (sid,))


def login_required(min_role: str = "viewer"):
    """FastAPI dependency. Verifies cookie, loads user, enforces role."""
    role_rank = {"viewer": 0, "operator": 1, "admin": 2}

    async def _dep(request: Request,
                   zai_session: str | None = Cookie(default=None)) -> dict:
        if not zai_session:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/admin/login"},
            )
        memory: Memory = request.app.state.memory
        secret: str = request.app.state.session_secret
        user = await resolve_session(memory,
                                      signed_cookie=zai_session,
                                      secret=secret)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/admin/login"},
            )
        if role_rank.get(user["role"], -1) < role_rank.get(min_role, 99):
            raise HTTPException(status_code=403, detail="insufficient role")
        return user

    return _dep
