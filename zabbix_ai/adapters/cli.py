from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from zabbix_ai.audit import AuditLog
from zabbix_ai.clients.claude import ClaudeClient
from zabbix_ai.clients.zabbix import ZabbixClient
from zabbix_ai.config import load_settings
from zabbix_ai.memory import Memory
from zabbix_ai.orchestrator import InvestigationContext, Orchestrator
from zabbix_ai.renderers.text import render
from zabbix_ai.tools import diag as tools_diag
from zabbix_ai.tools import lookup as tools_lookup
from zabbix_ai.tools import zabbix as tools_zabbix

DEFAULT_CONFIG = "/etc/zabbix-ai/config.yaml"

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zabbix-ai")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("investigate", help="Run an AI investigation")
    inv.add_argument("--eventid", type=int)
    inv.add_argument("--hostid", type=int)
    inv.add_argument("--instance", required=True)
    inv.add_argument("--question", default="")
    inv.add_argument("--ticket-id", type=int)

    sub.add_parser("list-instances", help="Show configured Zabbix instances")
    return p

async def _run(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.config))

    if args.cmd == "list-instances":
        for inst in settings.zabbix_instances:
            print(f"{inst.name}\t{inst.url}")
        return 0

    clients: dict[str, ZabbixClient] = {}
    for inst in settings.zabbix_instances:
        token = (
            inst.token.get_secret_value()
            if hasattr(inst.token, "get_secret_value")
            else inst.token
        )
        clients[inst.name] = ZabbixClient(inst.name, str(inst.url), token)

    tools_zabbix.register_tools()
    tools_diag.register_tools()
    tools_lookup.register_tools()

    Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    mem = Memory(settings.sqlite_path)
    await mem.connect()
    await mem.run_migrations(Path(__file__).resolve().parents[2] / "migrations")
    audit = AuditLog(mem)

    api_key = (
        settings.anthropic_api_key.get_secret_value()
        if hasattr(settings.anthropic_api_key, "get_secret_value")
        else settings.anthropic_api_key
    )
    claude = ClaudeClient(api_key=api_key)
    orch = Orchestrator(
        claude=claude, audit=audit,
        model=settings.default_model, summary_model=settings.summary_model,
        max_tool_calls=settings.max_tool_calls, clients=clients,
    )

    if args.cmd == "investigate":
        ctx = InvestigationContext(
            source="cli", instance=args.instance,
            eventid=args.eventid, hostid=args.hostid,
            ticket_id=args.ticket_id, question=args.question,
        )
        result = await orch.investigate(ctx)
        print(render(result))
        for c in clients.values():
            await c.aclose()
        await mem.close()
        return 0
    return 1

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))

if __name__ == "__main__":
    sys.exit(main())
