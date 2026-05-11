from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from zabbix_ai.audit import AuditLog
from zabbix_ai.memory import (
    Memory,
    compute_pattern_signature,
    upsert_host_facts,
    upsert_pattern,
)
from zabbix_ai.prompts import SYSTEM_PROMPT, build_cached_system_blocks
from zabbix_ai.services.budget import BudgetExceededError, enforce_budget
from zabbix_ai.services.host_briefing import build_host_briefing
from zabbix_ai.services.hostbill_link import (
    HostBillLink,
    get_recent_tickets,
    link_zabbix_host,
)
from zabbix_ai.tools import claude_tool_definitions, dispatch

_log = logging.getLogger(__name__)


def _parse_json_lenient(text: str) -> dict:
    """json.loads, but tolerant of common LLM output deviations.

    Strips markdown fences, trailing prose, and falls back to extracting
    the largest top-level {...} block. Returns an empty dict if everything
    fails — callers treat empty as "skip write-back".
    """
    text = text.strip()
    # strip ```json / ``` fences
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    # find largest matching {...} pair (handles unescaped strings less badly
    # than rfind alone — we walk from the first { and track brace depth)
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    in_string = False
    escape = False
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    candidate = text[start: end + 1] if end > start else text[start:]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return {}


@dataclass
class InvestigationContext:
    source: str
    question: str = ""
    instance: str | None = None
    eventid: int | None = None
    ticket_id: int | None = None
    customer_id: int | None = None
    hostid: int | None = None
    hostname: str | None = None
    host_inventory_summary: str = ""
    problem_name: str = ""
    hostgroup: str = ""
    briefing_md: str = ""
    # How the investigation was triggered. Mirrors the ``trigger_source``
    # column on ``investigations`` (migration 007). The orchestrator itself
    # does not consume this field — callers (e.g. the Zabbix auto-investigate
    # webhook) write it onto the row out-of-band so v1.5 cost/observability
    # queries can slice manual vs auto runs.
    trigger_source: str = "manual"
    # HostBill linkage populated by _enrich_context. ``hostbill_link`` is
    # always present after enrichment — it may simply have linked_by='unlinked'.
    hostbill_link: HostBillLink | None = None
    recent_tickets: list = field(default_factory=list)

@dataclass
class InvestigationResult:
    investigation_id: int
    summary: str
    tool_calls: int
    tokens_in: int
    tokens_out: int
    duration_ms: int
    transcript: list[dict[str, Any]] = field(default_factory=list)
    pattern_signature: str = ""

class Orchestrator:
    def __init__(self, *, claude, audit: AuditLog, model: str, summary_model: str,
                 max_tool_calls: int, clients: dict[str, Any],
                 memory: Memory | None = None,
                 hostbill_client=None,
                 scripts: dict[str, Any] | None = None,
                 host_briefing_config: dict[str, Any] | None = None,
                 settings: Any = None):
        self.claude = claude
        self.audit = audit
        self.model = model
        self.summary_model = summary_model
        self.max_tool_calls = max_tool_calls
        self.clients = clients
        self.memory = memory
        self.hostbill_client = hostbill_client
        self.scripts = scripts or {}
        self.host_briefing_config = host_briefing_config
        # Settings handle is optional — when present we consult the daily
        # Anthropic budget before any claude.create() call. Tests can pass
        # ``settings=None`` to skip the budget gate entirely.
        self.settings = settings
        # Per-investigation effective model (may be downgraded by the
        # budget gate). Reset at the top of each .investigate() call so
        # one over-budget run doesn't permanently pin haiku.
        self._model_for_this_run: str = model

    async def _enrich_context(self, ctx: InvestigationContext) -> None:
        """Pre-fetch problem and host data so write-back can compute a stable
        pattern signature. Mutates ctx in place. No-ops on failure — write-back
        will silently skip if fields stay empty."""
        if not ctx.instance or ctx.instance not in self.clients:
            return
        client = self.clients[ctx.instance]
        if ctx.eventid and not ctx.problem_name:
            try:
                problem = await client.get_problem(ctx.eventid)
            except Exception as e:
                _log.debug("enrich: get_problem(%s) failed: %s", ctx.eventid, e)
                problem = None
            if problem:
                ctx.problem_name = problem.get("name") or ""
                hosts = problem.get("hosts") or []
                if hosts and not ctx.hostid:
                    with contextlib.suppress(KeyError, ValueError, TypeError):
                        ctx.hostid = int(hosts[0]["hostid"])
                if hosts and not ctx.hostname:
                    ctx.hostname = hosts[0].get("host", "") or hosts[0].get("name", "")
        if ctx.hostid and not ctx.hostgroup:
            # Lightweight host.get — full get_host pulls inventory/interfaces/
            # tags and can be slow on busy hosts; enrichment only needs the
            # first hostgroup name.
            try:
                rows = await client.call("host.get", {
                    "hostids": [str(ctx.hostid)],
                    "output": ["host", "name"],
                    "selectHostGroups": ["name"],
                })
            except Exception as e:
                _log.debug("enrich: lightweight host.get(%s) failed: %s",
                           ctx.hostid, e)
                rows = []
            if rows:
                row = rows[0]
                groups = row.get("hostgroups") or row.get("groups") or []
                if groups:
                    ctx.hostgroup = groups[0].get("name", "")
                if not ctx.hostname:
                    ctx.hostname = row.get("name") or row.get("host", "")

        # Host briefing pre-fetch (after hostname/hostgroup are resolved)
        cfg = self.host_briefing_config
        if ctx.hostid and cfg and cfg.get("enabled", True):
            try:
                # Detect OS from hostname heuristic (rough; briefing uses Linux defaults)
                hn = (ctx.hostname or "").lower()
                os_kind = "windows" if any(
                    w in hn for w in ("win", "plesk", "iis", "windows")
                ) else "linux"
                ctx.briefing_md = await build_host_briefing(
                    client,
                    hostid=ctx.hostid,
                    os_kind=os_kind,
                    days=int(cfg.get("days", 30)),
                    max_tokens=int(cfg.get("max_tokens", 2000)),
                    memory=self.memory,
                )
            except Exception as e:
                _log.warning("host_briefing failed for %s: %s", ctx.hostid, e)

        # ── HostBill linkage (best-effort) ─────────────────────────────────
        # Always attempt linkage when we know the host; the linker degrades
        # to ``linked_by='unlinked'`` if HostBill is unconfigured / down so
        # this never blocks an investigation.
        if ctx.hostid and ctx.instance and self.memory is not None:
            try:
                ctx.hostbill_link = await link_zabbix_host(
                    memory=self.memory,
                    hostbill_client=self.hostbill_client,
                    zabbix_client=client,
                    zabbix_instance=ctx.instance,
                    zabbix_hostid=ctx.hostid,
                )
            except Exception as e:
                _log.warning("hostbill link failed for %s/%s: %s",
                             ctx.instance, ctx.hostid, e)
                ctx.hostbill_link = None

            link = ctx.hostbill_link
            if link is not None and link.hostbill_client_id is not None:
                try:
                    ctx.recent_tickets = await get_recent_tickets(
                        self.hostbill_client,
                        client_id=link.hostbill_client_id,
                        service_id=link.hostbill_service_id,
                        days=30,
                    )
                except Exception as e:
                    _log.debug("get_recent_tickets failed for %s: %s",
                               link.hostbill_client_id, e)
                    ctx.recent_tickets = []

    async def investigate(self, ctx: InvestigationContext) -> InvestigationResult:
        await self._enrich_context(ctx)
        # Budget gate runs BEFORE any Claude call so we never burn one
        # extra-paid token over the cap. The audit row is keyed by
        # investigation_id=None because the investigation hasn't been
        # created yet — the audit row's purpose is to log the *decision*,
        # not the run.
        effective_model = self.model
        if self.memory is not None and self.settings is not None:
            effective_model, reason = await enforce_budget(
                self.memory, self.settings,
                investigation_id=None,
                model_requested=self.model,
            )
            if effective_model is None:
                raise BudgetExceededError(
                    f"Anthropic budget exhausted ({reason})"
                )
        self._model_for_this_run = effective_model

        start = time.monotonic()
        inv_id = await self.audit.log_start(
            source=ctx.source, instance=ctx.instance, eventid=ctx.eventid,
            ticket_id=ctx.ticket_id, customer_id=ctx.customer_id,
            hostid=ctx.hostid, hostname=ctx.hostname,
            model=self._model_for_this_run,
        )
        system_blocks = build_cached_system_blocks(
            SYSTEM_PROMPT, claude_tool_definitions(), ctx.host_inventory_summary,
        )
        user_prompt = self._render_user_prompt(ctx)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        tool_calls = 0
        tokens_in = tokens_out = 0
        final_text = ""

        budget_exhausted = False
        while True:
            resp = await self.claude.create(
                model=self._model_for_this_run, system=system_blocks,
                tools=claude_tool_definitions(),
                messages=messages, max_tokens=2048,
            )
            tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
            tokens_out += getattr(resp.usage, "output_tokens", 0) or 0

            if resp.stop_reason == "end_turn" or budget_exhausted:
                final_text = self._extract_text(resp.content)
                break

            if tool_calls >= self.max_tool_calls:
                messages.append({"role": "user",
                                 "content": "Tool budget exhausted. Produce final summary now."})
                budget_exhausted = True
                continue

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls += 1
                try:
                    output = await dispatch(block.name, block.input or {},
                                             context={
                                                 "clients": self.clients,
                                                 "investigation_id": inv_id,
                                                 "memory": self.memory,
                                                 "hostbill_client": self.hostbill_client,
                                                 "scripts": self.scripts,
                                             })
                    await self.audit.log_tool(inv_id, block.name, block.input or {}, output)
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": str(output)[:8000]})
                except Exception as e:
                    await self.audit.log_tool(inv_id, block.name, block.input or {},
                                              f"ERROR: {e}")
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": f"ERROR: {e}",
                                         "is_error": True})
            messages.append({"role": "user", "content": tool_results})

        duration_ms = int((time.monotonic() - start) * 1000)
        # Run write-back before log_end so the computed pattern_signature
        # lands in the investigations row in the same transaction window.
        signature = await self._write_back(
            ctx=ctx, investigation_id=inv_id, final_text=final_text,
        )
        await self.audit.log_end(
            inv_id, summary=final_text, duration_ms=duration_ms,
            tokens_in=tokens_in, tokens_out=tokens_out,
            pattern_signature=signature,
        )
        return InvestigationResult(
            investigation_id=inv_id, summary=final_text,
            tool_calls=tool_calls, tokens_in=tokens_in, tokens_out=tokens_out,
            duration_ms=duration_ms, transcript=messages,
            pattern_signature=signature,
        )

    async def _write_back(self, *, ctx: InvestigationContext,
                          investigation_id: int,
                          final_text: str) -> str:
        """Run a cheap summarisation pass and update memory.

        Returns the pattern signature (empty string if write-back failed or
        memory is not configured).
        """
        if self.memory is None or not ctx.problem_name:
            return ""
        sig = compute_pattern_signature(problem_name=ctx.problem_name,
                                         hostgroup=ctx.hostgroup or "")
        try:
            prompt = (
                "Summarise this investigation as a JSON object with keys: "
                "root_cause_short (one sentence), fix_short (one sentence), "
                "host_facts (object of key→value strings, can be empty). "
                "Output ONLY the JSON object.\n\n"
                f"Investigation summary:\n{final_text[:4000]}"
            )
            resp = await self.claude.create(
                model=self.summary_model,
                system=[{"type": "text",
                         "text": "You extract structured facts from "
                                  "investigation summaries."}],
                tools=[],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
            )
            text = self._extract_text(resp.content)
            data = _parse_json_lenient(text)
            await upsert_pattern(
                self.memory, signature=sig,
                typical_root_cause=str(data.get("root_cause_short", "")),
                typical_fix=str(data.get("fix_short", "")),
            )
            facts = data.get("host_facts") or {}
            if isinstance(facts, dict) and ctx.hostid is not None and facts:
                await upsert_host_facts(
                    self.memory, hostid=ctx.hostid,
                    facts={k: str(v) for k, v in facts.items()
                           if isinstance(v, (str, int, float))},
                    source_investigation_id=investigation_id,
                )
        except Exception as e:
            _log.warning("write_back failed for inv %s: %s",
                         investigation_id, e)
        return sig

    @staticmethod
    def _render_user_prompt(ctx: InvestigationContext) -> str:
        parts: list[str] = []
        if ctx.briefing_md:
            parts.append(ctx.briefing_md)
            parts.append("")
        parts.append(f"Source: {ctx.source}")
        if ctx.instance:
            parts.append(f"Zabbix instance: {ctx.instance}")
        if ctx.eventid:
            parts.append(f"Event id: {ctx.eventid}")
        if ctx.hostid:
            parts.append(f"Host id: {ctx.hostid} ({ctx.hostname or ''})")
        if ctx.ticket_id:
            parts.append(f"Ticket id: {ctx.ticket_id}")
        if ctx.question:
            parts.append(f"\nQuestion / context:\n{ctx.question}")
        parts.append(
            "\nInvestigate using the provided tools and produce the final structured answer."
        )
        return "\n".join(parts)

    @staticmethod
    def _extract_text(content: list[Any]) -> str:
        out = []
        for b in content:
            t = getattr(b, "type", None)
            if t == "text":
                out.append(getattr(b, "text", ""))
        return "\n".join(out).strip()

    async def investigate_streaming(self, ctx: InvestigationContext):
        """Yield SSE-friendly events as the tool-use loop runs.

        Each yielded value is {"event": <str>, "data": <dict|str>}.
        Event kinds: started, tool_call, tool_result, thinking, final.
        """
        await self._enrich_context(ctx)
        # Same budget gate as the sync path — see .investigate() for the
        # rationale.
        effective_model = self.model
        if self.memory is not None and self.settings is not None:
            effective_model, reason = await enforce_budget(
                self.memory, self.settings,
                investigation_id=None,
                model_requested=self.model,
            )
            if effective_model is None:
                raise BudgetExceededError(
                    f"Anthropic budget exhausted ({reason})"
                )
        self._model_for_this_run = effective_model

        import time as _time
        start = _time.monotonic()
        inv_id = await self.audit.log_start(
            source=ctx.source, instance=ctx.instance, eventid=ctx.eventid,
            ticket_id=ctx.ticket_id, customer_id=ctx.customer_id,
            hostid=ctx.hostid, hostname=ctx.hostname,
            model=self._model_for_this_run,
        )
        yield {"event": "started", "data": {"investigation_id": inv_id,
                                             "model": self._model_for_this_run}}

        system_blocks = build_cached_system_blocks(
            SYSTEM_PROMPT, claude_tool_definitions(), ctx.host_inventory_summary,
        )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._render_user_prompt(ctx)},
        ]
        tool_calls = 0
        tokens_in = tokens_out = 0
        final_text = ""
        budget_exhausted = False

        while True:
            resp = await self.claude.create(
                model=self._model_for_this_run, system=system_blocks,
                tools=claude_tool_definitions(),
                messages=messages, max_tokens=2048,
            )
            tokens_in += getattr(resp.usage, "input_tokens", 0) or 0
            tokens_out += getattr(resp.usage, "output_tokens", 0) or 0

            text_chunks = [getattr(b, "text", "") for b in resp.content
                           if getattr(b, "type", None) == "text"]
            if text_chunks:
                yield {"event": "thinking",
                       "data": {"text": "\n".join(text_chunks)}}

            if resp.stop_reason == "end_turn" or budget_exhausted:
                final_text = "\n".join(text_chunks).strip()
                break

            if tool_calls >= self.max_tool_calls:
                messages.append({"role": "user",
                                 "content": "Tool budget exhausted. "
                                            "Produce final summary now."})
                budget_exhausted = True
                continue

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls += 1
                yield {"event": "tool_call",
                       "data": {"name": block.name,
                                "input": block.input or {},
                                "tool_use_id": block.id}}
                try:
                    output = await dispatch(block.name, block.input or {},
                                             context={
                                                 "clients": self.clients,
                                                 "investigation_id": inv_id,
                                                 "memory": self.memory,
                                                 "hostbill_client": self.hostbill_client,
                                                 "scripts": self.scripts,
                                             })
                    await self.audit.log_tool(inv_id, block.name,
                                              block.input or {}, output)
                    yield {"event": "tool_result",
                           "data": {"tool_use_id": block.id,
                                    "output": str(output)[:8000],
                                    "is_error": False}}
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": str(output)[:8000]})
                except Exception as e:
                    msg = f"ERROR: {e}"
                    await self.audit.log_tool(inv_id, block.name,
                                              block.input or {}, msg)
                    yield {"event": "tool_result",
                           "data": {"tool_use_id": block.id,
                                    "output": msg, "is_error": True}}
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": msg, "is_error": True})
            messages.append({"role": "user", "content": tool_results})

        duration_ms = int((_time.monotonic() - start) * 1000)
        signature = await self._write_back(
            ctx=ctx, investigation_id=inv_id, final_text=final_text,
        )
        await self.audit.log_end(
            inv_id, summary=final_text, duration_ms=duration_ms,
            tokens_in=tokens_in, tokens_out=tokens_out,
            pattern_signature=signature,
        )
        yield {"event": "final",
               "data": {"investigation_id": inv_id,
                        "summary": final_text,
                        "tool_calls": tool_calls,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "duration_ms": duration_ms,
                        "pattern_signature": signature}}
