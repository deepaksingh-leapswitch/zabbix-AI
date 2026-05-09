from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr


class ZabbixInstance(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    name: str
    url: HttpUrl
    token_env: str
    token: SecretStr = SecretStr("")


class SlackSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    bot_token_env: str
    signing_secret_env: str
    default_instance: str = ""
    channel_allowlist: list[str] = Field(default_factory=list)
    bot_token: SecretStr = SecretStr("")
    signing_secret: SecretStr = SecretStr("")


class ZabbixUiSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    signing_key_env: str
    link_ttl_seconds: int = 300
    signing_key: SecretStr = SecretStr("")


class HostBillSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    api_url: HttpUrl
    api_id_env: str
    api_key_env: str
    api_id: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")


class AdminSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    session_secret_env: str
    bootstrap_admin_password_env: str = ""  # only used on first start if no users exist
    session_max_age_seconds: int = 28800   # 8h
    session_secret: SecretStr = SecretStr("")
    bootstrap_admin_password: SecretStr = SecretStr("")


class Settings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    zabbix_instances: list[ZabbixInstance] = Field(default_factory=list)
    sqlite_path: str = "/var/lib/zabbix-ai/state.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    anthropic_api_key: SecretStr = SecretStr("")
    default_model: str = "claude-sonnet-4-6"
    summary_model: str = "claude-haiku-4-5-20251001"
    max_tool_calls: int = 8
    max_input_tokens: int = 50_000
    max_output_tokens: int = 10_000
    slack: SlackSettings | None = None
    zabbix_ui: ZabbixUiSettings | None = None
    hostbill: HostBillSettings | None = None
    admin: AdminSettings | None = None


def load_settings(config_path: Path | str) -> Settings:
    raw = yaml.safe_load(Path(config_path).read_text()) or {}
    if not raw.get("zabbix_instances"):
        raise ValueError("config.yaml has no zabbix_instances — is the file empty or truncated?")
    s = Settings(**raw)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment")
    s.anthropic_api_key = SecretStr(api_key)
    for inst in s.zabbix_instances:
        tok = os.environ.get(inst.token_env)
        if not tok:
            raise ValueError(f"{inst.token_env} not set in environment")
        inst.token = SecretStr(tok)
    if s.slack is not None:
        bot = os.environ.get(s.slack.bot_token_env)
        if not bot:
            raise ValueError(f"{s.slack.bot_token_env} not set in environment")
        sec = os.environ.get(s.slack.signing_secret_env)
        if not sec:
            raise ValueError(f"{s.slack.signing_secret_env} not set in environment")
        s.slack.bot_token = SecretStr(bot)
        s.slack.signing_secret = SecretStr(sec)
    if s.zabbix_ui is not None:
        key = os.environ.get(s.zabbix_ui.signing_key_env)
        if not key:
            raise ValueError(f"{s.zabbix_ui.signing_key_env} not set in environment")
        s.zabbix_ui.signing_key = SecretStr(key)
    if s.hostbill is not None:
        api_id = os.environ.get(s.hostbill.api_id_env)
        if not api_id:
            raise ValueError(f"{s.hostbill.api_id_env} not set in environment")
        api_key = os.environ.get(s.hostbill.api_key_env)
        if not api_key:
            raise ValueError(f"{s.hostbill.api_key_env} not set in environment")
        s.hostbill.api_id = SecretStr(api_id)
        s.hostbill.api_key = SecretStr(api_key)
    if s.admin is not None:
        sec = os.environ.get(s.admin.session_secret_env)
        if not sec:
            raise ValueError(f"{s.admin.session_secret_env} not set in environment")
        s.admin.session_secret = SecretStr(sec)
        if s.admin.bootstrap_admin_password_env:
            bap = os.environ.get(s.admin.bootstrap_admin_password_env, "")
            s.admin.bootstrap_admin_password = SecretStr(bap)
    return s
