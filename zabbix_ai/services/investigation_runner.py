from __future__ import annotations

import logging
import os
from pathlib import Path

from zabbix_ai.admin.config_overlay import overlay_settings
from zabbix_ai.admin.crypto import derive_key
from zabbix_ai.audit import AuditLog
from zabbix_ai.clients.claude import ClaudeClient
from zabbix_ai.clients.hostbill import HostBillClient
from zabbix_ai.clients.zabbix import ZabbixClient
from zabbix_ai.config import Settings
from zabbix_ai.memory import Memory
from zabbix_ai.orchestrator import (
    InvestigationContext,
    InvestigationResult,
    Orchestrator,
)
from zabbix_ai.services.script_bootstrap import ScriptIndex, ensure_diag_scripts
from zabbix_ai.tools import diag as tools_diag
from zabbix_ai.tools import forecast as tools_forecast
from zabbix_ai.tools import lookup as tools_lookup
from zabbix_ai.tools import memory as tools_memory
from zabbix_ai.tools import zabbix as tools_zabbix

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"
_log = logging.getLogger(__name__)

class InvestigationRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._mem: Memory | None = None
        self._zabbix_clients: dict[str, ZabbixClient] = {}
        self._orch: Orchestrator | None = None
        self._hostbill: HostBillClient | None = None
        self._scripts: dict[str, ScriptIndex] = {}

    async def _ensure_memory(self) -> None:
        """Connect to the DB and run migrations exactly once."""
        if self._mem is not None:
            return
        Path(self.settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = Memory(self.settings.sqlite_path)
        await self._mem.connect()
        await self._mem.run_migrations(_MIGRATIONS_DIR)

    async def __aenter__(self) -> InvestigationRunner:
        # Open DB early so config overlay can read DB-stored connections.
        await self._ensure_memory()
        assert self._mem is not None

        master = os.environ.get("SECRETS_KEY") or os.environ.get("SESSION_SECRET", "")
        if master:
            try:
                self.settings = await overlay_settings(
                    self.settings, self._mem, derive_key(master),
                )
            except Exception as e:
                _log.warning("config overlay failed, using file config: %s", e)

        for inst in self.settings.zabbix_instances:
            self._zabbix_clients[inst.name] = ZabbixClient(
                inst.name, str(inst.url), inst.token.get_secret_value(),
            )
        tools_zabbix.register_tools()
        tools_diag.register_tools()
        tools_lookup.register_tools()
        tools_memory.register_tools()
        tools_forecast.register_tools()

        # Ensure rca-ai.diag.* global scripts exist on every Zabbix instance.
        # Best-effort: a transient API failure must not block service startup.
        # Diag tools will fail at invocation time with a clear error if the
        # script index is empty.
        for name, client in self._zabbix_clients.items():
            try:
                self._scripts[name] = await ensure_diag_scripts(client)
            except Exception as e:
                _log.warning("script bootstrap failed for %s: %s", name, e)
                self._scripts[name] = ScriptIndex()

        # Memory is already connected (done above); no-op call for clarity.
        await self._ensure_memory()

        if self.settings.hostbill is not None:
            self._hostbill = HostBillClient(
                api_url=str(self.settings.hostbill.api_url),
                api_id=self.settings.hostbill.api_id.get_secret_value(),
                api_key=self.settings.hostbill.api_key.get_secret_value(),
            )

        claude = ClaudeClient(api_key=self.settings.anthropic_api_key.get_secret_value())
        hb_cfg = self.settings.host_briefing
        self._orch = Orchestrator(
            claude=claude,
            audit=AuditLog(self._mem),
            model=self.settings.default_model,
            summary_model=self.settings.summary_model,
            max_tool_calls=self.settings.max_tool_calls,
            clients=self._zabbix_clients,
            memory=self._mem,
            hostbill_client=self._hostbill,
            scripts=self._scripts,
            host_briefing_config={
                "enabled": hb_cfg.enabled,
                "days": hb_cfg.days,
                "max_tokens": hb_cfg.max_tokens,
            },
        )
        return self

    async def __aexit__(self, *_exc) -> None:
        for c in self._zabbix_clients.values():
            await c.aclose()
        if self._hostbill is not None:
            await self._hostbill.aclose()
        if self._mem:
            await self._mem.close()

    async def investigate(self, ctx: InvestigationContext) -> InvestigationResult:
        if not self._orch:
            raise RuntimeError("InvestigationRunner not entered")
        return await self._orch.investigate(ctx)

    def investigate_streaming(self, ctx: InvestigationContext):
        if not self._orch:
            raise RuntimeError("InvestigationRunner not entered")
        return self._orch.investigate_streaming(ctx)
