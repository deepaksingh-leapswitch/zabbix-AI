"""Integration tests for the operator resolution-edit form on
``/admin/investigations/{id}/resolution``.
"""
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
    templates_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "zabbix_ai" / "templates"
    )
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
    migrations = (
        Path(__file__).resolve().parent.parent.parent / "migrations"
    )
    await m.run_migrations(migrations)
    yield m
    await m.close()


async def _login(client: TestClient, mem: Memory, *,
                  username: str, password: str, role: str) -> None:
    u = await users.create_user(mem, username=username,
                                  password=password, role=role)
    await users.set_totp_enrolled(mem, u["id"])
    code = pyotp.TOTP(u["totp_secret"]).now()
    r = client.post("/admin/login", data={
        "username": username, "password": password, "totp_code": code,
    }, follow_redirects=False)
    assert r.status_code == 303, r.text


async def _insert_inv(mem: Memory) -> int:
    now = datetime.now(UTC).isoformat()
    await mem.execute(
        """INSERT INTO investigations (source, started_at, hostname)
           VALUES (?, ?, ?)""",
        ("cli", now, "srv01"),
    )
    row = await mem.fetchone("SELECT last_insert_rowid()")
    return int(row[0])


# ─── operator can POST resolution ───────────────────────────────────────────

async def test_operator_post_resolution_updates_db_and_audit(mem):
    inv_id = await _insert_inv(mem)
    app = _make_app(mem)

    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="op1",
                      password="oppass1", role="operator")

        r = client.post(
            f"/admin/investigations/{inv_id}/resolution",
            data={"resolution_notes": "Rotated logs, freed 14 GB."},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        assert r.headers["location"] == f"/admin/investigations/{inv_id}"

    row = await mem.fetchone(
        """SELECT resolution_notes, resolution_by, resolution_source
           FROM investigations WHERE id=?""",
        (inv_id,),
    )
    notes, by, source = row
    assert notes == "Rotated logs, freed 14 GB."
    assert by == "op1"
    assert source == "manual"

    audit = await mem.fetchone(
        """SELECT event_type, by_user, target
           FROM admin_audit_log
           WHERE event_type='investigation_resolution'""",
    )
    assert audit is not None
    assert audit[1] == "op1"
    assert audit[2] == f"investigation:{inv_id}"


# ─── viewer cannot POST resolution ──────────────────────────────────────────

async def test_viewer_post_resolution_forbidden(mem):
    inv_id = await _insert_inv(mem)
    app = _make_app(mem)

    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="view1",
                      password="viewpass1", role="viewer")
        r = client.post(
            f"/admin/investigations/{inv_id}/resolution",
            data={"resolution_notes": "should be rejected"},
            follow_redirects=False,
        )
        assert r.status_code == 403, r.text

    row = await mem.fetchone(
        "SELECT resolution_notes FROM investigations WHERE id=?",
        (inv_id,),
    )
    assert row[0] is None


# ─── GET detail surfaces existing resolution ────────────────────────────────

async def test_investigation_detail_shows_resolution(mem):
    inv_id = await _insert_inv(mem)
    await mem.execute(
        """UPDATE investigations
           SET resolution_notes=?, resolution_at=?,
               resolution_by=?, resolution_source=?
           WHERE id=?""",
        ("DISM /Online /Cleanup-Image — freed 8 GB",
         "2026-05-10T03:00:00+00:00", "deepak", "zabbix_ack", inv_id),
    )

    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="admin",
                      password="adminpw", role="admin")
        r = client.get(f"/admin/investigations/{inv_id}")
        assert r.status_code == 200
        assert "DISM /Online /Cleanup-Image" in r.text
        assert "deepak" in r.text
        assert "zabbix_ack" in r.text


# ─── clearing notes removes resolution metadata ──────────────────────────────

async def test_operator_clearing_resolution_nulls_columns(mem):
    inv_id = await _insert_inv(mem)
    # Seed an existing resolution
    await mem.execute(
        """UPDATE investigations
           SET resolution_notes='old', resolution_at='2026-05-01T00:00:00+00:00',
               resolution_by='someone', resolution_source='zabbix_ack'
           WHERE id=?""",
        (inv_id,),
    )

    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="op2",
                      password="oppass2", role="operator")
        r = client.post(
            f"/admin/investigations/{inv_id}/resolution",
            data={"resolution_notes": "   "},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

    row = await mem.fetchone(
        """SELECT resolution_notes, resolution_at, resolution_by,
                  resolution_source
           FROM investigations WHERE id=?""",
        (inv_id,),
    )
    assert row == (None, None, None, None)
