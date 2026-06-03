from pathlib import Path

import pytest

from zabbix_ai.admin import connections_store as cs
from zabbix_ai.admin.config_overlay import resolve_oauth_google
from zabbix_ai.admin.crypto import derive_key
from zabbix_ai.config import OAuthGoogleSettings
from zabbix_ai.memory import Memory

KEY = derive_key("unit-test-master")


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "t.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()


async def test_returns_base_when_no_overlay(memory):
    base = OAuthGoogleSettings(client_id="file.apps", client_secret_env="X")
    assert (await resolve_oauth_google(memory, KEY, base)) is base
    assert (await resolve_oauth_google(memory, KEY, None)) is None


async def test_overlay_wins(memory):
    await cs.conn_upsert(memory, type_="oauth_google", name="primary",
                         config={"client_id": "db.apps",
                                 "allowed_email_domain": "leapswitch.com",
                                 "default_role": "operator"}, enabled=True)
    await cs.secret_set(memory, key="oauth_google:primary:client_secret",
                        value="db-secret", crypto_key=KEY)
    got = await resolve_oauth_google(memory, KEY, None)
    assert got is not None
    assert got.client_id == "db.apps"
    assert got.allowed_email_domain == "leapswitch.com"
    assert got.default_role == "operator"
    assert got.client_secret.get_secret_value() == "db-secret"


async def test_overlay_disabled_falls_back(memory):
    await cs.conn_upsert(memory, type_="oauth_google", name="primary",
                         config={"client_id": "db.apps"}, enabled=False)
    await cs.secret_set(memory, key="oauth_google:primary:client_secret",
                        value="db-secret", crypto_key=KEY)
    base = OAuthGoogleSettings(client_id="file.apps", client_secret_env="X")
    assert (await resolve_oauth_google(memory, KEY, base)) is base


async def test_overlay_missing_secret_falls_back(memory):
    await cs.conn_upsert(memory, type_="oauth_google", name="primary",
                         config={"client_id": "db.apps"}, enabled=True)
    # no secret stored → cannot use overlay
    assert (await resolve_oauth_google(memory, KEY, None)) is None


async def test_overlay_missing_client_id_falls_back(memory):
    await cs.conn_upsert(memory, type_="oauth_google", name="primary",
                         config={"allowed_email_domain": "x.com"}, enabled=True)
    await cs.secret_set(memory, key="oauth_google:primary:client_secret",
                        value="db-secret", crypto_key=KEY)
    assert (await resolve_oauth_google(memory, KEY, None)) is None
