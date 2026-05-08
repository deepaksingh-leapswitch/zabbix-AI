from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.tools import dispatch
from zabbix_ai.tools.zabbix import register_tools


@pytest.fixture
def fake_client():
    c = MagicMock()
    c.get_problem = AsyncMock(return_value={"eventid": "42", "name": "disk full",
                                             "severity": "4",
                                             "hosts": [{"hostid": "7", "host": "web-1"}],
                                             "tags": []})
    c.get_open_problems = AsyncMock(return_value=[])
    c.get_host = AsyncMock(return_value={"hostid": "7", "host": "web-1",
                                          "groups": [{"groupid": "1", "name": "WebServers"}]})
    c.get_history = AsyncMock(return_value={"vfs.fs.size[/,pused]": [{"clock": 1, "value": "92"}]})
    return c

@pytest.fixture
def context(fake_client):
    return {"clients": {"monitoring": fake_client}}

async def test_get_problem_dispatch(context):
    register_tools()
    result = await dispatch("zabbix.get_problem",
                            {"eventid": 42, "instance": "monitoring"},
                            context=context)
    assert result["eventid"] == "42"

async def test_get_problem_unknown_instance_raises(context):
    register_tools()
    with pytest.raises(ValueError, match="unknown instance"):
        await dispatch("zabbix.get_problem",
                       {"eventid": 42, "instance": "nope"}, context=context)

async def test_get_host(context):
    register_tools()
    r = await dispatch("zabbix.get_host",
                       {"hostid": 7, "instance": "monitoring"}, context=context)
    assert r["host"] == "web-1"

async def test_get_history(context):
    register_tools()
    r = await dispatch("zabbix.get_history",
                       {"hostid": 7, "instance": "monitoring",
                        "keys": ["vfs.fs.size[/,pused]"], "range_seconds": 3600},
                       context=context)
    assert "vfs.fs.size[/,pused]" in r
