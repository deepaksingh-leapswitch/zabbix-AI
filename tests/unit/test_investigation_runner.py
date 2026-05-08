# tests/unit/test_investigation_runner.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zabbix_ai.config import load_settings
from zabbix_ai.services.investigation_runner import InvestigationRunner


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)

def _resp(stop_reason, blocks, in_t=10, out_t=5):
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
    return load_settings(cfg)

async def test_runner_executes_investigation(settings):
    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create = AsyncMock(side_effect=[
            _resp("end_turn", [_Block(type="text", text="root_cause: ok")]),
        ])
        async with InvestigationRunner(settings) as runner:
            from zabbix_ai.orchestrator import InvestigationContext
            ctx = InvestigationContext(source="test", instance="monitoring",
                                        question="?")
            result = await runner.investigate(ctx)
        assert "root_cause: ok" in result.summary
