from unittest.mock import AsyncMock, MagicMock, patch

from zabbix_ai.adapters.cli import _run, build_parser


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)

def _resp(stop_reason, blocks, in_t=10, out_t=5):
    return MagicMock(stop_reason=stop_reason, content=blocks,
                     usage=MagicMock(input_tokens=in_t, output_tokens=out_t,
                                     cache_creation_input_tokens=0,
                                     cache_read_input_tokens=0))

async def test_cli_investigate_end_to_end(tmp_path, monkeypatch, capsys):
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

    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create = AsyncMock(side_effect=[
            _resp("end_turn", [_Block(type="text",
                                      text="root_cause: tested\nconfidence: high")]),
        ])
        args = build_parser().parse_args([
            "--config", str(cfg),
            "investigate", "--instance", "monitoring", "--question", "is it ok?",
        ])
        rc = await _run(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Investigation #" in out
        assert "root_cause: tested" in out
