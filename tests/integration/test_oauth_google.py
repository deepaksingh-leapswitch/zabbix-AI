"""Integration tests for Google SSO OAuth routes (Feature 2)."""
from __future__ import annotations

import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from zabbix_ai.admin.routes import auth_routes, oauth_google
from zabbix_ai.memory import Memory

SECRET = "test-session-secret-32-bytes-ok!"
TTL = 3600

_TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent.parent / "zabbix_ai" / "templates"
)


def _make_app_no_sso(memory: Memory) -> FastAPI:
    """App with Google SSO disabled (oauth_google=None)."""
    app = FastAPI()
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False
    app.state.memory = memory
    app.state.settings = types.SimpleNamespace(oauth_google=None)
    app.include_router(auth_routes.router)
    app.include_router(oauth_google.router)
    return app


def _make_app_with_sso(memory: Memory, *,
                        client_id: str = "test-client-id.apps.googleusercontent.com",
                        allowed_domain: str = "") -> FastAPI:
    """App with Google SSO configured."""
    from pydantic import SecretStr

    from zabbix_ai.config import OAuthGoogleSettings

    sso_settings = OAuthGoogleSettings(
        client_id=client_id,
        client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
        allowed_email_domain=allowed_domain,
        default_role="viewer",
        client_secret=SecretStr("test-client-secret"),
    )
    app = FastAPI()
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.session_secret = SECRET
    app.state.session_ttl = TTL
    app.state.cookie_secure = False
    app.state.memory = memory
    app.state.settings = types.SimpleNamespace(oauth_google=sso_settings)
    app.include_router(auth_routes.router)
    app.include_router(oauth_google.router)
    return app


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


# --- Tests: SSO disabled ---

def test_start_returns_503_when_sso_not_configured(mem):
    app = _make_app_no_sso(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/oauth/google/start", follow_redirects=False)
        assert r.status_code == 503


def test_callback_returns_503_when_sso_not_configured(mem):
    app = _make_app_no_sso(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get(
            "/admin/oauth/google/callback",
            params={"code": "somecode", "state": "somestate"},
            follow_redirects=False,
        )
        assert r.status_code == 503


def test_login_page_no_google_button_when_sso_disabled(mem):
    app = _make_app_no_sso(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/login")
        assert r.status_code == 200
        assert "Sign in with Google" not in r.text


# --- Tests: SSO enabled ---

def test_start_redirects_to_google_when_sso_configured(mem):
    app = _make_app_with_sso(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/oauth/google/start", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth")


def test_start_redirect_has_required_params(mem):
    client_id = "test-client-id.apps.googleusercontent.com"
    app = _make_app_with_sso(mem, client_id=client_id)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/oauth/google/start", follow_redirects=False)
    loc = r.headers["location"]
    parsed = urlparse(loc)
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == [client_id]
    assert qs["response_type"] == ["code"]
    assert "openid" in qs["scope"][0]
    assert "email" in qs["scope"][0]
    assert "state" in qs
    assert "nonce" in qs


def test_start_sets_pkce_cookie(mem):
    app = _make_app_with_sso(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/oauth/google/start", follow_redirects=False)
    assert "zai_oauth_pkce" in r.cookies


def test_start_includes_hd_param_when_domain_set(mem):
    app = _make_app_with_sso(mem, allowed_domain="leapswitch.com")
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/oauth/google/start", follow_redirects=False)
    loc = r.headers["location"]
    qs = parse_qs(urlparse(loc).query)
    assert qs.get("hd") == ["leapswitch.com"]


def test_login_page_shows_google_button_when_sso_configured(mem):
    app = _make_app_with_sso(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/admin/login")
    assert r.status_code == 200
    assert "Sign in with Google" in r.text
    assert "/admin/oauth/google/start" in r.text


def test_callback_returns_400_with_no_cookie(mem):
    app = _make_app_with_sso(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get(
            "/admin/oauth/google/callback",
            params={"code": "someauth", "state": "somestate"},
            follow_redirects=False,
        )
    assert r.status_code == 400


def test_callback_returns_400_with_missing_code(mem):
    app = _make_app_with_sso(mem)
    with TestClient(app, raise_server_exceptions=True) as client:
        # First get a valid pkce cookie from /start
        r_start = client.get("/admin/oauth/google/start", follow_redirects=False)
        assert "zai_oauth_pkce" in r_start.cookies
        # Now call callback without code
        r = client.get(
            "/admin/oauth/google/callback",
            params={"state": "somestate"},
            follow_redirects=False,
        )
    assert r.status_code == 400
