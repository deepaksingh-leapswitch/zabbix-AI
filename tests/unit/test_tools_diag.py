from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.services.script_bootstrap import DIAG_DEFINITIONS, ScriptIndex
from zabbix_ai.tools import dispatch
from zabbix_ai.tools.diag import ALLOWED_DIAG_KEYS, register_tools


@pytest.fixture
def fake_client():
    c = MagicMock()
    c.call = AsyncMock(return_value={"response": "success",
                                      "value": "Filesystem  Size  Use%  Mounted"})
    return c


@pytest.fixture
def script_index():
    idx = ScriptIndex()
    for d in DIAG_DEFINITIONS:
        idx.by_name[d.name] = "100"
        idx.defs_by_name[d.name] = d
    return idx


@pytest.fixture
def context(fake_client, script_index):
    return {
        "clients": {"monitoring": fake_client},
        "scripts": {"monitoring": script_index},
    }


async def test_diag_df_runs(context):
    register_tools()
    out = await dispatch("diag.df", {"hostid": 7, "instance": "monitoring"},
                        context=context)
    assert "Filesystem" in out


async def test_diag_df_calls_script_execute(context, fake_client):
    register_tools()
    await dispatch("diag.df", {"hostid": 7, "instance": "monitoring"},
                   context=context)
    method, params = fake_client.call.await_args.args
    assert method == "script.execute"
    assert params["scriptid"] == "100"
    assert params["hostid"] == "7"
    assert "manualinput" not in params


async def test_diag_systemctl_status_passes_manualinput(context, fake_client):
    fake_client.call = AsyncMock(return_value={"response": "success",
                                                "value": "active (running)"})
    context["clients"]["monitoring"] = fake_client
    register_tools()
    out = await dispatch("diag.systemctl_status",
                        {"hostid": 7, "instance": "monitoring", "unit": "mysql"},
                        context=context)
    assert "active" in out
    method, params = fake_client.call.await_args.args
    assert method == "script.execute"
    assert params["manualinput"] == "mysql"


async def test_diag_journal_tail_validates_lines(context):
    register_tools()
    with pytest.raises(ValueError, match="lines"):
        await dispatch("diag.journal_tail",
                       {"hostid": 7, "instance": "monitoring", "lines": 99999},
                       context=context)


async def test_diag_systemctl_rejects_invalid_unit(context):
    register_tools()
    with pytest.raises(ValueError, match="invalid unit"):
        await dispatch("diag.systemctl_status",
                       {"hostid": 7, "instance": "monitoring",
                        "unit": "ev;rm -rf"},
                       context=context)


async def test_diag_unknown_command_rejected(context):
    register_tools()
    with pytest.raises(KeyError):
        await dispatch("diag.rm_rf", {"hostid": 7, "instance": "monitoring"},
                       context=context)


async def test_diag_unbootstrapped_instance_rejected(fake_client, script_index):
    register_tools()
    # context has no scripts entry for this instance
    ctx = {"clients": {"monitoring": fake_client}, "scripts": {}}
    with pytest.raises(ValueError, match="not bootstrapped"):
        await dispatch("diag.df", {"hostid": 7, "instance": "monitoring"},
                       context=ctx)


async def test_diag_failed_response_raises(context, fake_client):
    fake_client.call = AsyncMock(return_value={"response": "failed",
                                                "value": "agent unreachable"})
    context["clients"]["monitoring"] = fake_client
    register_tools()
    with pytest.raises(ValueError, match="execution failed"):
        await dispatch("diag.df", {"hostid": 7, "instance": "monitoring"},
                       context=context)


def test_allowlist_complete():
    assert "diag.df" in ALLOWED_DIAG_KEYS
    assert "diag.mysql_processlist" in ALLOWED_DIAG_KEYS
    assert "system.run" not in ALLOWED_DIAG_KEYS
