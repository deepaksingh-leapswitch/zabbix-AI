"""Integration tests for /admin/connections/hostbill/links routes."""
from __future__ import annotations

from datetime import UTC, datetime
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


def _make_app(mem: Memory):
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from zabbix_ai.admin.routes import auth_routes, hostbill_links
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
    app.state.zabbix_clients = {}
    app.state.hostbill_client = None

    app.include_router(auth_routes.router)
    app.include_router(hostbill_links.router)
    return app


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


async def _seed_link(mem, *, instance="monitoring", hostid=17977,
                     linked_by="auto:ip", confidence="high",
                     service_id=88, client_id=7,
                     client_name="Acme Pvt", domain="plesk1.example.com"):
    await mem.execute(
        "INSERT INTO host_hostbill_link "
        "(zabbix_instance, zabbix_hostid, hostbill_service_id, "
        " hostbill_client_id, hostbill_client_name, hostbill_domain, "
        " linked_at, linked_by, confidence) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (instance, hostid, service_id, client_id, client_name, domain,
         datetime.now(UTC).isoformat(), linked_by, confidence),
    )


async def _make_authed_client(mem, *, role="admin"):
    u = await users.create_user(
        mem, username=role, password=f"{role}pw", role=role,
    )
    await users.set_totp_enrolled(mem, u["id"])
    app = _make_app(mem)
    code = pyotp.TOTP(u["totp_secret"]).now()
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    r = client.post("/admin/login", data={
        "username": role, "password": f"{role}pw", "totp_code": code,
    }, follow_redirects=False)
    assert r.status_code == 303
    return client


async def test_viewer_forbidden(mem):
    await _seed_link(mem)
    client = await _make_authed_client(mem, role="viewer")
    r = client.get(
        "/admin/connections/hostbill/links", follow_redirects=False,
    )
    assert r.status_code == 403


async def test_admin_sees_seeded_rows(mem):
    await _seed_link(mem, hostid=17977, client_name="Acme Pvt")
    await _seed_link(mem, hostid=11653, client_name="Beta Ltd",
                     service_id=99, client_id=8, domain="plesk2.example.com")
    client = await _make_authed_client(mem, role="admin")
    r = client.get(
        "/admin/connections/hostbill/links", follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Acme Pvt" in r.text
    assert "Beta Ltd" in r.text
    # Both hostids visible
    assert "17977" in r.text
    assert "11653" in r.text


async def test_unlinked_filter_excludes_high_confidence(mem):
    await _seed_link(mem, hostid=1, confidence="high",
                     client_name="High Conf")
    await _seed_link(mem, hostid=2, confidence="low",
                     linked_by="auto:hostname",
                     client_name="Low Conf")
    await _seed_link(mem, hostid=3, linked_by="unlinked",
                     confidence="low", service_id=None,
                     client_id=None, client_name="", domain="")
    client = await _make_authed_client(mem, role="admin")
    r = client.get(
        "/admin/connections/hostbill/links?show=unlinked",
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "High Conf" not in r.text
    assert "Low Conf" in r.text


async def test_manual_override_updates_row_and_audits(mem):
    await _seed_link(mem, hostid=17977,
                     linked_by="auto:hostname", confidence="low",
                     service_id=88, client_name="Wrong Name")
    client = await _make_authed_client(mem, role="admin")
    r = client.post(
        "/admin/connections/hostbill/links/monitoring/17977",
        data={
            "hostbill_service_id": "42",
            "hostbill_client_id": "7",
            "hostbill_client_name": "Correct Name",
            "hostbill_domain": "plesk1.example.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    row = await mem.fetchone(
        "SELECT hostbill_service_id, hostbill_client_name, linked_by, confidence "
        "FROM host_hostbill_link "
        "WHERE zabbix_instance=? AND zabbix_hostid=?",
        ("monitoring", 17977),
    )
    assert row == (42, "Correct Name", "manual", "high")

    audit = await mem.fetchone(
        "SELECT event_type, by_user, target FROM admin_audit_log "
        "WHERE event_type=? ORDER BY id DESC LIMIT 1",
        ("hostbill_link_manual",),
    )
    assert audit is not None
    assert audit[1] == "admin"
    assert audit[2] == "monitoring:17977"


async def test_edit_form_renders_existing_link(mem):
    await _seed_link(mem, hostid=17977, service_id=88,
                     client_name="Acme Pvt", domain="plesk1.example.com")
    client = await _make_authed_client(mem, role="admin")
    r = client.get(
        "/admin/connections/hostbill/links/monitoring/17977/edit",
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "88" in r.text
    assert "Acme Pvt" in r.text
