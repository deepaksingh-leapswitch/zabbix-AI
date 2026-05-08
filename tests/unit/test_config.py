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
