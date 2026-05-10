"""Unit tests for zabbix_ai.services.host_briefing."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

from zabbix_ai.services.host_briefing import build_host_briefing

# ---------------------------------------------------------------------------
# Helpers — build a mock ZabbixClient
# ---------------------------------------------------------------------------

def _make_client(
    *,
    host: dict | None = None,
    problems: list | None = None,
    events: list | None = None,
    items: list | None = None,
    history: list | None = None,
) -> MagicMock:
    """Return a MagicMock ZabbixClient with configurable canned responses.

    `call` is mapped per method name so individual pieces can be tuned.
    """
    now = int(time.time())

    _host = host or {
        "hostid": "42",
        "host": "srv-test-01",
        "name": "srv-test-01",
        "status": "0",
        "hostgroups": [{"name": "Linux servers"}, {"name": "Prod"}],
        "tags": [{"tag": "env", "value": "prod"}],
        "inventory": {"os_short": "Ubuntu 22.04"},
    }
    _problems = problems if problems is not None else [
        {
            "eventid": "1001",
            "name": "High CPU",
            "severity": "3",
            "clock": str(now - 300),
        },
    ]
    _events = events if events is not None else [
        {"eventid": "900", "name": "Disk space low", "clock": str(now - 86400), "severity": "3"},
        {"eventid": "901", "name": "Disk space low", "clock": str(now - 172800), "severity": "3"},
        {"eventid": "902", "name": "High CPU", "clock": str(now - 200000), "severity": "3"},
    ]
    _items = items if items is not None else [
        {"itemid": "5001", "key_": "system.cpu.util", "value_type": "0", "name": "CPU util"},
    ]
    _history = history if history is not None else [
        {"clock": str(now - i * 300), "value": str(30.0 + i * 0.5)}
        for i in range(20)
    ]

    async def _call(method, params=None):
        if method == "host.get":
            return [_host]
        if method == "problem.get":
            return _problems
        if method == "event.get":
            return _events
        if method == "item.get":
            return _items
        if method == "history.get":
            return _history
        return []

    client = MagicMock()
    client.call = AsyncMock(side_effect=_call)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_briefing_contains_expected_sections():
    """Happy-path: briefing includes header, open problems, history, metrics."""
    client = _make_client()
    md = await build_host_briefing(client, hostid=42, days=30, os_kind="linux")

    assert "=== Host briefing (pre-fetched) ===" in md
    assert "srv-test-01" in md
    assert "Open problems" in md
    assert "High CPU" in md
    # History section should show "Disk space low" (not in open problems)
    assert "Disk space low" in md
    assert "30-day problem history" in md
    # Metric trends
    assert "metric trends" in md.lower() or "Metric" in md


async def test_briefing_skipped_when_disabled():
    """If host_briefing_config has enabled=False, orchestrator should not call us.

    We test this at the build_host_briefing level by confirming the function itself
    still runs (it doesn't know about the enabled flag — the orchestrator gates it).
    This test instead verifies the orchestrator integration path via InvestigationContext.
    """
    # The enabled check is in the orchestrator. Here we just confirm briefing_md
    # can be left empty and _render_user_prompt works without briefing.
    from zabbix_ai.orchestrator import InvestigationContext, Orchestrator

    ctx = InvestigationContext(source="test", question="hi")
    rendered = Orchestrator._render_user_prompt(ctx)
    assert "briefing" not in rendered.lower()
    assert "Source: test" in rendered


async def test_open_problems_not_duplicated_in_history():
    """Triggers in the open-problems list must be excluded from 30-day history."""
    now = int(time.time())
    # "High CPU" is both open and in the event log
    client = _make_client(
        problems=[{"eventid": "1001", "name": "High CPU", "severity": "3",
                   "clock": str(now - 60)}],
        events=[
            {"eventid": "900", "name": "High CPU", "clock": str(now - 1000), "severity": "3"},
            {"eventid": "901", "name": "High CPU", "clock": str(now - 2000), "severity": "3"},
            {"eventid": "902", "name": "Disk space low", "clock": str(now - 3000), "severity": "3"},
        ],
    )
    md = await build_host_briefing(client, hostid=42, days=30, os_kind="linux")

    # "High CPU" appears once in open problems
    assert "High CPU" in md

    # In the history section, "High CPU" should NOT appear (it was deduped)
    lines = md.split("\n")
    history_section_active = False
    history_lines: list[str] = []
    for line in lines:
        if "30-day problem history" in line:
            history_section_active = True
        elif history_section_active and line.startswith("#"):
            break
        elif history_section_active:
            history_lines.append(line)

    history_text = "\n".join(history_lines)
    assert "High CPU" not in history_text
    assert "Disk space low" in history_text


async def test_inverted_key_converts_to_pct_used():
    """vm.memory.size[pavailable] returns free %; briefing must show used %."""
    now = int(time.time())
    # All samples report 30% free → should be rendered as 70% used
    history_samples = [
        {"clock": str(now - i * 300), "value": "30.0"}
        for i in range(20)
    ]

    async def _call(method, params=None):
        if method == "host.get":
            return [{"hostid": "42", "host": "test", "name": "test",
                     "hostgroups": [], "tags": [], "inventory": {}}]
        if method == "problem.get":
            return []
        if method == "event.get":
            return []
        if method == "item.get":
            key = (params or {}).get("search", {}).get("key_", "")
            # Only match for pavailable key
            if "pavailable" in key:
                return [{"itemid": "9001", "key_": "vm.memory.size[pavailable]",
                         "value_type": "0", "name": "Memory available"}]
            return []
        if method == "history.get":
            return history_samples
        return []

    client = MagicMock()
    client.call = AsyncMock(side_effect=_call)

    md = await build_host_briefing(client, hostid=42, days=30, os_kind="linux")

    # The briefing should show ~70% (inverted from 30% free)
    # It appears in the metrics table — check the mean is ~70
    assert "70.0" in md or "70" in md


async def test_missing_metric_key_skipped_gracefully():
    """If no candidate key exists for a metric, it should be silently omitted."""

    async def _call(method, params=None):
        if method == "host.get":
            return [{"hostid": "99", "host": "sparse", "name": "sparse",
                     "hostgroups": [], "tags": [], "inventory": {}}]
        if method == "problem.get":
            return []
        if method == "event.get":
            return []
        if method == "item.get":
            return []   # no items found for any key
        if method == "history.get":
            return []
        return []

    client = MagicMock()
    client.call = AsyncMock(side_effect=_call)

    md = await build_host_briefing(client, hostid=99, days=30, os_kind="linux")
    # Should succeed without error; no metric rows → no metrics section
    assert "=== Host briefing (pre-fetched) ===" in md
    assert "metric trends" not in md.lower()


async def test_briefing_respects_max_tokens_soft_cap():
    """When rendered briefing exceeds max_tokens, lower-priority sections are dropped."""
    now = int(time.time())
    # Generate lots of events to make the history section large
    many_events = [
        {"eventid": str(i), "name": f"Alert type {i % 15}", "clock": str(now - i * 1000),
         "severity": "2"}
        for i in range(200)
    ]

    async def _call(method, params=None):
        if method == "host.get":
            return [{"hostid": "42", "host": "bighost", "name": "bighost",
                     "hostgroups": [{"name": "Test"}], "tags": [], "inventory": {}}]
        if method == "problem.get":
            return []
        if method == "event.get":
            return many_events
        if method == "item.get":
            return []
        if method == "history.get":
            return []
        return []

    client = MagicMock()
    client.call = AsyncMock(side_effect=_call)

    # Tiny cap — briefing must stay roughly within it
    md = await build_host_briefing(client, hostid=42, days=30, max_tokens=300)
    # rough_tokens estimate: len // 4
    approx_tokens = len(md) // 4
    # Should be no more than 2x the cap (header alone can use ~100 tokens)
    assert approx_tokens < 600, f"briefing too large: ~{approx_tokens} tokens"


async def test_briefing_prepended_in_user_prompt():
    """Orchestrator _render_user_prompt should prepend briefing_md."""
    from zabbix_ai.orchestrator import InvestigationContext, Orchestrator

    ctx = InvestigationContext(
        source="test",
        question="Is the disk OK?",
        hostid=42,
        hostname="srv01",
        briefing_md="=== Host briefing (pre-fetched) ===\n\n**Host:** srv01",
    )
    rendered = Orchestrator._render_user_prompt(ctx)
    assert rendered.startswith("=== Host briefing (pre-fetched) ===")
    assert "Source: test" in rendered
    assert "Is the disk OK?" in rendered
