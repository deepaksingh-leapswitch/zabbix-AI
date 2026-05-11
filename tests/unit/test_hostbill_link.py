"""Unit tests for services/hostbill_link.py — the Zabbix→HostBill auto-linker."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.memory import Memory
from zabbix_ai.services import hostbill_link as hb


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    migrations = Path(__file__).resolve().parent.parent.parent / "migrations"
    await m.run_migrations(migrations)
    yield m
    await m.close()


def _zabbix(host: dict):
    """Build a mock ZabbixClient that returns the given host from get_host."""
    z = MagicMock()
    z.get_host = AsyncMock(return_value=host)
    return z


def _hostbill(*, services_by_ip=None, services_by_domain=None,
              service_by_id=None, client_by_id=None):
    """Build a mock HostBillClient with the requested behaviours."""
    services_by_ip = services_by_ip or {}
    services_by_domain = services_by_domain or {}
    service_by_id = service_by_id or {}
    client_by_id = client_by_id or {}

    async def _search_services(*, ip=None, domain=None):
        if ip is not None and ip in services_by_ip:
            return services_by_ip[ip]
        if domain is not None and domain.lower() in services_by_domain:
            return services_by_domain[domain.lower()]
        return []

    async def _get_service(sid):
        return service_by_id.get(int(sid))

    async def _get_client(cid):
        return client_by_id.get(int(cid))

    h = MagicMock()
    h.search_services = AsyncMock(side_effect=_search_services)
    h.get_service = AsyncMock(side_effect=_get_service)
    h.get_client = AsyncMock(side_effect=_get_client)
    return h


# ── tag matcher ──────────────────────────────────────────────────────────────

async def test_link_by_tag_high_confidence(mem):
    host = {
        "host": "plesk1",
        "tags": [{"tag": "hostbill_service_id", "value": "42"}],
        "interfaces": [{"ip": "1.2.3.4"}],
    }
    hbc = _hostbill(
        service_by_id={42: {
            "id": "42", "userid": "7",
            "domain": "plesk1.example.com",
        }},
        client_by_id={7: {"firstname": "Acme", "lastname": "Pvt"}},
    )
    link = await hb.link_zabbix_host(
        memory=mem, hostbill_client=hbc, zabbix_client=_zabbix(host),
        zabbix_instance="monitoring", zabbix_hostid=17977,
    )
    assert link.linked_by == "auto:tag"
    assert link.confidence == "high"
    assert link.hostbill_service_id == 42


# ── IP matcher ───────────────────────────────────────────────────────────────

async def test_link_by_ip_single_hit_high(mem):
    host = {
        "host": "plesk1",
        "tags": [],
        "interfaces": [{"ip": "45.64.104.120"}],
    }
    hbc = _hostbill(services_by_ip={
        "45.64.104.120": [{
            "id": "88", "userid": "7",
            "domain": "plesk1.example.com",
            "firstname": "Acme", "lastname": "Pvt",
        }],
    })
    link = await hb.link_zabbix_host(
        memory=mem, hostbill_client=hbc, zabbix_client=_zabbix(host),
        zabbix_instance="monitoring", zabbix_hostid=17977,
    )
    assert link.linked_by == "auto:ip"
    assert link.confidence == "high"
    assert link.hostbill_service_id == 88


async def test_link_by_ip_multi_hit_low(mem):
    host = {
        "host": "plesk1",
        "tags": [],
        "interfaces": [{"ip": "45.64.104.120"}],
    }
    hbc = _hostbill(services_by_ip={
        "45.64.104.120": [
            {"id": "88", "userid": "7", "domain": "a.example.com"},
            {"id": "99", "userid": "8", "domain": "b.example.com"},
        ],
    })
    link = await hb.link_zabbix_host(
        memory=mem, hostbill_client=hbc, zabbix_client=_zabbix(host),
        zabbix_instance="monitoring", zabbix_hostid=17977,
    )
    assert link.linked_by == "auto:ip"
    assert link.confidence == "low"
    assert link.hostbill_service_id in (88, 99)


# ── hostname matcher ─────────────────────────────────────────────────────────

async def test_link_by_hostname(mem):
    host = {
        "host": "plesk1.example.com",
        "name": "Plesk1 India-Pune",
        "tags": [],
        "interfaces": [{"ip": "45.64.104.120"}],
    }
    hbc = _hostbill(
        services_by_ip={},
        services_by_domain={"plesk1.example.com": [{
            "id": "77", "userid": "7", "domain": "plesk1.example.com",
        }]},
    )
    link = await hb.link_zabbix_host(
        memory=mem, hostbill_client=hbc, zabbix_client=_zabbix(host),
        zabbix_instance="monitoring", zabbix_hostid=17977,
    )
    assert link.linked_by.startswith("auto:hostname")
    assert link.hostbill_service_id == 77


# ── no-match path ────────────────────────────────────────────────────────────

async def test_no_match_writes_unlinked(mem):
    host = {
        "host": "unknown-host",
        "tags": [],
        "interfaces": [{"ip": "10.10.10.10"}],
    }
    hbc = _hostbill()
    link = await hb.link_zabbix_host(
        memory=mem, hostbill_client=hbc, zabbix_client=_zabbix(host),
        zabbix_instance="monitoring", zabbix_hostid=99999,
    )
    assert link.linked_by == "unlinked"
    assert link.hostbill_service_id is None
    # A row was persisted so we don't retry every investigation.
    row = await mem.fetchone(
        "SELECT linked_by FROM host_hostbill_link "
        "WHERE zabbix_instance=? AND zabbix_hostid=?",
        ("monitoring", 99999),
    )
    assert row is not None
    assert row[0] == "unlinked"


# ── HostBill unreachable (None client) ───────────────────────────────────────

async def test_hostbill_client_none_degrades_gracefully(mem):
    host = {"host": "plesk1", "tags": [], "interfaces": [{"ip": "1.2.3.4"}]}
    link = await hb.link_zabbix_host(
        memory=mem, hostbill_client=None, zabbix_client=_zabbix(host),
        zabbix_instance="monitoring", zabbix_hostid=11,
    )
    assert link.linked_by == "unlinked"
    assert link.hostbill_service_id is None


# ── cache hit on second call ─────────────────────────────────────────────────

async def test_second_call_uses_cache(mem):
    host = {"host": "plesk1", "tags": [],
            "interfaces": [{"ip": "45.64.104.120"}]}
    hbc = _hostbill(services_by_ip={
        "45.64.104.120": [{"id": "88", "userid": "7", "domain": "a.example.com"}],
    })
    zclient = _zabbix(host)
    await hb.link_zabbix_host(
        memory=mem, hostbill_client=hbc, zabbix_client=zclient,
        zabbix_instance="monitoring", zabbix_hostid=17977,
    )
    # Reset call counts and call again — should not call get_host again.
    zclient.get_host.reset_mock()
    hbc.search_services.reset_mock()
    link2 = await hb.link_zabbix_host(
        memory=mem, hostbill_client=hbc, zabbix_client=zclient,
        zabbix_instance="monitoring", zabbix_hostid=17977,
    )
    assert link2.hostbill_service_id == 88
    assert zclient.get_host.call_count == 0
    assert hbc.search_services.call_count == 0
