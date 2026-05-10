"""Integration tests for /admin/connections routes."""
from __future__ import annotations

from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from zabbix_ai.admin import users
from zabbix_ai.admin.crypto import derive_key
from zabbix_ai.memory import Memory

SECRET = "test-session-secret-32-bytes-ok!"
TTL = 3600
_CRYPTO_KEY = derive_key(SECRET)


def _make_app(mem: Memory) -> object:
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from zabbix_ai.admin.routes import auth_routes, connections
    from zabbix_ai.config import Settings

    app = FastAPI()
    templates_dir = Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.memory = mem
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False
    app.state.crypto_key = _CRYPTO_KEY
    app.state.settings = Settings(
        zabbix_instances=[],
        anthropic_api_key="sk-test",  # type: ignore[arg-type]
    )

    app.include_router(auth_routes.router)
    app.include_router(connections.router)
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
async def admin_client(mem):
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


@pytest.fixture
async def viewer_client(mem):
    u = await users.create_user(mem, username="viewer", password="viewpw", role="viewer")
    await users.set_totp_enrolled(mem, u["id"])
    totp_secret = u["totp_secret"]

    app = _make_app(mem)
    code = pyotp.TOTP(totp_secret).now()
    with TestClient(app, raise_server_exceptions=True) as client:
        client.post("/admin/login", data={
            "username": "viewer", "password": "viewpw", "totp_code": code,
        }, follow_redirects=False)
        yield client


def test_connections_overview_unauthenticated_redirects(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/connections", follow_redirects=False)
        assert r.status_code == 303


def test_connections_overview_admin_returns_200(admin_client):
    r = admin_client.get("/admin/connections", follow_redirects=False)
    assert r.status_code == 200
    assert "Connections" in r.text


def test_connections_viewer_forbidden(viewer_client):
    r = viewer_client.get("/admin/connections", follow_redirects=False)
    assert r.status_code == 403


def test_zabbix_list_returns_200(admin_client):
    r = admin_client.get("/admin/connections/zabbix", follow_redirects=False)
    assert r.status_code == 200


def test_zabbix_new_form_returns_200(admin_client):
    r = admin_client.get("/admin/connections/zabbix/new", follow_redirects=False)
    assert r.status_code == 200


def test_hostbill_form_returns_200(admin_client):
    r = admin_client.get("/admin/connections/hostbill", follow_redirects=False)
    assert r.status_code == 200


def test_slack_form_returns_200(admin_client):
    r = admin_client.get("/admin/connections/slack", follow_redirects=False)
    assert r.status_code == 200


def test_anthropic_form_returns_200(admin_client):
    r = admin_client.get("/admin/connections/anthropic", follow_redirects=False)
    assert r.status_code == 200


def test_oauth_google_form_returns_200(admin_client):
    r = admin_client.get("/admin/connections/oauth-google", follow_redirects=False)
    assert r.status_code == 200


def test_zabbix_ui_form_returns_200(admin_client):
    r = admin_client.get("/admin/connections/zabbix-ui", follow_redirects=False)
    assert r.status_code == 200


def test_zabbix_save_and_list(admin_client):
    r = admin_client.post(
        "/admin/connections/zabbix/save",
        data={"name": "prod", "url": "https://zabbix.example.com",
              "token": "mytoken", "enabled": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r2 = admin_client.get("/admin/connections/zabbix", follow_redirects=False)
    assert "prod" in r2.text


def test_anthropic_save_and_form_shows_configured(admin_client):
    r = admin_client.post(
        "/admin/connections/anthropic/save",
        data={"api_key": "sk-ant-test"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r2 = admin_client.get("/admin/connections/anthropic", follow_redirects=False)
    assert "**********" in r2.text or "leave blank" in r2.text
