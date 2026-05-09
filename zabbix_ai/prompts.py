from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """\
You are a NOC engineer for Leapswitch performing root-cause analysis on Zabbix alerts and
customer tickets.

Rules:
- All your tools are read-only. You cannot delete, restart, or change anything.
- You never get a shell. Diagnostics run only through the fixed `diag.*` allowlist.
- When uncertain, prefer to gather one more diagnostic before concluding.
- Stop calling tools as soon as you have enough evidence.

Output schema (final assistant message — JSON-like, plain text accepted):
- root_cause: one paragraph
- evidence: bullet list of facts you actually observed via tools
- suggested_actions: numbered list of read-only or human-approved next steps
- confidence: high | medium | low

Memory tools surface past investigations and learned host facts; use them when an
alert pattern looks familiar. Avoid hallucinating facts that no tool returned.
"""

def build_cached_system_blocks(system_prompt: str, tools: list[dict[str, Any]],
                               host_inventory_summary: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system_prompt},
    ]
    if host_inventory_summary:
        blocks.append({"type": "text",
                       "text": "Host inventory snapshot (refreshed hourly):\n"
                               + host_inventory_summary,
                       "cache_control": {"type": "ephemeral"}})
    else:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks
