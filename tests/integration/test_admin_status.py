"""Integration tests for /admin/status and /admin/status.json (v1.4)."""
from __future__ import annotations

from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from zabbix_ai import __version__
from zabbix_ai.admin import users
from zabbix_ai.admin.crypto import derive_key
from zabbix_ai.memory import Memory
from zabbix_ai.services.connection_health import record_health

SECRET = "test-session-secret-32-bytes-ok!"
TTL = 3600
_CRYPTO_KEY = derive_key(SECRET)


def _make_app(mem: Memory) -> object:
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from zabbix_ai.admin.routes import auth_routes, status
    from zabbix_ai.config import Settings

    app = FastAPI()
    templates_dir = (
        Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
    )
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
    app.include_router(status.router)
    return app


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


async def _login(client, mem, *, username: str, password: str, role: str) -> None:
    u = await users.create_user(mem, username=username, password=password, role=role)
    await users.set_totp_enrolled(mem, u["id"])
    code = pyotp.TOTP(u["totp_secret"]).now()
    client.post("/admin/login", data={
        "username": username, "password": password, "totp_code": code,
    }, follow_redirects=False)


def test_status_unauthenticated_redirects(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/status", follow_redirects=False)
        assert r.status_code == 303
        r = client.get("/admin/status.json", follow_redirects=False)
        assert r.status_code == 303


async def test_status_admin_returns_200_and_shows_version(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="admin", password="adminpw",
                      role="admin")
        r = client.get("/admin/status", follow_redirects=False)
        assert r.status_code == 200
        assert __version__ in r.text
        assert "System status" in r.text


async def test_status_json_admin_returns_expected_keys(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="admin", password="adminpw",
                      role="admin")
        r = client.get("/admin/status.json", follow_redirects=False)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        for key in ("app", "database", "zabbix", "anthropic",
                    "secrets_count", "investigations", "tables",
                    "generated_at"):
            assert key in body, f"missing {key} in status JSON"
        assert body["app"]["version"] == __version__


async def test_status_viewer_is_forbidden(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="viewer", password="vpw",
                      role="viewer")
        r = client.get("/admin/status", follow_redirects=False)
        assert r.status_code == 403
        r = client.get("/admin/status.json", follow_redirects=False)
        assert r.status_code == 403


async def test_status_shows_recorded_anthropic_health(mem):
    # Record one success and one failure for Anthropic.
    await record_health(mem, kind="anthropic", name="primary", ok=True)
    await record_health(mem, kind="anthropic", name="primary",
                        ok=False, error="HTTP 529 overloaded")

    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="admin", password="adminpw",
                      role="admin")
        r = client.get("/admin/status.json", follow_redirects=False)
        assert r.status_code == 200
        body = r.json()
        anth = body["anthropic"]
        # State is "error" because the most-recent timestamp is the failure.
        assert anth["state"] == "error"
        assert anth["last_error"] == "HTTP 529 overloaded"
        assert anth["last_success_at"]
        assert anth["last_error_at"]

        # And the HTML page should mention the error message.
        r = client.get("/admin/status")
        assert r.status_code == 200
        assert "HTTP 529 overloaded" in r.text


async def test_status_json_includes_zabbix_instances(mem, tmp_path):
    # Build an app whose settings has a zabbix instance, then record health.
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from zabbix_ai.admin.routes import auth_routes, status
    from zabbix_ai.config import Settings, ZabbixInstance

    app = FastAPI()
    templates_dir = (
        Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
    )
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.memory = mem
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False
    app.state.crypto_key = _CRYPTO_KEY
    app.state.settings = Settings(
        zabbix_instances=[ZabbixInstance(
            name="monitoring",
            url="https://zbx.test",  # type: ignore[arg-type]
            token_env="ZBX_TOK",
        )],
        anthropic_api_key="sk-test",  # type: ignore[arg-type]
    )
    app.include_router(auth_routes.router)
    app.include_router(status.router)

    await record_health(mem, kind="zabbix", name="monitoring", ok=True)

    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="admin", password="adminpw",
                      role="admin")
        r = client.get("/admin/status.json", follow_redirects=False)
        assert r.status_code == 200
        body = r.json()
        assert len(body["zabbix"]) == 1
        assert body["zabbix"][0]["name"] == "monitoring"
        assert body["zabbix"][0]["state"] == "ok"
