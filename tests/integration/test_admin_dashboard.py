from __future__ import annotations

from datetime import UTC
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

    from zabbix_ai.admin.routes import auth_routes, dashboard

    app = FastAPI()
    templates_dir = Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.memory = mem
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False

    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    return app


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


@pytest.fixture
async def authed_client(mem):
    u = await users.create_user(mem, username="admin", password="adminpw", role="admin")
    await users.set_totp_enrolled(mem, u["id"])
    totp_secret = u["totp_secret"]

    app = _make_app(mem)
    code = pyotp.TOTP(totp_secret).now()
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post("/admin/login", data={
            "username": "admin", "password": "adminpw", "totp_code": code,
        }, follow_redirects=False)
        yield client


def test_dashboard_unauthenticated_redirects(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin", follow_redirects=False)
        assert r.status_code == 303
        assert "/admin/login" in r.headers["location"]


def test_dashboard_returns_200_when_authed(authed_client):
    r = authed_client.get("/admin", follow_redirects=False)
    assert r.status_code == 200
    assert "Dashboard" in r.text


async def test_dashboard_shows_investigation_counts(mem, tmp_path):
    """Seed investigations and verify counts appear in dashboard."""
    from datetime import datetime

    u = await users.create_user(mem, username="admin2", password="pw", role="admin")
    await users.set_totp_enrolled(mem, u["id"])
    totp_secret = u["totp_secret"]

    # Seed two investigations with today's timestamp
    now = datetime.now(UTC).isoformat()
    await mem.execute(
        """INSERT INTO investigations (source, started_at, tokens_in, tokens_out)
           VALUES (?, ?, ?, ?)""",
        ("cli", now, 100, 50),
    )
    await mem.execute(
        """INSERT INTO investigations (source, started_at, tokens_in, tokens_out)
           VALUES (?, ?, ?, ?)""",
        ("slack", now, 200, 80),
    )

    app = _make_app(mem)
    code = pyotp.TOTP(totp_secret).now()
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post("/admin/login", data={
            "username": "admin2", "password": "pw", "totp_code": code,
        }, follow_redirects=False)
        r = client.get("/admin")
        assert r.status_code == 200
        assert "2" in r.text  # inv_today count
