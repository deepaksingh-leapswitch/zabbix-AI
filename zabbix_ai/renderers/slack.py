from __future__ import annotations

from typing import Any

from zabbix_ai.orchestrator import InvestigationResult

_MAX_BLOCK_TEXT = 2900  # Slack hard cap is 3000; leave a margin

def _truncate(s: str, limit: int = _MAX_BLOCK_TEXT) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"

def render_placeholder(*, question: str = "") -> list[dict[str, Any]]:
    text = ":mag: *Investigating…*"
    if question:
        text += f"\n> {_truncate(question, 200)}"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

def render_blocks(result: InvestigationResult) -> list[dict[str, Any]]:
    summary = _truncate(result.summary or "_(no summary produced)_")
    secs = (result.duration_ms or 0) / 1000.0
    metadata = (
        f"Investigation #{result.investigation_id} · "
        f"{result.tool_calls} tool calls · "
        f"{result.tokens_in}+{result.tokens_out} tokens · "
        f"{result.duration_ms} ms"
        if secs < 10 else
        f"Investigation #{result.investigation_id} · "
        f"{result.tool_calls} tool calls · "
        f"{result.tokens_in}+{result.tokens_out} tokens · "
        f"{secs:.1f} s"
    )
    return [
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f":robot_face: *Investigation #{result.investigation_id}*"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": summary}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": metadata}]},
    ]
