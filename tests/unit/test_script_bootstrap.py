from unittest.mock import AsyncMock, MagicMock

from zabbix_ai.services.script_bootstrap import (
    DIAG_DEFINITIONS,
    DiagDef,
    ensure_diag_scripts,
)


def _make_client(existing: list[dict] | None = None,
                 next_ids: list[str] | None = None):
    c = MagicMock()
    next_ids = list(next_ids or [])

    async def _call(method: str, params=None):
        if method == "script.get":
            return list(existing or [])
        if method == "script.create":
            sid = next_ids.pop(0)
            return {"scriptids": [sid]}
        raise AssertionError(f"unexpected call {method}")

    c.call = AsyncMock(side_effect=_call)
    return c


async def test_creates_all_when_none_exist():
    next_ids = [str(900 + i) for i in range(len(DIAG_DEFINITIONS))]
    client = _make_client(existing=[], next_ids=list(next_ids))
    index = await ensure_diag_scripts(client)
    assert {d.name for d in DIAG_DEFINITIONS} <= set(index.by_name)
    assert index.scriptid("diag.df") in next_ids


async def test_skips_existing_creates_missing():
    existing = [{"scriptid": "5", "name": "rca-ai.diag.df"}]
    client = _make_client(existing=existing,
                          next_ids=[str(800 + i)
                                     for i in range(len(DIAG_DEFINITIONS) - 1)])
    index = await ensure_diag_scripts(client)
    assert index.scriptid("diag.df") == "5"
    # call count: one script.get + (len(defs)-1) script.creates
    assert client.call.await_count == 1 + (len(DIAG_DEFINITIONS) - 1)


async def test_parameterised_create_includes_manualinput():
    client = _make_client(existing=[], next_ids=["7"])
    only = [DiagDef("diag.foo", "x", "echo {MANUALINPUT}",
                     manualinput=True,
                     manualinput_prompt="value",
                     manualinput_validator=r"^[a-z]+$",
                     manualinput_default_value="hi",
                     manualinput_arg_name="value")]
    await ensure_diag_scripts(client, defs=only)
    method, params = [c.args for c in client.call.await_args_list][1]
    assert method == "script.create"
    assert params["manualinput"] == "1"
    assert params["manualinput_validator"] == r"^[a-z]+$"
