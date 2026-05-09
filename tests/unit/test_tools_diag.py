from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.services.script_bootstrap import DIAG_DEFINITIONS, ScriptIndex
from zabbix_ai.tools import dispatch
from zabbix_ai.tools.diag import ALLOWED_DIAG_KEYS, _normalise_os, register_tools


@pytest.fixture
def fake_client():
    c = MagicMock()
    c.call = AsyncMock(return_value={"response": "success",
                                      "value": "Filesystem  Size  Use%  Mounted"})
    c.get_host = AsyncMock(return_value={
        "hostid": "7", "host": "linux-host",
        "inventory": {"os_short": "Linux"},
        "tags": [],
    })
    return c


@pytest.fixture
def script_index():
    idx = ScriptIndex()
    for d in DIAG_DEFINITIONS:
        idx.defs_by_name[d.name] = d
        for os_kind in d.supported_os:
            idx.by_name.setdefault(d.name, {})[os_kind] = f"sid-{d.name}-{os_kind}"
    return idx


@pytest.fixture
def context(fake_client, script_index):
    return {
        "clients": {"monitoring": fake_client},
        "scripts": {"monitoring": script_index},
    }


# ---------------- OS normalisation ----------------

def test_normalise_os_windows():
    assert _normalise_os("Windows") == "windows"
    assert _normalise_os("Microsoft Windows Server 2022") == "windows"


def test_normalise_os_linux():
    assert _normalise_os("Linux") == "linux"
    assert _normalise_os("CentOS Linux 7") == "linux"
    assert _normalise_os("Ubuntu") == "linux"
    assert _normalise_os("AlmaLinux 9") == "linux"


def test_normalise_os_default_when_unknown():
    assert _normalise_os(None) == "linux"
    assert _normalise_os("") == "linux"
    assert _normalise_os("FreeBSD") == "linux"   # default fallback


# ---------------- diag dispatch ----------------

async def test_diag_df_runs_on_linux_host(context, fake_client):
    register_tools()
    out = await dispatch("diag.df", {"hostid": 7, "instance": "monitoring"},
                        context=context)
    assert "Filesystem" in out
    method, params = fake_client.call.await_args.args
    assert method == "script.execute"
    assert params["scriptid"] == "sid-diag.df-linux"
    assert params["hostid"] == "7"


async def test_diag_picks_windows_scriptid_for_windows_host(
        context, fake_client, script_index,
):
    fake_client.get_host = AsyncMock(return_value={
        "hostid": "8", "host": "win-host",
        "inventory": {"os_short": "Windows"},
        "tags": [],
    })
    register_tools()
    await dispatch("diag.df", {"hostid": 8, "instance": "monitoring"},
                   context=context)
    _method, params = fake_client.call.await_args.args
    assert params["scriptid"] == "sid-diag.df-windows"


async def test_diag_uses_host_tag_when_inventory_missing(
        context, fake_client,
):
    fake_client.get_host = AsyncMock(return_value={
        "hostid": "9", "host": "tagged-host",
        "inventory": {},
        "tags": [{"tag": "os", "value": "windows"}],
    })
    register_tools()
    await dispatch("diag.df", {"hostid": 9, "instance": "monitoring"},
                   context=context)
    _method, params = fake_client.call.await_args.args
    assert "windows" in params["scriptid"]


async def test_os_detection_cached_within_investigation(context, fake_client):
    register_tools()
    await dispatch("diag.df", {"hostid": 7, "instance": "monitoring"},
                   context=context)
    await dispatch("diag.free", {"hostid": 7, "instance": "monitoring"},
                   context=context)
    await dispatch("diag.uptime", {"hostid": 7, "instance": "monitoring"},
                   context=context)
    # get_host should only be called once for hostid=7
    assert fake_client.get_host.await_count == 1


async def test_diag_unsupported_on_windows_raises(context, fake_client):
    fake_client.get_host = AsyncMock(return_value={
        "hostid": "11",
        "inventory": {"os_short": "Windows"},
        "tags": [],
    })
    register_tools()
    # mysql_status has no Windows variant
    with pytest.raises(ValueError, match="not available for windows"):
        await dispatch("diag.mysql_status",
                       {"hostid": 11, "instance": "monitoring"},
                       context=context)


async def test_snapshot_dispatches(context, fake_client):
    fake_client.call = AsyncMock(return_value={
        "response": "success",
        "value": "=== uptime ===\n... lots of output ...",
    })
    context["clients"]["monitoring"] = fake_client
    register_tools()
    out = await dispatch("diag.snapshot",
                         {"hostid": 7, "instance": "monitoring"},
                         context=context)
    assert "uptime" in out
    _method, params = fake_client.call.await_args.args
    assert "snapshot" in params["scriptid"]


async def test_diag_systemctl_status_passes_manualinput(context, fake_client):
    fake_client.call = AsyncMock(return_value={"response": "success",
                                                "value": "active (running)"})
    context["clients"]["monitoring"] = fake_client
    register_tools()
    out = await dispatch(
        "diag.systemctl_status",
        {"hostid": 7, "instance": "monitoring", "unit": "mysql"},
        context=context,
    )
    assert "active" in out
    _method, params = fake_client.call.await_args.args
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
                        "unit": "ev;rm -rf /"},
                       context=context)


async def test_diag_unknown_command_rejected(context):
    register_tools()
    with pytest.raises(KeyError):
        await dispatch("diag.rm_rf", {"hostid": 7, "instance": "monitoring"},
                       context=context)


async def test_diag_unbootstrapped_instance_rejected(fake_client):
    register_tools()
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
    assert "diag.snapshot" in ALLOWED_DIAG_KEYS
    assert "diag.mysql_processlist" in ALLOWED_DIAG_KEYS
    assert "system.run" not in ALLOWED_DIAG_KEYS
