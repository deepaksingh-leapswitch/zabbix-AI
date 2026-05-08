from __future__ import annotations

from pathlib import Path

from zabbix_ai.audit import AuditLog
from zabbix_ai.clients.claude import ClaudeClient
from zabbix_ai.clients.zabbix import ZabbixClient
from zabbix_ai.config import Settings
from zabbix_ai.memory import Memory
from zabbix_ai.orchestrator import (
    InvestigationContext,
    InvestigationResult,
    Orchestrator,
)
from zabbix_ai.tools import diag as tools_diag
from zabbix_ai.tools import lookup as tools_lookup
from zabbix_ai.tools import zabbix as tools_zabbix

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"

class InvestigationRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._mem: Memory | None = None
        self._zabbix_clients: dict[str, ZabbixClient] = {}
        self._orch: Orchestrator | None = None

    async def __aenter__(self) -> InvestigationRunner:
        for inst in self.settings.zabbix_instances:
            self._zabbix_clients[inst.name] = ZabbixClient(
                inst.name, str(inst.url), inst.token.get_secret_value(),
            )
        tools_zabbix.register_tools()
        tools_diag.register_tools()
        tools_lookup.register_tools()

        Path(self.settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = Memory(self.settings.sqlite_path)
        await self._mem.connect()
        await self._mem.run_migrations(_MIGRATIONS_DIR)

        claude = ClaudeClient(api_key=self.settings.anthropic_api_key.get_secret_value())
        self._orch = Orchestrator(
            claude=claude,
            audit=AuditLog(self._mem),
            model=self.settings.default_model,
            summary_model=self.settings.summary_model,
            max_tool_calls=self.settings.max_tool_calls,
            clients=self._zabbix_clients,
        )
        return self

    async def __aexit__(self, *_exc) -> None:
        for c in self._zabbix_clients.values():
            await c.aclose()
        if self._mem:
            await self._mem.close()

    async def investigate(self, ctx: InvestigationContext) -> InvestigationResult:
        if not self._orch:
            raise RuntimeError("InvestigationRunner not entered")
        return await self._orch.investigate(ctx)
