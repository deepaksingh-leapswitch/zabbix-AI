import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zabbix_ai.config import load_settings
from zabbix_ai.memory import Memory, find_pattern
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.services.investigation_runner import InvestigationRunner


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


def _claude(stop_reason, blocks, in_t=10, out_t=5):
    return MagicMock(stop_reason=stop_reason, content=blocks,
                     usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
zabbix_instances:
  - name: monitoring
    url: https://zbx.test
    token_env: ZBX_TOK
sqlite_path: {tmp_path / 'state.db'}
default_model: m
summary_model: h
max_tool_calls: 4
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ZBX_TOK", "tok")
    return load_settings(cfg), tmp_path / "state.db"


async def test_runner_writeback_persists_to_sqlite(settings):
    s, db_path = settings
    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create = AsyncMock(side_effect=[
            _claude("end_turn", [_Block(type="text", text="ok")]),
            _claude("end_turn", [_Block(type="text",
                                         text=json.dumps({
                                             "root_cause_short": "disk full",
                                             "fix_short": "rotate logs",
                                             "host_facts": {"role": "web"}}))]),
        ])
        async with InvestigationRunner(s) as runner:
            ctx = InvestigationContext(
                source="cli", instance="monitoring",
                hostid=12345, hostname="web-01",
                problem_name="Disk space low on /var",
                hostgroup="Managed cPanel VPS",
            )
            result = await runner.investigate(ctx)
            assert result.pattern_signature

    # reopen DB and confirm rows
    m = Memory(str(db_path))
    await m.connect()
    pat = await find_pattern(m, signature=result.pattern_signature)
    assert pat is not None
    assert pat["typical_fix"] == "rotate logs"
    rows = await m.fetchall(
        "SELECT key, value FROM host_facts WHERE hostid=12345",
    )
    await m.close()
    assert ("role", "web") in rows
