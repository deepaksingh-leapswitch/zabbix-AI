from unittest.mock import AsyncMock, MagicMock, patch

from zabbix_ai.clients.claude import ClaudeClient
from zabbix_ai.prompts import SYSTEM_PROMPT, build_cached_system_blocks


def test_system_prompt_includes_safety_rules():
    assert "read-only" in SYSTEM_PROMPT.lower()
    assert "no shell" in SYSTEM_PROMPT.lower() or "never get a shell" in SYSTEM_PROMPT.lower()

def test_cached_blocks_have_cache_control():
    tools = [{"name": "x", "description": "x", "input_schema": {"type": "object"}}]
    inv = "host inventory snapshot"
    blocks = build_cached_system_blocks(SYSTEM_PROMPT, tools, inv)
    assert blocks[0]["type"] == "text"
    assert any(b.get("cache_control") == {"type": "ephemeral"} for b in blocks)

async def test_claude_client_calls_messages_create_with_cache():
    fake_resp = MagicMock(stop_reason="end_turn", content=[],
                          usage=MagicMock(input_tokens=10, output_tokens=5,
                                          cache_creation_input_tokens=0,
                                          cache_read_input_tokens=0))
    with patch("zabbix_ai.clients.claude.AsyncAnthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create = AsyncMock(return_value=fake_resp)
        c = ClaudeClient(api_key="sk-ant-test")
        resp = await c.create(model="claude-sonnet-4-6",
                              system=[{"type": "text", "text": "sys",
                                       "cache_control": {"type": "ephemeral"}}],
                              tools=[], messages=[{"role": "user", "content": "hi"}],
                              max_tokens=100)
        assert resp.stop_reason == "end_turn"
        mock_anthropic.return_value.messages.create.assert_awaited_once()
