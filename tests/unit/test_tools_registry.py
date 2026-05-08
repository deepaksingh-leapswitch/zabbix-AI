import pytest
from zabbix_ai.tools import register, dispatch, ALLOWED_TOOLS, claude_tool_definitions

def test_register_adds_tool():
    @register("test.echo", description="echo back",
              schema={"type": "object", "properties": {"x": {"type": "string"}},
                      "required": ["x"]})
    async def echo(*, x: str) -> str:
        return x
    assert "test.echo" in ALLOWED_TOOLS

async def test_dispatch_calls_registered_tool():
    @register("test.add", description="add",
              schema={"type": "object",
                      "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                      "required": ["a", "b"]})
    async def add(*, a: int, b: int) -> int:
        return a + b
    result = await dispatch("test.add", {"a": 2, "b": 3}, context={})
    assert result == 5

async def test_dispatch_unknown_raises():
    with pytest.raises(KeyError, match="not allowed"):
        await dispatch("evil.delete_everything", {}, context={})

def test_claude_tool_definitions_returns_list():
    defs = claude_tool_definitions()
    assert isinstance(defs, list)
    assert all("name" in d and "input_schema" in d for d in defs)
