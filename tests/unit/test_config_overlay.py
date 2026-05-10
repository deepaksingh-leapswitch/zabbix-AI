"""Unit tests for zabbix_ai.admin.config_overlay."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from zabbix_ai.admin.config_overlay import overlay_settings
from zabbix_ai.admin.connections_store import conn_upsert, secret_set
from zabbix_ai.admin.crypto import derive_key
from zabbix_ai.config import (
    Settings,
    ZabbixInstance,
)
from zabbix_ai.memory import Memory

_MIGRATIONS = Path("migrations")
_KEY = derive_key("overlay-test-key")


def _base_settings() -> Settings:
    """Minimal settings with one Zabbix instance."""
    return Settings(
        zabbix_instances=[
            ZabbixInstance(
                name="file-inst",
                url="https://zabbix-file.example.com",  # type: ignore[arg-type]
                token_env="ZABBIX_TOKEN_FILE",
                token=SecretStr("file-token"),
            )
        ],
        anthropic_api_key=SecretStr("file-anthropic-key"),
    )


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    await m.run_migrations(_MIGRATIONS)
    yield m
    await m.close()


async def test_empty_db_returns_settings_unchanged(mem):
    s = _base_settings()
    result = await overlay_settings(s, mem, _KEY)
    assert len(result.zabbix_instances) == 1
    assert result.zabbix_instances[0].name == "file-inst"
    assert result.anthropic_api_key.get_secret_value() == "file-anthropic-key"
    assert result.hostbill is None
    assert result.slack is None
    assert result.oauth_google is None


async def test_zabbix_instance_from_db_replaces_file(mem):
    await conn_upsert(mem, type_="zabbix", name="db-inst",
                      config={"url": "https://zabbix-db.example.com"})
    await secret_set(mem, key="zabbix:db-inst:token",
                     value="db-token", crypto_key=_KEY)

    s = _base_settings()
    result = await overlay_settings(s, mem, _KEY)

    assert len(result.zabbix_instances) == 1
    assert result.zabbix_instances[0].name == "db-inst"
    assert result.zabbix_instances[0].token.get_secret_value() == "db-token"


async def test_zabbix_disabled_instance_skipped(mem):
    await conn_upsert(mem, type_="zabbix", name="disabled-inst",
                      config={"url": "https://zabbix-db.example.com"},
                      enabled=False)
    await secret_set(mem, key="zabbix:disabled-inst:token",
                     value="token", crypto_key=_KEY)

    s = _base_settings()
    result = await overlay_settings(s, mem, _KEY)
    # DB row exists but is disabled, so file config should remain
    assert result.zabbix_instances[0].name == "file-inst"


async def test_hostbill_from_db(mem):
    await conn_upsert(mem, type_="hostbill", name="primary",
                      config={"api_url": "https://hb.example.com/admin/api.php"})
    await secret_set(mem, key="hostbill:primary:api_id",
                     value="id-123", crypto_key=_KEY)
    await secret_set(mem, key="hostbill:primary:api_key",
                     value="key-abc", crypto_key=_KEY)

    s = _base_settings()
    result = await overlay_settings(s, mem, _KEY)

    assert result.hostbill is not None
    assert result.hostbill.api_id.get_secret_value() == "id-123"
    assert result.hostbill.api_key.get_secret_value() == "key-abc"


async def test_slack_from_db(mem):
    await conn_upsert(mem, type_="slack", name="primary",
                      config={"default_instance": "prod", "channel_allowlist": ["#ops"]})
    await secret_set(mem, key="slack:primary:bot_token",
                     value="xoxb-test", crypto_key=_KEY)
    await secret_set(mem, key="slack:primary:signing_secret",
                     value="sig-secret", crypto_key=_KEY)

    s = _base_settings()
    result = await overlay_settings(s, mem, _KEY)

    assert result.slack is not None
    assert result.slack.bot_token.get_secret_value() == "xoxb-test"
    assert result.slack.signing_secret.get_secret_value() == "sig-secret"
    assert result.slack.default_instance == "prod"
    assert result.slack.channel_allowlist == ["#ops"]


async def test_oauth_google_from_db(mem):
    await conn_upsert(mem, type_="oauth_google", name="primary",
                      config={"client_id": "client-123",
                              "allowed_email_domain": "example.com",
                              "default_role": "operator"})
    await secret_set(mem, key="oauth_google:primary:client_secret",
                     value="client-secret-abc", crypto_key=_KEY)

    s = _base_settings()
    result = await overlay_settings(s, mem, _KEY)

    assert result.oauth_google is not None
    assert result.oauth_google.client_id == "client-123"
    assert result.oauth_google.client_secret.get_secret_value() == "client-secret-abc"
    assert result.oauth_google.allowed_email_domain == "example.com"
    assert result.oauth_google.default_role == "operator"


async def test_anthropic_key_from_db(mem):
    await secret_set(mem, key="anthropic:primary:api_key",
                     value="db-anthropic-key", crypto_key=_KEY)

    s = _base_settings()
    result = await overlay_settings(s, mem, _KEY)

    assert result.anthropic_api_key.get_secret_value() == "db-anthropic-key"


async def test_zabbix_ui_signing_key_from_db(mem):
    await secret_set(mem, key="zabbix_ui:primary:signing_key",
                     value="my-signing-key", crypto_key=_KEY)

    s = _base_settings()
    assert s.zabbix_ui is None
    result = await overlay_settings(s, mem, _KEY)

    assert result.zabbix_ui is not None
    assert result.zabbix_ui.signing_key.get_secret_value() == "my-signing-key"
