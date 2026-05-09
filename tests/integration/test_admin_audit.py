from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from zabbix_ai.admin import users
from zabbix_ai.memory import Memory

SECRET = "test-session-secret-32-bytes-ok!"
TTL = 3600


def _make_app(mem: Memory) -> object:
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from zabbix_ai.admin.routes import audit_routes, auth_routes

    app = FastAPI()
    templates_dir = Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.memory = mem
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False

    app.include_router(auth_routes.router)
    app.include_router(audit_routes.router)
    return app


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


async def _login(client, mem, username="admin", password="adminpw"):
    u = await users.create_user(mem, username=username, password=password, role="admin")
    await users.set_totp_enrolled(mem, u["id"])
    code = pyotp.TOTP(u["totp_secret"]).now()
    client.post("/admin/login", data={
        "username": username, "password": password, "totp_code": code,
    }, follow_redirects=False)


async def test_audit_list_empty(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/audit")
        assert r.status_code == 200
        assert "No audit log entries found" in r.text


async def test_audit_list_shows_entries(mem):
    now = datetime.now(UTC).isoformat()
    await mem.execute(
        """INSERT INTO audit_log (ts, investigation_id, event_type, tool_name, source)
           VALUES (?, ?, ?, ?, ?)""",
        (now, 1, "tool_call", "get_host_info", "cli"),
    )
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/audit")
        assert r.status_code == 200
        assert "tool_call" in r.text
        assert "get_host_info" in r.text


async def test_audit_filter_by_event_type(mem):
    now = datetime.now(UTC).isoformat()
    await mem.execute(
        "INSERT INTO audit_log (ts, event_type, source) VALUES (?, ?, ?)",
        (now, "tool_call", "cli"),
    )
    await mem.execute(
        "INSERT INTO audit_log (ts, event_type, source) VALUES (?, ?, ?)",
        (now, "investigation_start", "slack"),
    )
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/audit?event_type=tool_call")
        assert r.status_code == 200
        assert "tool_call" in r.text


async def test_audit_unauthenticated_redirects(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/audit", follow_redirects=False)
        assert r.status_code == 303
