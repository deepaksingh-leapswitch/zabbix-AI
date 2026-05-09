import pytest
from pydantic import ValidationError

from zabbix_ai.config import load_settings


def test_load_settings_reads_yaml_and_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://monitoring.leapswitch.com
    token_env: MONITORING_TOKEN
  - name: dcmonitoring
    url: https://dcmonitoring.leapswitch.com
    token_env: DCMON_TOKEN
sqlite_path: /tmp/state.db
log_level: INFO
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("MONITORING_TOKEN", "tok-m")
    monkeypatch.setenv("DCMON_TOKEN", "tok-d")

    s = load_settings(cfg)

    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test"
    assert len(s.zabbix_instances) == 2
    assert s.zabbix_instances[0].token.get_secret_value() == "tok-m"
    assert str(s.zabbix_instances[1].url).rstrip("/") == "https://dcmonitoring.leapswitch.com"
    assert s.sqlite_path == "/tmp/state.db"


def test_missing_anthropic_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
""")
    monkeypatch.setenv("TOK", "tok")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        load_settings(cfg)


def test_missing_zabbix_token_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: NONEXISTENT_TOKEN
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(ValueError, match="NONEXISTENT_TOKEN"):
        load_settings(cfg)


def test_empty_yaml_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(ValueError, match="empty or truncated"):
        load_settings(cfg)


def test_unknown_yaml_key_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
unknown_key: foo
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    with pytest.raises(ValidationError):
        load_settings(cfg)


def test_slack_settings_loaded(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
slack:
  bot_token_env: SLACK_BOT_TOKEN
  signing_secret_env: SLACK_SIGNING_SECRET
  default_instance: monitoring
  channel_allowlist:
    - C111
    - C222
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")

    s = load_settings(cfg)
    assert s.slack is not None
    assert s.slack.bot_token.get_secret_value() == "xoxb-test"
    assert s.slack.signing_secret.get_secret_value() == "shh"
    assert s.slack.default_instance == "monitoring"
    assert s.slack.channel_allowlist == ["C111", "C222"]


def test_slack_section_optional(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    s = load_settings(cfg)
    assert s.slack is None


def test_slack_missing_token_env_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
slack:
  bot_token_env: SLACK_BOT_TOKEN
  signing_secret_env: SLACK_SIGNING_SECRET
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
        load_settings(cfg)


def test_zabbix_ui_settings_loaded(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
zabbix_ui:
  signing_key_env: URL_SIGNING_KEY
  link_ttl_seconds: 600
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("URL_SIGNING_KEY", "this-is-a-32-byte-key-or-more-pls")
    s = load_settings(cfg)
    assert s.zabbix_ui is not None
    assert s.zabbix_ui.signing_key.get_secret_value().startswith("this-is")
    assert s.zabbix_ui.link_ttl_seconds == 600


def test_zabbix_ui_section_optional(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    assert load_settings(cfg).zabbix_ui is None


def test_zabbix_ui_missing_key_env_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
zabbix_ui:
  signing_key_env: NOPE_KEY
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(ValueError, match="NOPE_KEY"):
        load_settings(cfg)


def test_hostbill_settings_loaded(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
hostbill:
  api_url: https://billing.test/admin/api.php
  api_id_env: HOSTBILL_API_ID
  api_key_env: HOSTBILL_API_KEY
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("HOSTBILL_API_ID", "id-1")
    monkeypatch.setenv("HOSTBILL_API_KEY", "key-1")
    s = load_settings(cfg)
    assert s.hostbill is not None
    assert s.hostbill.api_id.get_secret_value() == "id-1"
    assert s.hostbill.api_key.get_secret_value() == "key-1"
    assert str(s.hostbill.api_url).startswith("https://billing.test")


def test_hostbill_section_optional(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    assert load_settings(cfg).hostbill is None


def test_hostbill_missing_api_id_env_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
hostbill:
  api_url: https://billing.test/admin/api.php
  api_id_env: NOPE_ID
  api_key_env: HOSTBILL_API_KEY
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("HOSTBILL_API_KEY", "k")
    monkeypatch.delenv("NOPE_ID", raising=False)
    with pytest.raises(ValueError, match="NOPE_ID"):
        load_settings(cfg)


def test_admin_settings_loaded(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
admin:
  session_secret_env: SESSION_SECRET
  bootstrap_admin_password_env: BOOTSTRAP_ADMIN_PASSWORD
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    monkeypatch.setenv("SESSION_SECRET", "32-bytes-of-random-secret-please")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "first-time-pw")
    s = load_settings(cfg)
    assert s.admin is not None
    assert s.admin.session_secret.get_secret_value().startswith("32-")
    assert s.admin.bootstrap_admin_password.get_secret_value() == "first-time-pw"


def test_admin_section_optional(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x.test
    token_env: TOK
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOK", "tok")
    assert load_settings(cfg).admin is None
