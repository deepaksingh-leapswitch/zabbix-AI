from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.tools import dispatch
from zabbix_ai.tools.lookup import register_tools


@pytest.fixture
def context():
    c = MagicMock()
    c.call = AsyncMock(side_effect=[
        [{"hostid": "7", "host": "web-1"}],
        [{"hostid": "9", "host": "db-1"}],
    ])
    return {"clients": {"monitoring": c}}

async def test_host_by_domain(context):
    register_tools()
    r = await dispatch("lookup.host_by_domain",
                       {"domain": "shop.example.com", "instance": "monitoring"},
                       context=context)
    assert r["host"] == "web-1"

async def test_host_by_ip(context):
    register_tools()
    r = await dispatch("lookup.host_by_ip",
                       {"ip": "10.0.0.5", "instance": "monitoring"},
                       context=context)
    assert r["host"] == "web-1"
