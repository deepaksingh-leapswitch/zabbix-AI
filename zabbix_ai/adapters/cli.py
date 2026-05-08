from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from zabbix_ai.config import load_settings
from zabbix_ai.orchestrator import InvestigationContext
from zabbix_ai.renderers.text import render
from zabbix_ai.services.investigation_runner import InvestigationRunner

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

    async with InvestigationRunner(settings) as runner:
        if args.cmd == "investigate":
            ctx = InvestigationContext(
                source="cli", instance=args.instance,
                eventid=args.eventid, hostid=args.hostid,
                ticket_id=args.ticket_id, question=args.question,
            )
            result = await runner.investigate(ctx)
            print(render(result))
            return 0
    return 1

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))

if __name__ == "__main__":
    sys.exit(main())
