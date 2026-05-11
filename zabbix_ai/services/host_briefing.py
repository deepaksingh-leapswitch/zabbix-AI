"""host_briefing — pre-fetch a structured Markdown briefing for a host.

Called by the orchestrator's _enrich_context step when hostid is known.
Returns a compact Markdown block (target ≤ 2000 tokens) that is prepended
to the first user message so the AI skips redundant discovery tool calls.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from typing import Any

from zabbix_ai.memory import Memory, find_pattern, find_similar_past_investigations
from zabbix_ai.tools.forecast import _linear_fit, _to_floats

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Item-key candidates per metric (auto-detect first match)
# ---------------------------------------------------------------------------

LINUX_METRICS: dict[str, list[str]] = {
    "cpu_pct": ["system.cpu.util", "system.cpu.util[,user]", "system.cpu.util[,idle]"],
    "mem_pct": ["vm.memory.utilization", "vm.memory.size[pavailable]"],
    "disk_root_pct": ["vfs.fs.size[/,pused]", "vfs.fs.size[/,used,pfree]"],
    "load1m": ["system.cpu.load[all,avg1]", "system.cpu.load[,avg1]"],
}

WINDOWS_METRICS: dict[str, list[str]] = {
    "cpu_pct": [
        "system.cpu.util",
        r'perf_counter_en["\Processor Information(_Total)\% Processor Time"]',
        r'perf_counter["\Processor(_Total)\% Processor Time"]',
    ],
    "mem_pct": [
        "vm.memory.utilization",
        r'perf_counter_en["\Memory\% Committed Bytes In Use"]',
    ],
    "disk_root_pct": ["vfs.fs.size[C:,pused]"],
    "load1m": [],  # Windows has no load avg
}

# Keys that return "free %" rather than "used %" — invert before display
_INVERTED_KEYS: frozenset[str] = frozenset(
    {"vm.memory.size[pavailable]", "vfs.fs.size[/,used,pfree]"}
)

# Severity map (Zabbix numeric → label)
_SEV: dict[str, str] = {
    "0": "Not classified",
    "1": "Info",
    "2": "Warning",
    "3": "Average",
    "4": "High",
    "5": "Disaster",
}

# Section priority for token-cap trimming (lowest priority first)
_SECTION_ORDER = ["forecast_hits", "past_investigations", "patterns", "history"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _age_str(ts: int | str | None) -> str:
    if ts is None:
        return "?"
    try:
        delta = int(time.time()) - int(ts)
    except (TypeError, ValueError):
        return "?"
    if delta < 120:
        return f"{delta}s"
    if delta < 7200:
        return f"{delta // 60}m"
    if delta < 172800:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _rough_tokens(text: str) -> int:
    """Rough token count estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _compute_stats(points: list[tuple[float, float]]) -> dict[str, float] | None:
    """Compute min/max/mean/median/latest/slope_per_day from (clock, value) pairs."""
    if len(points) < 2:
        return None
    values = [p[1] for p in points]
    slope_sec, _intercept = _linear_fit(points)
    latest_clock = max(points, key=lambda p: p[0])
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "latest": latest_clock[1],
        "slope_per_day": slope_sec * 86400,
    }


def _time_to_threshold(stats: dict[str, float], threshold: float) -> float | None:
    """Return seconds until metric crosses threshold, or None if never/already."""
    slope_day = stats["slope_per_day"]
    if slope_day <= 0:
        return None
    # current ~ stats["latest"], slope in /day
    gap = threshold - stats["latest"]
    if gap <= 0:
        return None
    return (gap / slope_day) * 86400  # convert days to seconds


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

async def build_host_briefing(
    client: Any,
    *,
    hostid: int,
    days: int = 30,
    os_kind: str = "linux",
    max_tokens: int = 2000,
    memory: Memory | None = None,
    hostbill_link: Any = None,
    recent_tickets: list[dict] | None = None,
) -> str:
    """Return a Markdown host briefing, capped at roughly max_tokens.

    Sections dropped (lowest priority first) if over the token cap:
    forecast_hits → past_investigations → patterns → 30-day history.

    When ``hostbill_link`` is supplied and has a HostBill client_id, a
    "Customer (HostBill)" section is rendered between the host header and
    the open-problems section. When the link is unlinked or HostBill is
    unreachable, the section is omitted silently.
    """
    time_from = int(time.time()) - days * 86400

    # Parallel fetch: host details, open problems, 30-day event history
    host_task = asyncio.create_task(_fetch_host(client, hostid))
    problems_task = asyncio.create_task(_fetch_open_problems(client, hostid))
    events_task = asyncio.create_task(_fetch_events(client, hostid, time_from))

    host_info, open_problems, events = await asyncio.gather(
        host_task, problems_task, events_task,
        return_exceptions=True,
    )

    # Render sections into a dict so we can drop from the back
    sections: dict[str, str] = {}

    # ── Section 1: Host header ───────────────────────────────────────────────
    header_lines = _render_header(host_info, hostid, os_kind)
    sections["header"] = "\n".join(header_lines)

    # ── Section 1b: HostBill customer (only when linked) ─────────────────────
    hb_md = _render_hostbill(hostbill_link, recent_tickets)
    if hb_md:
        sections["hostbill"] = hb_md

    # ── Section 2: Open problems ─────────────────────────────────────────────
    open_names: set[str] = set()
    if isinstance(open_problems, list) and open_problems:
        open_names = {p.get("name", "") for p in open_problems}
        sections["open_problems"] = _render_open_problems(open_problems)

    # ── Section 3: 30-day problem history ───────────────────────────────────
    if isinstance(events, list) and events:
        hist_md = _render_history(events, open_names)
        if hist_md:
            sections["history"] = hist_md

    # ── Sections 4+5: Metric trends + forecast ───────────────────────────────
    metric_keys = WINDOWS_METRICS if os_kind.lower() == "windows" else LINUX_METRICS
    metric_rows, forecast_rows = await _fetch_metrics(
        client, hostid, metric_keys, time_from,
    )
    if metric_rows:
        sections["metrics"] = _render_metrics(metric_rows)
    if forecast_rows:
        sections["forecast_hits"] = _render_forecasts(forecast_rows)

    # ── Sections 6+7: Past investigations + patterns ─────────────────────────
    if memory is not None:
        past = await _safe(
            find_similar_past_investigations(memory, hostid=hostid,
                                             pattern_signature=None, limit=5)
        )
        if past:
            sections["past_investigations"] = _render_past(past)
            sigs = {inv.get("pattern_signature", "") for inv in past
                    if inv.get("pattern_signature")}
            pat_parts: list[str] = []
            for sig in sigs:
                p = await _safe(find_pattern(memory, signature=sig))
                if p:
                    pat_parts.append(
                        f"| `{sig}` | {p.get('occurrences', 0)} | "
                        f"{str(p.get('typical_fix') or '')[:100]} |"
                    )
            if pat_parts:
                sections["patterns"] = (
                    "**Matching patterns**\n\n"
                    "| Signature | Occurrences | Typical fix |\n"
                    "|-----------|-------------|-------------|\n"
                    + "\n".join(pat_parts)
                )

    # ── Assemble and trim to token cap ───────────────────────────────────────
    return _assemble(sections, max_tokens)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def _safe(coro: Any) -> Any:
    try:
        return await coro
    except Exception as exc:
        _log.debug("host_briefing fetch skipped: %s", exc)
        return None


async def _fetch_host(client: Any, hostid: int) -> dict | Exception:
    try:
        rows = await client.call("host.get", {
            "hostids": [str(hostid)],
            "output": ["host", "name", "status"],
            "selectHostGroups": ["name"],
            "selectTags": ["tag", "value"],
            "selectInventory": ["os", "os_short"],
        })
        return rows[0] if rows else {}
    except Exception as exc:
        _log.debug("host_briefing: host.get failed: %s", exc)
        return exc


async def _fetch_open_problems(client: Any, hostid: int) -> list | Exception:
    try:
        return await client.call("problem.get", {
            "hostids": [str(hostid)],
            "output": "extend",
            "recent": False,
        })
    except Exception as exc:
        _log.debug("host_briefing: problem.get failed: %s", exc)
        return exc


async def _fetch_events(
    client: Any, hostid: int, time_from: int,
) -> list | Exception:
    try:
        return await client.call("event.get", {
            "hostids": [str(hostid)],
            "output": ["eventid", "name", "clock", "severity"],
            "value": 1,
            "source": 0,
            "object": 0,
            "time_from": time_from,
            "limit": 2000,
        })
    except Exception as exc:
        _log.debug("host_briefing: event.get failed: %s", exc)
        return exc


async def _fetch_metrics(
    client: Any,
    hostid: int,
    metric_keys: dict[str, list[str]],
    time_from: int,
) -> tuple[list[dict], list[dict]]:
    """Return (metric_rows, forecast_rows) for each metric in metric_keys."""
    metric_rows: list[dict] = []
    forecast_rows: list[dict] = []

    async def _one_metric(name: str, candidates: list[str]) -> None:
        if not candidates:
            return
        # Find first matching item
        item: dict | None = None
        matched_key: str = ""
        for key in candidates:
            try:
                rows = await client.call("item.get", {
                    "hostids": [str(hostid)],
                    "search": {"key_": key},
                    "searchByAny": True,
                    "output": ["itemid", "key_", "value_type", "name"],
                    "limit": 1,
                })
                if rows:
                    item = rows[0]
                    matched_key = key
                    break
            except Exception as exc:
                _log.debug("host_briefing item.get(%s): %s", key, exc)
        if item is None:
            return

        # Fetch history
        try:
            history_rows = await client.call("history.get", {
                "itemids": [item["itemid"]],
                "history": int(item.get("value_type", 0)),
                "time_from": time_from,
                "sortfield": "clock",
                "sortorder": "ASC",
                "limit": 2000,
            })
        except Exception as exc:
            _log.debug("host_briefing history.get(%s): %s", item["itemid"], exc)
            return

        points = _to_floats(
            [{"clock": r["clock"], "value": r["value"]} for r in history_rows]
        )
        if len(points) < 2:
            return

        # Invert if key returns "free %" instead of "used %"
        inverted = matched_key in _INVERTED_KEYS
        if inverted:
            points = [(c, 100.0 - v) for c, v in points]

        stats = _compute_stats(points)
        if stats is None:
            return

        is_pct = name in ("cpu_pct", "mem_pct", "disk_root_pct")
        warn = is_pct and (stats["latest"] >= 85 or stats["slope_per_day"] > 1.0)

        metric_rows.append({
            "name": name,
            "key": matched_key,
            "stats": stats,
            "warn": warn,
            "is_pct": is_pct,
        })

        # Forecast: only percentage metrics where threshold crossing < 90 days
        if is_pct:
            secs = _time_to_threshold(stats, 90.0)
            if secs is not None and 0 < secs < 90 * 86400:
                days_left = secs / 86400
                forecast_rows.append({"name": name, "days": days_left})

    tasks = [_one_metric(n, ks) for n, ks in metric_keys.items()]
    await asyncio.gather(*tasks, return_exceptions=True)
    return metric_rows, forecast_rows


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _render_header(host_info: Any, hostid: int, os_kind: str) -> list[str]:
    lines: list[str] = ["=== Host briefing (pre-fetched) ===", ""]
    if isinstance(host_info, dict) and host_info:
        name = host_info.get("name") or host_info.get("host", f"hostid={hostid}")
        lines.append(f"**Host:** {name} (id={hostid})")
        groups = host_info.get("hostgroups") or host_info.get("groups") or []
        if groups:
            top5 = ", ".join(g["name"] for g in groups[:5])
            lines.append(f"**Groups:** {top5}")
        inv = host_info.get("inventory") or {}
        os_info = inv.get("os_short") or inv.get("os") or os_kind
        lines.append(f"**OS:** {os_info}")
        tags = host_info.get("tags") or []
        if tags:
            tag_str = ", ".join(
                f"{t['tag']}={t['value']}" if t.get("value") else t["tag"]
                for t in tags[:8]
            )
            lines.append(f"**Tags:** {tag_str}")
    else:
        lines.append(f"**Host id:** {hostid}  **OS:** {os_kind}")
    lines.append("")
    return lines


def _render_open_problems(problems: list[dict]) -> str:
    lines = ["**Open problems**", ""]
    lines.append("| Severity | Age | Event | Name |")
    lines.append("|----------|-----|-------|------|")
    for p in problems:
        sev = _SEV.get(str(p.get("severity", "0")), "?")
        age = _age_str(p.get("clock"))
        eid = p.get("eventid", "")
        name = (p.get("name") or "")[:80]
        lines.append(f"| {sev} | {age} | {eid} | {name} |")
    return "\n".join(lines)


def _render_history(events: list[dict], open_names: set[str]) -> str:
    counts: dict[str, int] = {}
    for ev in events:
        n = ev.get("name") or ""
        if n not in open_names:
            counts[n] = counts.get(n, 0) + 1
    if not counts:
        return ""
    top10 = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    lines = ["**30-day problem history** (excluding open)", ""]
    lines.append("| Occurrences | Trigger |")
    lines.append("|-------------|---------|")
    for name, cnt in top10:
        lines.append(f"| {cnt} | {name[:80]} |")
    return "\n".join(lines)


def _render_metrics(rows: list[dict]) -> str:
    lines = ["**30-day metric trends**", ""]
    lines.append("| Metric | Min | Max | Mean | Latest | Slope/day |")
    lines.append("|--------|-----|-----|------|--------|-----------|")
    for row in rows:
        s = row["stats"]
        warn = " ⚠" if row["warn"] else ""
        fmt = "{:.1f}" if row["is_pct"] else "{:.2f}"
        lines.append(
            f"| {row['name']}{warn} "
            f"| {fmt.format(s['min'])} "
            f"| {fmt.format(s['max'])} "
            f"| {fmt.format(s['mean'])} "
            f"| {fmt.format(s['latest'])} "
            f"| {s['slope_per_day']:+.3f} |"
        )
    return "\n".join(lines)


def _render_forecasts(rows: list[dict]) -> str:
    lines = ["**Forecast: metrics approaching 90% threshold**", ""]
    lines.append("| Metric | Days to 90% |")
    lines.append("|--------|-------------|")
    for row in rows:
        lines.append(f"| {row['name']} | {row['days']:.1f} |")
    return "\n".join(lines)


def _render_past(investigations: list[dict]) -> str:
    lines = ["**Past investigations on this host**", ""]
    lines.append("| ID | Age | Pattern | Summary |")
    lines.append("|----|-----|---------|---------|")
    for inv in investigations:
        age = _age_str_iso(inv.get("started_at"))
        sig = (inv.get("pattern_signature") or "")[:16]
        summary = (inv.get("summary") or inv.get("root_cause") or "")[:60]
        lines.append(f"| {inv.get('id')} | {age} | `{sig}` | {summary} |")

    # Surface any captured resolutions — the AI must lead with these.
    resolved = [inv for inv in investigations if inv.get("resolution_notes")]
    if resolved:
        lines.append("")
        lines.append("**Prior resolutions (READ FIRST — lead your report with these)**")
        lines.append("")
        for inv in resolved:
            when = (inv.get("resolution_at") or "")[:10]  # YYYY-MM-DD
            who = inv.get("resolution_by") or "?"
            sig = (inv.get("pattern_signature") or "")[:16]
            summary = (inv.get("summary") or inv.get("root_cause") or "")[:80]
            notes = (inv.get("resolution_notes") or "").strip().replace(
                "\n", " ",
            )[:400]
            lines.append(
                f"- {when}: signature=`{sig}`, "
                f"confidence={inv.get('confidence') or '—'}"
            )
            if summary:
                lines.append(f"    summary: {summary}")
            lines.append(f"    resolution: {who} — {notes}")
    return "\n".join(lines)


def _render_hostbill(link: Any, recent_tickets: list[dict] | None) -> str:
    """Render the Customer (HostBill) section.

    Returns an empty string when ``link`` is None, unlinked, or has no
    HostBill client_id — the assembler then silently drops the section.
    """
    if link is None:
        return ""
    client_id = getattr(link, "hostbill_client_id", None)
    service_id = getattr(link, "hostbill_service_id", None)
    if not client_id:
        return ""

    client_name = getattr(link, "hostbill_client_name", "") or "—"
    domain = getattr(link, "hostbill_domain", "") or "—"

    tickets = recent_tickets or []
    open_count = 0
    closed_count = 0
    for t in tickets:
        status = str(t.get("status", "")).strip().lower()
        if status in {"closed", "resolved", "answered"}:
            closed_count += 1
        else:
            open_count += 1

    lines = ["### Customer (HostBill)", ""]
    lines.append(f"Client: {client_name} (id {client_id})")
    if service_id is not None:
        lines.append(f"Service: {domain} (id {service_id})")
    else:
        lines.append(f"Service: {domain}")
    lines.append(
        f"Recent tickets (30 d): {open_count} open / {closed_count} closed"
    )
    # Show up to five recent tickets, newest first.
    for t in tickets[:5]:
        tid = t.get("id") or t.get("ticket_id") or "?"
        when = (
            t.get("date") or t.get("lastreply") or t.get("date_opened") or ""
        )[:10]
        status = str(t.get("status", "")).strip().lower() or "open"
        subj = (t.get("subject") or t.get("title") or "").strip()[:100]
        lines.append(f"  - #{tid} {when} [{status}]  Subject: {subj}")
    return "\n".join(lines)


def _age_str_iso(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        from datetime import UTC, datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        delta = int(datetime.now(UTC).timestamp() - dt.timestamp())
    except Exception:
        return "?"
    return _age_str(int(time.time()) - delta)


# ---------------------------------------------------------------------------
# Assembly + token cap
# ---------------------------------------------------------------------------

_SECTION_RENDER_ORDER = [
    "header",
    "hostbill",
    "open_problems",
    "history",
    "metrics",
    "forecast_hits",
    "past_investigations",
    "patterns",
]

# Lowest priority first — these are dropped first when over cap
_DROP_ORDER = ["forecast_hits", "past_investigations", "patterns", "history"]


def _assemble(sections: dict[str, str], max_tokens: int) -> str:
    def _build(drop: set[str]) -> str:
        parts: list[str] = []
        for key in _SECTION_RENDER_ORDER:
            if key in sections and key not in drop:
                parts.append(sections[key])
        return "\n\n".join(parts)

    dropped: set[str] = set()
    result = _build(dropped)
    for section_key in _DROP_ORDER:
        if _rough_tokens(result) <= max_tokens:
            break
        if section_key in sections:
            dropped.add(section_key)
            result = _build(dropped)

    return result
