from __future__ import annotations

from zabbix_ai.orchestrator import InvestigationResult


def render(result: InvestigationResult) -> str:
    return (
        f"=== Investigation #{result.investigation_id} ===\n"
        f"Tool calls: {result.tool_calls}\n"
        f"Tokens: in={result.tokens_in} out={result.tokens_out}\n"
        f"Duration: {result.duration_ms} ms\n\n"
        f"{result.summary}\n"
    )
