from __future__ import annotations
import os
from pathlib import Path
import yaml
from pydantic import BaseModel, Field


class ZabbixInstance(BaseModel):
    name: str
    url: str
    token_env: str
    token: str = ""


class Settings(BaseModel):
    zabbix_instances: list[ZabbixInstance] = Field(default_factory=list)
    sqlite_path: str = "/var/lib/zabbix-ai/state.db"
    log_level: str = "INFO"
    anthropic_api_key: str = ""
    default_model: str = "claude-sonnet-4-6"
    summary_model: str = "claude-haiku-4-5-20251001"
    max_tool_calls: int = 8
    max_input_tokens: int = 50_000
    max_output_tokens: int = 10_000


def load_settings(config_path: Path | str) -> Settings:
    raw = yaml.safe_load(Path(config_path).read_text()) or {}
    s = Settings(**raw)
    s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not s.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment")
    for inst in s.zabbix_instances:
        tok = os.environ.get(inst.token_env)
        if not tok:
            raise ValueError(f"{inst.token_env} not set in environment")
        inst.token = tok
    return s
