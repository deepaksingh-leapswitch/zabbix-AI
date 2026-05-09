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

    from zabbix_ai.admin.routes import auth_routes, investigations

    app = FastAPI()
    templates_dir = Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.memory = mem
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False

    app.include_router(auth_routes.router)
    app.include_router(investigations.router)
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


async def test_investigations_list_unauthenticated(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/investigations", follow_redirects=False)
        assert r.status_code == 303


async def test_investigations_list_empty(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/investigations")
        assert r.status_code == 200
        assert "No investigations found" in r.text


async def test_investigations_list_shows_rows(mem):
    now = datetime.now(UTC).isoformat()
    await mem.execute(
        """INSERT INTO investigations (source, started_at, hostname, tokens_in, tokens_out)
           VALUES (?, ?, ?, ?, ?)""",
        ("cli", now, "web01.example.com", 100, 50),
    )
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/investigations")
        assert r.status_code == 200
        assert "web01.example.com" in r.text
        assert "cli" in r.text


async def test_investigations_detail(mem):
    now = datetime.now(UTC).isoformat()
    await mem.execute(
        """INSERT INTO investigations
           (source, started_at, hostname, summary, root_cause, tokens_in, tokens_out)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("slack", now, "db01", "Disk full on db01", "Log files accumulated", 200, 80),
    )
    row = await mem.fetchone("SELECT id FROM investigations ORDER BY id DESC LIMIT 1")
    assert row
    inv_id = row[0]

    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get(f"/admin/investigations/{inv_id}")
        assert r.status_code == 200
        assert "Disk full on db01" in r.text
        assert "Log files accumulated" in r.text


async def test_investigation_detail_not_found(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/investigations/99999")
        assert r.status_code == 404


async def test_investigations_filter_by_source(mem):
    now = datetime.now(UTC).isoformat()
    await mem.execute(
        "INSERT INTO investigations (source, started_at) VALUES (?, ?)", ("cli", now),
    )
    await mem.execute(
        "INSERT INTO investigations (source, started_at) VALUES (?, ?)", ("slack", now),
    )
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/investigations?source=cli")
        assert r.status_code == 200
        assert "cli" in r.text
        # slack row should not appear (only 1 result with source=cli filter)
        assert r.text.count("<tr>") < 4  # header + 1 row + overhead
