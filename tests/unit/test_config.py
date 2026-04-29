import pytest
from zabbix_ai.config import Settings, ZabbixInstance, load_settings

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

    assert s.anthropic_api_key == "sk-ant-test"
    assert len(s.zabbix_instances) == 2
    assert s.zabbix_instances[0].token == "tok-m"
    assert s.zabbix_instances[1].url == "https://dcmonitoring.leapswitch.com"
    assert s.sqlite_path == "/tmp/state.db"

def test_missing_anthropic_key_raises(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("zabbix_instances: []\nsqlite_path: /tmp/x\n")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        load_settings(cfg)

def test_missing_zabbix_token_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
zabbix_instances:
  - name: monitoring
    url: https://x
    token_env: NONEXISTENT_TOKEN
sqlite_path: /tmp/x
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(ValueError, match="NONEXISTENT_TOKEN"):
        load_settings(cfg)
