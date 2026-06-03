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
    # Optional separate write-role token, only used by zabbix_write for the
    # approval-gated 6-day monitoring disable. Empty ⇒ disable unavailable.
    write_token_env: str = ""
    write_token: SecretStr = SecretStr("")


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
    link_ttl_seconds: int = Field(default=300, ge=10, le=600)
    signing_key: SecretStr = SecretStr("")


class HostBillSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    api_url: HttpUrl
    api_id_env: str
    api_key_env: str
    api_id: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")


class HostBriefingSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    enabled: bool = True
    days: int = 30
    max_tokens: int = 2000


class AutoInvestigateSettings(BaseModel):
    """Settings for the Zabbix → webhook → auto-investigate path (v1.5).

    When ``enabled`` is true, a Zabbix action POSTs to
    ``/zabbix/auto-investigate`` on problem-open. The webhook authenticates
    the request via HMAC-SHA256 over the body keyed by the secret in
    ``webhook_secret_env``.
    """
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    enabled: bool = False
    # Name of the env var holding the shared HMAC secret. The actual value is
    # fetched lazily via the ``webhook_secret`` property so rotating the
    # secret in the environment doesn't require an app restart.
    webhook_secret_env: str = "ZABBIX_WEBHOOK_SECRET"
    # Zabbix trigger severities are 0..5 (5 = Disaster). Default 4 = High.
    min_severity: int = Field(default=4, ge=0, le=5)
    # Empty list ⇒ all host groups allowed.
    allowed_hostgroups: list[str] = Field(default_factory=list)
    # Free-form Slack channel id or "#name" (Slack accepts either). When unset,
    # the auto-investigate completes without a Slack post.
    slack_channel: str | None = None
    # When true (default), the webhook returns 202 immediately and runs
    # the investigation off the request path. False = legacy inline blocking.
    async_processing: bool = True

    @property
    def webhook_secret(self) -> SecretStr:
        return SecretStr(os.environ.get(self.webhook_secret_env, ""))


class TicketFlowSettings(BaseModel):
    """Auto ticket-raise + Slack follow-up flow (built on auto_investigate).

    Disabled by default. When enabled, a qualifying problem produces an
    AI-drafted HostBill ticket that a human approves in Slack before it is
    created, then an escalating follow-up loop chases it until a human
    replies, the problem recovers, or (at disable_after_days) monitoring is
    disabled via an approval-gated Zabbix write.
    """
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    enabled: bool = False
    # Only raise a ticket for problems at/above this Zabbix severity (0..5).
    ticket_min_severity: int = Field(default=4, ge=0, le=5)
    # Only raise a ticket when the Zabbix problem name matches one of
    # these case-insensitive regexes. Empty => all problems. Lets you
    # roll the flow out by problem type (start with ICMP, add disk later).
    trigger_name_patterns: list[str] = Field(default_factory=list)
    # Target for internal (non-customer) tickets.
    internal_department_id: int | None = None
    internal_client_id: int | None = None
    # For an internal ticket with no client_id, HostBill requires a
    # requester name + email on the ticket.
    internal_requester_name: str = "Zabbix RCA AI"
    internal_requester_email: str = ""
    # Escalating Slack-thread nudge cadence in minutes; the last value repeats.
    nudge_schedule_minutes: list[int] = Field(
        default_factory=lambda: [15, 30, 60, 60])
    # Stop nudging after this many days with no human reply (ticket stays open).
    quiet_after_days: int = Field(default=3, ge=1)
    # If the problem is still active after this many days, offer (Slack-approval)
    # to disable monitoring in Zabbix.
    disable_after_days: int = Field(default=6, ge=1)
    # Optional allowlist of Slack user ids allowed to click Approve / Disable.
    # Empty means anyone in the channel may approve.
    approver_slack_user_ids: list[str] = Field(default_factory=list)
    # Dry-run: post drafts to test_slack_channel, route every ticket to the
    # internal department, and never write to Zabbix. For safe rollout.
    dry_run: bool = False
    test_slack_channel: str | None = None


class BudgetSettings(BaseModel):
    """Daily Anthropic spend cap for v1.5.

    All values default to the no-op state (cap=0 means unlimited) so the
    field can be added to ``Settings`` without breaking any existing
    deployment that hasn't enabled it.
    """
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    daily_inr_cap: float = 0.0            # 0 = unlimited
    over_budget_action: Literal["haiku_only", "pause", "warn"] = "haiku_only"
    reset_hour_utc: int = Field(default=0, ge=0, le=23)
    usd_to_inr: float = 83.0


class AdminSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    session_secret_env: str
    bootstrap_admin_password_env: str = ""  # only used on first start if no users exist
    session_max_age_seconds: int = 28800   # 8h
    session_secret: SecretStr = SecretStr("")
    bootstrap_admin_password: SecretStr = SecretStr("")


class OAuthGoogleSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    client_id: str
    client_secret_env: str
    allowed_email_domain: str = ""    # e.g. "leapswitch.com" — restrict to this domain
    default_role: str = "viewer"       # role assigned to first-time SSO users
    client_secret: SecretStr = SecretStr("")


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
    oauth_google: OAuthGoogleSettings | None = None
    host_briefing: HostBriefingSettings = Field(default_factory=HostBriefingSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    auto_investigate: AutoInvestigateSettings | None = None
    ticket_flow: TicketFlowSettings | None = None


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
        if inst.write_token_env:
            inst.write_token = SecretStr(
                os.environ.get(inst.write_token_env, ""))
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
    if s.oauth_google is not None:
        sec = os.environ.get(s.oauth_google.client_secret_env)
        if not sec:
            raise ValueError(f"{s.oauth_google.client_secret_env} not set in environment")
        s.oauth_google.client_secret = SecretStr(sec)
    return s
