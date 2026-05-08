# tests/unit/test_slack_renderer.py
from zabbix_ai.orchestrator import InvestigationResult
from zabbix_ai.renderers.slack import render_blocks, render_placeholder


def test_placeholder_blocks():
    blocks = render_placeholder(question="why?")
    assert blocks[0]["type"] == "section"
    assert "Investigating" in blocks[0]["text"]["text"]

def test_render_blocks_includes_summary_and_metadata():
    r = InvestigationResult(
        investigation_id=42, summary="root_cause: disk full\nconfidence: high",
        tool_calls=3, tokens_in=1200, tokens_out=400, duration_ms=4500,
    )
    blocks = render_blocks(r)
    rendered = "\n".join(b.get("text", {}).get("text", "") for b in blocks
                          if b.get("type") == "section")
    assert "disk full" in rendered
    assert "Investigation #42" in rendered
    # context block carries metadata
    ctx = [b for b in blocks if b["type"] == "context"]
    assert ctx
    ctx_text = ctx[0]["elements"][0]["text"]
    assert "3 tool calls" in ctx_text
    assert "4500 ms" in ctx_text or "4.5" in ctx_text

def test_render_blocks_truncates_very_long_summary():
    huge = "x" * 4000
    r = InvestigationResult(investigation_id=1, summary=huge,
                            tool_calls=0, tokens_in=0, tokens_out=0, duration_ms=0)
    blocks = render_blocks(r)
    body = blocks[1]["text"]["text"]
    assert len(body) <= 3000  # Slack block text limit
    assert body.endswith("…") or body.endswith("...")
