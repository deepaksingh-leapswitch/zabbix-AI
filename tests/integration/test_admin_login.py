from __future__ import annotations

from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from zabbix_ai.admin import users
from zabbix_ai.memory import Memory

SECRET = "test-session-secret-32-bytes-ok!"
TTL = 3600


def _make_app(db_path: str):
    """Build a minimal FastAPI app with admin state pre-configured."""
    import types

    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from zabbix_ai.admin.routes import auth_routes

    app = FastAPI()
    templates_dir = Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False
    # Provide a minimal settings stub (oauth_google=None → no Google SSO button)
    settings_stub = types.SimpleNamespace(oauth_google=None)
    app.state.settings = settings_stub

    app.include_router(auth_routes.router)
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
async def seeded(mem, tmp_path):
    """Create a test user with TOTP enrolled and return (app, totp_secret)."""
    u = await users.create_user(mem, username="admin", password="adminpw", role="admin")
    totp_secret = u["totp_secret"]
    await users.set_totp_enrolled(mem, u["id"])

    app = _make_app(str(tmp_path / "test.db"))
    app.state.memory = mem
    return app, totp_secret


def test_login_page_returns_200(seeded):
    app, _ = seeded
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/login")
        assert r.status_code == 200
        assert "Sign in" in r.text


def test_login_invalid_credentials_returns_400(seeded):
    app, _ = seeded
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post("/admin/login", data={
            "username": "admin", "password": "wrongpw", "totp_code": "",
        })
        assert r.status_code == 400
        assert "invalid credentials" in r.text


def test_login_valid_creds_no_totp_returns_400(seeded):
    app, _ = seeded
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post("/admin/login", data={
            "username": "admin", "password": "adminpw", "totp_code": "",
        })
        assert r.status_code == 400
        assert "TOTP" in r.text


def test_login_valid_creds_valid_totp_sets_cookie(seeded):
    app, totp_secret = seeded
    code = pyotp.TOTP(totp_secret).now()
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post("/admin/login", data={
            "username": "admin", "password": "adminpw", "totp_code": code,
        }, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin"
        assert "zai_session" in r.cookies


def test_logout_clears_cookie(seeded):
    app, totp_secret = seeded
    code = pyotp.TOTP(totp_secret).now()
    with TestClient(app, raise_server_exceptions=True) as client:
        # login first
        client.post("/admin/login", data={
            "username": "admin", "password": "adminpw", "totp_code": code,
        }, follow_redirects=False)
        assert "zai_session" in client.cookies

        # logout
        r = client.get("/admin/logout", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login"
        # session cookie should be cleared
        assert "zai_session" not in client.cookies


async def test_unenrolled_user_redirected_to_enroll(mem, tmp_path):
    """User without TOTP enrolled should be redirected to enroll-totp page."""
    u = await users.create_user(mem, username="newuser", password="newpw", role="viewer")
    assert u["totp_secret"]

    app = _make_app(str(tmp_path / "test.db"))
    app.state.memory = mem

    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.post("/admin/login", data={
            "username": "newuser", "password": "newpw", "totp_code": "",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert "/admin/enroll-totp" in r.headers["location"]
        assert "zai_pretotp" in r.cookies
