from __future__ import annotations

from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from zabbix_ai.admin import users
from zabbix_ai.memory import Memory, upsert_host_facts, upsert_pattern

SECRET = "test-session-secret-32-bytes-ok!"
TTL = 3600


def _make_app(mem: Memory) -> object:
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from zabbix_ai.admin.routes import auth_routes, memory_routes

    app = FastAPI()
    templates_dir = Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.memory = mem
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False

    app.include_router(auth_routes.router)
    app.include_router(memory_routes.router)
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


async def test_patterns_list_empty(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/patterns")
        assert r.status_code == 200
        assert "No patterns recorded yet" in r.text


async def test_patterns_list_shows_patterns(mem):
    await upsert_pattern(mem, signature="abc123deadbeef01",
                          typical_root_cause="disk full", typical_fix="clean logs")
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/patterns")
        assert r.status_code == 200
        assert "abc123deadbeef01" in r.text
        assert "disk full" in r.text


async def test_pattern_detail(mem):
    await upsert_pattern(mem, signature="abc123deadbeef01",
                          typical_root_cause="disk full", typical_fix="clean logs")
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/patterns/abc123deadbeef01")
        assert r.status_code == 200
        assert "disk full" in r.text
        assert "clean logs" in r.text


async def test_pattern_detail_not_found(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/patterns/nosuchpattern")
        assert r.status_code == 404


async def test_host_facts_list_empty(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/host-facts")
        assert r.status_code == 200
        assert "No host facts found" in r.text


async def test_host_facts_list_shows_facts(mem):
    await upsert_host_facts(mem, hostid=42,
                             facts={"os": "Ubuntu 22.04", "cpu": "8 cores"})
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/host-facts")
        assert r.status_code == 200
        assert "42" in r.text
        assert "Ubuntu 22.04" in r.text


async def test_host_facts_filter_by_hostid(mem):
    await upsert_host_facts(mem, hostid=42, facts={"os": "Ubuntu 22.04"})
    await upsert_host_facts(mem, hostid=99, facts={"os": "CentOS 7"})
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem)
        r = client.get("/admin/host-facts?hostid=42")
        assert r.status_code == 200
        assert "Ubuntu 22.04" in r.text
        assert "CentOS 7" not in r.text


async def test_memory_routes_unauthenticated(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        for path in ["/admin/patterns", "/admin/host-facts"]:
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 303
