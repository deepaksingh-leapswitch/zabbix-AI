from __future__ import annotations

from pydantic import HttpUrl, SecretStr

from zabbix_ai.admin import connections_store as cs
from zabbix_ai.config import (
    AutoInvestigateSettings,
    BudgetSettings,
    HostBillSettings,
    HostBriefingSettings,
    OAuthGoogleSettings,
    Settings,
    SlackSettings,
    ZabbixInstance,
    ZabbixUiSettings,
)
from zabbix_ai.memory import Memory


async def overlay_settings(settings: Settings, memory: Memory,
                            crypto_key: bytes) -> Settings:
    """Mutate settings in place to overlay DB-stored connection config.

    DB rows (when present) take precedence over file-based config so the
    admin UI can change things without editing /etc/zabbix-ai/*.
    """
    # Zabbix instances
    zabbix_rows = await cs.conn_list(memory, type_filter="zabbix")
    if zabbix_rows:
        instances: list[ZabbixInstance] = []
        for row in zabbix_rows:
            if not row["enabled"]:
                continue
            cfg = row["config"]
            tok = await cs.secret_get(
                memory, key=f"zabbix:{row['name']}:token",
                crypto_key=crypto_key,
            )
            if not tok:
                continue
            instances.append(ZabbixInstance(
                name=row["name"], url=HttpUrl(cfg["url"]),
                token_env=cfg.get("token_env", ""),
                token=SecretStr(tok),
            ))
        if instances:
            settings.zabbix_instances = instances

    # HostBill (singleton, name='primary')
    hb = await cs.conn_get(memory, type_="hostbill", name="primary")
    if hb and hb["enabled"]:
        api_id = await cs.secret_get(
            memory, key="hostbill:primary:api_id", crypto_key=crypto_key,
        )
        api_key = await cs.secret_get(
            memory, key="hostbill:primary:api_key", crypto_key=crypto_key,
        )
        if api_id and api_key:
            settings.hostbill = HostBillSettings(
                api_url=HttpUrl(hb["config"]["api_url"]),
                api_id_env=hb["config"].get("api_id_env", ""),
                api_key_env=hb["config"].get("api_key_env", ""),
                api_id=SecretStr(api_id),
                api_key=SecretStr(api_key),
            )

    # Anthropic API key (singleton)
    ak = await cs.secret_get(
        memory, key="anthropic:primary:api_key", crypto_key=crypto_key,
    )
    if ak:
        settings.anthropic_api_key = SecretStr(ak)

    # Slack (singleton)
    slack = await cs.conn_get(memory, type_="slack", name="primary")
    if slack and slack["enabled"]:
        bot = await cs.secret_get(
            memory, key="slack:primary:bot_token", crypto_key=crypto_key,
        )
        sig = await cs.secret_get(
            memory, key="slack:primary:signing_secret", crypto_key=crypto_key,
        )
        if bot and sig:
            settings.slack = SlackSettings(
                bot_token_env="", signing_secret_env="",
                default_instance=slack["config"].get("default_instance", ""),
                channel_allowlist=slack["config"].get("channel_allowlist", []),
                bot_token=SecretStr(bot),
                signing_secret=SecretStr(sig),
            )

    # OAuth Google (singleton)
    og = await cs.conn_get(memory, type_="oauth_google", name="primary")
    if og and og["enabled"]:
        cs_secret = await cs.secret_get(
            memory, key="oauth_google:primary:client_secret",
            crypto_key=crypto_key,
        )
        if cs_secret:
            settings.oauth_google = OAuthGoogleSettings(
                client_id=og["config"]["client_id"],
                client_secret_env="",
                allowed_email_domain=og["config"].get("allowed_email_domain", ""),
                default_role=og["config"].get("default_role", "viewer"),
                client_secret=SecretStr(cs_secret),
            )

    # Zabbix UI signing key (singleton)
    zui = await cs.secret_get(
        memory, key="zabbix_ui:primary:signing_key", crypto_key=crypto_key,
    )
    if zui:
        if settings.zabbix_ui is None:
            settings.zabbix_ui = ZabbixUiSettings(
                signing_key_env="", link_ttl_seconds=300,
            )
        settings.zabbix_ui.signing_key = SecretStr(zui)

    # Models & limits (singleton)
    sysconn = await cs.conn_get(memory, type_="system", name="defaults")
    if sysconn and sysconn["enabled"]:
        cfg = sysconn["config"]
        if cfg.get("default_model"):
            settings.default_model = cfg["default_model"]
        if cfg.get("summary_model"):
            settings.summary_model = cfg["summary_model"]
        if cfg.get("max_tool_calls"):
            settings.max_tool_calls = int(cfg["max_tool_calls"])
        if cfg.get("max_input_tokens"):
            settings.max_input_tokens = int(cfg["max_input_tokens"])
        if cfg.get("max_output_tokens"):
            settings.max_output_tokens = int(cfg["max_output_tokens"])
        # Host briefing settings
        if "host_briefing_enabled" in cfg:
            settings.host_briefing = HostBriefingSettings(
                enabled=bool(cfg["host_briefing_enabled"]),
                days=int(cfg.get("host_briefing_days",
                                  settings.host_briefing.days)),
                max_tokens=int(cfg.get("host_briefing_max_tokens",
                                        settings.host_briefing.max_tokens)),
            )
        elif "host_briefing_days" in cfg or "host_briefing_max_tokens" in cfg:
            settings.host_briefing = HostBriefingSettings(
                enabled=settings.host_briefing.enabled,
                days=int(cfg.get("host_briefing_days",
                                  settings.host_briefing.days)),
                max_tokens=int(cfg.get("host_briefing_max_tokens",
                                        settings.host_briefing.max_tokens)),
            )
        # Budget settings overlay — only touch settings.budget when at
        # least one budget_* key exists in the DB row, so a deployment
        # that hasn't enabled the dashboard widget still keeps its
        # file/default settings.budget.
        budget_keys = {"budget_daily_inr_cap", "budget_over_budget_action",
                       "budget_reset_hour_utc", "budget_usd_to_inr"}
        if budget_keys & set(cfg.keys()):
            settings.budget = BudgetSettings(
                daily_inr_cap=float(cfg.get("budget_daily_inr_cap",
                                             settings.budget.daily_inr_cap)),
                over_budget_action=str(cfg.get("budget_over_budget_action",
                                                settings.budget.over_budget_action)),
                reset_hour_utc=int(cfg.get("budget_reset_hour_utc",
                                            settings.budget.reset_hour_utc)),
                usd_to_inr=float(cfg.get("budget_usd_to_inr",
                                          settings.budget.usd_to_inr)),
            )
        # Auto-investigate overlay — only build a fresh AutoInvestigateSettings
        # when the admin has touched the form (any auto_investigate_* key
        # present). Hostgroups are stored as a CSV string in the DB to keep
        # the row a flat key→value map.
        ai_keys = {"auto_investigate_enabled", "auto_investigate_min_severity",
                   "auto_investigate_allowed_hostgroups",
                   "auto_investigate_slack_channel"}
        if ai_keys & set(cfg.keys()):
            base = settings.auto_investigate or AutoInvestigateSettings()
            hg_raw = cfg.get("auto_investigate_allowed_hostgroups", "")
            if isinstance(hg_raw, list):
                hostgroups = [str(g) for g in hg_raw if str(g).strip()]
            else:
                hostgroups = [g.strip() for g in str(hg_raw).split(",")
                              if g.strip()]
            slack_channel = (cfg.get("auto_investigate_slack_channel",
                                      base.slack_channel) or "") or None
            settings.auto_investigate = AutoInvestigateSettings(
                enabled=bool(cfg.get("auto_investigate_enabled", base.enabled)),
                webhook_secret_env=base.webhook_secret_env,
                min_severity=int(cfg.get("auto_investigate_min_severity",
                                          base.min_severity)),
                allowed_hostgroups=hostgroups,
                slack_channel=slack_channel,
            )
    return settings



async def resolve_oauth_google(memory, crypto_key, base):
    """Effective Google OAuth config for the auth/login layer.

    The DB overlay (admin UI Connections store) takes precedence; otherwise the
    static file config (``base``). Returns None if neither is configured. Any
    overlay error falls back to ``base`` so a bad row never breaks login.
    """
    try:
        if memory is not None and crypto_key is not None:
            og = await cs.conn_get(memory, type_="oauth_google", name="primary")
            if og and og.get("enabled"):
                secret = await cs.secret_get(
                    memory, key="oauth_google:primary:client_secret",
                    crypto_key=crypto_key)
                if secret and og["config"].get("client_id"):
                    return OAuthGoogleSettings(
                        client_id=og["config"]["client_id"],
                        client_secret_env="",
                        allowed_email_domain=og["config"].get("allowed_email_domain", ""),
                        default_role=og["config"].get("default_role", "viewer"),
                        client_secret=SecretStr(secret),
                    )
    except Exception:
        pass
    return base
