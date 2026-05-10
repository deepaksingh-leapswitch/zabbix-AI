"""Unit tests for zabbix_ai.admin.connections_store."""
from __future__ import annotations

from pathlib import Path

import pytest

from zabbix_ai.admin.connections_store import (
    conn_delete,
    conn_get,
    conn_list,
    conn_upsert,
    secret_delete,
    secret_get,
    secret_set,
)
from zabbix_ai.admin.crypto import derive_key
from zabbix_ai.memory import Memory

_MIGRATIONS = Path("migrations")
_KEY = derive_key("test-master-key")


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    await m.run_migrations(_MIGRATIONS)
    yield m
    await m.close()


# ─── secret round-trip ───────────────────────────────────────────────────────

async def test_secret_set_and_get(mem):
    await secret_set(mem, key="mykey", value="myvalue", crypto_key=_KEY)
    result = await secret_get(mem, key="mykey", crypto_key=_KEY)
    assert result == "myvalue"


async def test_secret_get_missing_returns_none(mem):
    result = await secret_get(mem, key="no-such-key", crypto_key=_KEY)
    assert result is None


async def test_secret_overwrite(mem):
    await secret_set(mem, key="k", value="v1", crypto_key=_KEY)
    await secret_set(mem, key="k", value="v2", crypto_key=_KEY)
    assert await secret_get(mem, key="k", crypto_key=_KEY) == "v2"


async def test_secret_delete(mem):
    await secret_set(mem, key="del-me", value="x", crypto_key=_KEY)
    await secret_delete(mem, key="del-me")
    assert await secret_get(mem, key="del-me", crypto_key=_KEY) is None


async def test_secret_wrong_key_fails(mem):
    wrong_key = derive_key("wrong-master")
    await secret_set(mem, key="k", value="v", crypto_key=_KEY)
    from cryptography.exceptions import InvalidTag
    with pytest.raises(InvalidTag):
        await secret_get(mem, key="k", crypto_key=wrong_key)


# ─── connections CRUD ────────────────────────────────────────────────────────

async def test_conn_upsert_and_get(mem):
    await conn_upsert(mem, type_="zabbix", name="prod",
                      config={"url": "https://zabbix.example.com"},
                      updated_by="admin")
    row = await conn_get(mem, type_="zabbix", name="prod")
    assert row is not None
    assert row["type"] == "zabbix"
    assert row["name"] == "prod"
    assert row["config"]["url"] == "https://zabbix.example.com"
    assert row["enabled"] is True
    assert row["updated_by"] == "admin"


async def test_conn_get_missing_returns_none(mem):
    result = await conn_get(mem, type_="zabbix", name="nonexistent")
    assert result is None


async def test_conn_list_empty(mem):
    rows = await conn_list(mem)
    assert rows == []


async def test_conn_list_type_filter(mem):
    await conn_upsert(mem, type_="zabbix", name="a", config={"url": "http://a"})
    await conn_upsert(mem, type_="zabbix", name="b", config={"url": "http://b"})
    await conn_upsert(mem, type_="slack", name="primary", config={})
    zabbix = await conn_list(mem, type_filter="zabbix")
    assert len(zabbix) == 2
    all_rows = await conn_list(mem)
    assert len(all_rows) == 3


async def test_conn_upsert_update(mem):
    await conn_upsert(mem, type_="hostbill", name="primary",
                      config={"api_url": "http://old"})
    await conn_upsert(mem, type_="hostbill", name="primary",
                      config={"api_url": "http://new"}, enabled=False)
    row = await conn_get(mem, type_="hostbill", name="primary")
    assert row["config"]["api_url"] == "http://new"
    assert row["enabled"] is False


async def test_conn_delete(mem):
    await conn_upsert(mem, type_="slack", name="primary", config={})
    await conn_delete(mem, type_="slack", name="primary")
    assert await conn_get(mem, type_="slack", name="primary") is None
