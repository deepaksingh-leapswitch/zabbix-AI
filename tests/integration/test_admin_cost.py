"""Integration tests for /admin/cost routes (v1.4 part 2)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

    from zabbix_ai.admin.routes import auth_routes, cost
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
    app.include_router(cost.router)
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


async def _seed_investigations(mem: Memory) -> list[int]:
    """Insert 3 investigations with known token counts:
      - id=1: today, sonnet, 1M in + 1M out  → $18 → ₹1494
      - id=2: today, haiku, 500k in + 500k out → $2.40 → ₹199.2
      - id=3: 10 days ago, sonnet, 100k in + 0 out → $0.30 → ₹24.9
    Returns list of inserted ids.
    """
    now = datetime.now(UTC)
    today_iso = now.isoformat()
    ten_days_ago = (now - timedelta(days=10)).isoformat()

    await mem.execute(
        """INSERT INTO investigations
           (id, source, started_at, hostid, hostname, tokens_in, tokens_out, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (1, "cli", today_iso, 101, "host-a",
         1_000_000, 1_000_000, "claude-sonnet-4-6"),
    )
    await mem.execute(
        """INSERT INTO investigations
           (id, source, started_at, hostid, hostname, tokens_in, tokens_out, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (2, "slack", today_iso, 102, "host-b",
         500_000, 500_000, "claude-haiku-4-5-20251001"),
    )
    await mem.execute(
        """INSERT INTO investigations
           (id, source, started_at, hostid, hostname, tokens_in, tokens_out, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (3, "cli", ten_days_ago, 101, "host-a",
         100_000, 0, "claude-sonnet-4-6"),
    )
    return [1, 2, 3]


async def test_cost_unauthenticated_redirects(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/cost", follow_redirects=False)
        assert r.status_code == 303
        assert "/admin/login" in r.headers["location"]


async def test_cost_export_unauthenticated_redirects(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/cost/export.csv", follow_redirects=False)
        assert r.status_code == 303


async def test_cost_viewer_forbidden(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="viewer", password="pw", role="viewer")
        r = client.get("/admin/cost", follow_redirects=False)
        assert r.status_code == 403
        r2 = client.get("/admin/cost/export.csv", follow_redirects=False)
        assert r2.status_code == 403


async def test_cost_dashboard_admin_shows_totals(mem):
    await _seed_investigations(mem)
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="admin", password="adminpw",
                     role="admin")
        r = client.get("/admin/cost", follow_redirects=False)
        assert r.status_code == 200
        assert "Cost dashboard" in r.text
        # Today total: ₹1494 (sonnet 1M+1M = $18) + ₹199.20 (haiku 0.5M+0.5M
        # = $2.40) = ₹1693.20 — verify the rupee total appears.
        assert "1693.20" in r.text
        # Seeded hosts/sources should appear.
        assert "host-a" in r.text
        assert "host-b" in r.text
        assert "cli" in r.text
        assert "slack" in r.text
        # CSV download link present.
        assert "/admin/cost/export.csv" in r.text


async def test_cost_dashboard_empty_state(mem):
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="admin", password="adminpw",
                     role="admin")
        r = client.get("/admin/cost", follow_redirects=False)
        assert r.status_code == 200
        # No spend → headline shows ₹0.00.
        assert "0.00" in r.text


async def test_cost_export_csv(mem):
    ids = await _seed_investigations(mem)
    app = _make_app(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        await _login(client, mem, username="admin", password="adminpw",
                     role="admin")
        r = client.get("/admin/cost/export.csv", follow_redirects=False)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        body = r.text
        # Header row.
        assert "id,started_at,source,hostname,model,tokens_in,tokens_out,cost_inr" in body
        # All seeded ids appear.
        for inv_id in ids:
            assert f"\n{inv_id}," in body or body.startswith(f"{inv_id},")
        # Known cost on id=1: sonnet 1M+1M = $18 → ₹1494.0000
        assert "1494.0000" in body
