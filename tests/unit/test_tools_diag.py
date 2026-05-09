from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.tools import dispatch
from zabbix_ai.tools.diag import ALLOWED_DIAG_KEYS, register_tools


@pytest.fixture
def fake_client():
    c = MagicMock()
    c.get_item = AsyncMock(return_value={"itemid": "100", "lastclock": "1000"})
    c.task_create_check_now = AsyncMock()
    c.wait_for_fresh_value = AsyncMock(return_value="Filesystem  Size  Use%  Mounted")
    return c

@pytest.fixture
def context(fake_client):
    return {"clients": {"monitoring": fake_client}}

async def test_diag_df_runs(context):
    register_tools()
    out = await dispatch("diag.df", {"hostid": 7, "instance": "monitoring"},
                        context=context)
    assert "Filesystem" in out

async def test_diag_systemctl_status_arg(context):
    register_tools()
    out = await dispatch("diag.systemctl_status",
                        {"hostid": 7, "instance": "monitoring", "unit": "mysql"},
                        context=context)
    assert out

async def test_diag_unknown_command_rejected(context):
    register_tools()
    with pytest.raises(KeyError):
        await dispatch("diag.rm_rf", {"hostid": 7, "instance": "monitoring"},
                       context=context)

def test_allowlist_complete():
    assert "diag.df" in ALLOWED_DIAG_KEYS
    assert "diag.mysql_processlist" in ALLOWED_DIAG_KEYS
    assert "system.run" not in ALLOWED_DIAG_KEYS
