from unittest.mock import AsyncMock, MagicMock

from zabbix_ai.services.script_bootstrap import (
    DIAG_DEFINITIONS,
    DiagDef,
    ensure_diag_scripts,
)


def _make_client(existing: list[dict] | None = None,
                 next_ids: list[str] | None = None,
                 legacy: list[dict] | None = None):
    """Mock client.

    `legacy` is the response to the cleanup `script.get` call; `existing`
    is the response to the wanted-names lookup; `next_ids` are returned
    in order from successive `script.create` calls.
    """
    c = MagicMock()
    next_ids = list(next_ids or [])
    legacy = list(legacy or [])
    existing = list(existing or [])
    state = {"get_calls": 0}

    async def _call(method: str, params=None):
        if method == "script.get":
            state["get_calls"] += 1
            # 1st script.get is the legacy cleanup search
            if state["get_calls"] == 1:
                return list(legacy)
            return list(existing)
        if method == "script.create":
            sid = next_ids.pop(0)
            return {"scriptids": [sid]}
        if method in ("script.delete", "script.update"):
            return {}
        raise AssertionError(f"unexpected call {method}")

    c.call = AsyncMock(side_effect=_call)
    return c


def _expected_script_count() -> int:
    return sum(len(d.supported_os) for d in DIAG_DEFINITIONS)


async def test_creates_one_script_per_supported_os():
    n = _expected_script_count()
    client = _make_client(existing=[], next_ids=[str(900 + i) for i in range(n)])
    index = await ensure_diag_scripts(client)
    for d in DIAG_DEFINITIONS:
        for os_kind in d.supported_os:
            assert index.scriptid(d.name, os_kind) is not None


async def test_skips_existing_creates_missing():
    existing = [{"scriptid": "5", "name": "diag.df",
                 "menu_path": "zabbix-AI/Linux"}]
    n_to_create = _expected_script_count() - 1
    client = _make_client(existing=existing,
                          next_ids=[str(800 + i) for i in range(n_to_create)])
    index = await ensure_diag_scripts(client)
    assert index.scriptid("diag.df", "linux") == "5"


async def test_existing_scripts_updated_with_current_command():
    existing = [{"scriptid": "5", "name": "diag.df",
                 "menu_path": "zabbix-AI/Linux"}]
    only = [DiagDef("diag.df", "x", linux="df --new-flag")]
    client = _make_client(existing=existing, next_ids=[])
    await ensure_diag_scripts(client, defs=only)
    update_calls = [c for c in client.call.await_args_list
                    if c.args[0] == "script.update"]
    assert len(update_calls) == 1
    p = update_calls[0].args[1]
    assert p["scriptid"] == "5"
    assert p["command"] == "df --new-flag"


async def test_create_params_include_menu_path_and_30s_timeout():
    client = _make_client(existing=[], next_ids=["7"])
    only = [DiagDef("diag.foo", "x", linux="echo hi")]
    await ensure_diag_scripts(client, defs=only)
    create_calls = [c for c in client.call.await_args_list
                    if c.args[0] == "script.create"]
    assert len(create_calls) == 1
    params = create_calls[0].args[1]
    assert params["name"] == "diag.foo"
    assert params["menu_path"] == "zabbix-AI/Linux"
    assert params["timeout"] == "30s"


async def test_no_script_created_when_os_not_supported():
    client = _make_client(existing=[], next_ids=["10"])
    only = [DiagDef("diag.linux_only", "L only", linux="echo l")]
    await ensure_diag_scripts(client, defs=only)
    create_calls = [c for c in client.call.await_args_list
                    if c.args[0] == "script.create"]
    assert len(create_calls) == 1
    assert create_calls[0].args[1]["menu_path"] == "zabbix-AI/Linux"


async def test_legacy_scripts_deleted():
    legacy = [{"scriptid": "100", "name": "rca-ai.diag.df.linux"},
              {"scriptid": "101", "name": "rca-ai.diag.free.linux"}]
    only = [DiagDef("diag.linux_only", "L only", linux="echo l")]
    client = _make_client(existing=[], next_ids=["10"], legacy=legacy)
    await ensure_diag_scripts(client, defs=only)
    delete_calls = [c for c in client.call.await_args_list
                    if c.args[0] == "script.delete"]
    assert len(delete_calls) == 1
    assert delete_calls[0].args[1] == ["100", "101"]


async def test_parameterised_create_includes_manualinput():
    client = _make_client(existing=[], next_ids=["7"])
    only = [DiagDef("diag.foo", "x",
                     linux="echo {MANUALINPUT}",
                     manualinput=True,
                     manualinput_prompt="value",
                     manualinput_validator=r"^[a-z]+$",
                     manualinput_default_value="hi",
                     manualinput_arg_name="value")]
    await ensure_diag_scripts(client, defs=only)
    create_calls = [c for c in client.call.await_args_list
                    if c.args[0] == "script.create"]
    assert len(create_calls) == 1
    params = create_calls[0].args[1]
    assert params["manualinput"] == "1"
    assert params["manualinput_validator"] == r"^[a-z]+$"


async def test_snapshot_definition_present():
    snap = next((d for d in DIAG_DEFINITIONS if d.name == "diag.snapshot"), None)
    assert snap is not None
    assert snap.linux is not None
    assert snap.windows is not None
    assert "uptime" in snap.linux
    # Windows snapshot is base64-encoded for cmd.exe safety; verify the
    # encoded wrapper is present.
    assert "powershell" in snap.windows
    assert "EncodedCommand" in snap.windows


# ---------------- v1.4 tools: network / cert_expiry / smart ----------------

def _diag(name: str) -> DiagDef | None:
    return next((d for d in DIAG_DEFINITIONS if d.name == name), None)


def test_v14_diag_network_defined_for_both_os():
    d = _diag("diag.network")
    assert d is not None
    assert d.linux is not None and d.windows is not None
    assert "ip route" in d.linux
    # Windows variant must be base64-encoded PowerShell.
    assert "powershell" in d.windows
    assert "EncodedCommand" in d.windows


def test_v14_diag_smart_defined_for_both_os():
    d = _diag("diag.smart")
    assert d is not None
    assert d.linux is not None and d.windows is not None
    assert "smartctl" in d.linux
    assert "powershell" in d.windows
    assert "EncodedCommand" in d.windows


def test_v14_diag_cert_expiry_defined_with_manualinput():
    d = _diag("diag.cert_expiry")
    assert d is not None
    assert d.linux is not None and d.windows is not None
    assert d.manualinput is True
    assert d.manualinput_arg_name == "endpoints"
    # Validator regex must enforce comma-separated host:port (max 10).
    assert d.manualinput_validator
    import re as _re
    pat = _re.compile(d.manualinput_validator)
    assert pat.fullmatch("mail.example.com:993")
    assert pat.fullmatch("a:1,b:2,c:3")
    assert not pat.fullmatch("mail.example.com:993;rm")
    assert not pat.fullmatch("nohost")
    eleven = ",".join(f"h{i}:1" for i in range(11))
    assert not pat.fullmatch(eleven)
    # MANUALINPUT placeholder must appear in both bodies for Zabbix to
    # substitute the user-supplied value at execution time.
    assert "{MANUALINPUT}" in d.linux
    assert "{MANUALINPUT}" in d.windows


async def test_v14_tools_create_scripts_for_both_os():
    """The three new tools each register one Linux + one Windows script."""
    n = _expected_script_count()
    client = _make_client(existing=[], next_ids=[str(900 + i) for i in range(n)])
    index = await ensure_diag_scripts(client)
    for name in ("diag.network", "diag.cert_expiry", "diag.smart"):
        assert index.scriptid(name, "linux") is not None
        assert index.scriptid(name, "windows") is not None


def test_diag_disk_usage_defined_for_both_os():
    d = _diag("diag.disk_usage")
    assert d is not None
    assert d.linux is not None and d.windows is not None
    # Linux body must invoke du with the depth + sort pipe the AllowKey expects
    assert "du -hxd 3" in d.linux
    assert "sort -hr" in d.linux
    # Windows variant must be base64-encoded PowerShell with disk-summary cues
    assert "powershell" in d.windows
    assert "EncodedCommand" in d.windows


def test_diag_windows_winsxs_defined_windows_only():
    d = _diag("diag.windows_winsxs")
    assert d is not None
    # No Linux variant — it's a Windows-only diag.
    assert d.linux is None
    assert d.windows is not None
    # Windows variant must be base64-encoded PowerShell
    assert "powershell" in d.windows
    assert "EncodedCommand" in d.windows
    # Should be listed as Windows-supported only
    assert d.supported_os == ["windows"]


async def test_v14_cert_expiry_create_includes_manualinput_validator():
    """cert_expiry's bootstrap params must include the validator regex."""
    d = _diag("diag.cert_expiry")
    assert d is not None
    client = _make_client(existing=[], next_ids=["1001", "1002"])
    await ensure_diag_scripts(client, defs=[d])
    create_calls = [c for c in client.call.await_args_list
                    if c.args[0] == "script.create"]
    # one Linux + one Windows
    assert len(create_calls) == 2
    for c in create_calls:
        params = c.args[1]
        assert params["manualinput"] == "1"
        assert "host:port" in params["manualinput_prompt"]
        assert params["manualinput_validator"] == d.manualinput_validator
