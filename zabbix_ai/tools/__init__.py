from __future__ import annotations
from collections.abc import Awaitable, Callable
from typing import Any

ToolFunc = Callable[..., Awaitable[Any]]

ALLOWED_TOOLS: dict[str, ToolFunc] = {}
_TOOL_META: dict[str, dict[str, Any]] = {}

def register(name: str, *, description: str, schema: dict[str, Any]) -> Callable[[ToolFunc], ToolFunc]:
    def decorator(fn: ToolFunc) -> ToolFunc:
        ALLOWED_TOOLS[name] = fn
        _TOOL_META[name] = {"description": description, "input_schema": schema}
        return fn
    return decorator

async def dispatch(name: str, args: dict[str, Any], *, context: dict[str, Any]) -> Any:
    if name not in ALLOWED_TOOLS:
        raise KeyError(f"tool '{name}' not allowed")
    fn = ALLOWED_TOOLS[name]
    return await fn(**args, _ctx=context) if _accepts_ctx(fn) else await fn(**args)

def _accepts_ctx(fn: ToolFunc) -> bool:
    import inspect
    sig = inspect.signature(fn)
    return "_ctx" in sig.parameters

def claude_tool_definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": meta["description"], "input_schema": meta["input_schema"]}
        for name, meta in _TOOL_META.items()
    ]
