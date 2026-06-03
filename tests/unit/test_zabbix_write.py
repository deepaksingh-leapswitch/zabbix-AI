from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from zabbix_ai.config import (Settings, SlackSettings, TicketFlowSettings,
                              ZabbixInstance)
from zabbix_ai.memory import Memory
from zabbix_ai.services import followup_worker as fw
from zabbix_ai.services import ticket_flow as tf
from zabbix_ai.services import zabbix_write as zw


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()


class ClassifyZabbix:
    """Fake Zabbix read client for classify_problem + event_is_resolved."""

    def __init__(self, item_keys, hostid="50", triggerid="900", resolved=False):
        self.item_keys = item_keys
        self.hostid = hostid
        self.triggerid = triggerid
        self.resolved = resolved
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "event.get":
            if params.get("output") == ["eventid", "r_clock", "value"]:
                return [{"r_clock": "123" if self.resolved else "0",
                         "value": "0" if self.resolved else "1"}]
            return [{"objectid": self.triggerid, "object": "0",
                     "hosts": [{"hostid": self.hostid}]}]
        if method == "trigger.get":
            return [{"triggerid": self.triggerid,
                     "items": [{"key_": k} for k in self.item_keys]}]
        if method in ("host.update", "trigger.update", "maintenance.create"):
            return {"ok": True}
        return []

    async def aclose(self):
        pass


def _settings_with_write(write=True, dry_run=False):
    s = Settings(
        zabbix_instances=[ZabbixInstance(
            name="monitoring", url="https://z.test", token_env="T",
            write_token_env="TW")],
        slack=SlackSettings(bot_token_env="B", signing_secret_env="S"),
        ticket_flow=TicketFlowSettings(enabled=True, dry_run=dry_run,
                                       disable_after_days=6),
    )
    s.slack.bot_token = SecretStr("x")
    s.slack.signing_secret = SecretStr("y")
    s.zabbix_instances[0].token = SecretStr("read-tok")
    s.zabbix_instances[0].write_token = SecretStr("write-tok" if write else "")
    return s


async def test_classify_icmp():
    z = ClassifyZabbix(item_keys=["icmpping", "icmppingsec"])
    scope, target = await zw.classify_problem(z, eventid=1)
    assert scope == "icmp" and target["hostid"] == "50"


async def test_classify_agent():
    z = ClassifyZabbix(item_keys=["vfs.fs.size[/,pused]"])
    scope, target = await zw.classify_problem(z, eventid=1)
    assert scope == "agent" and target["triggerid"] == "900"


async def test_classify_other():
    z = ClassifyZabbix(item_keys=["snmp.custom.thing"])
    scope, _ = await zw.classify_problem(z, eventid=1)
    assert scope == "other"


def test_write_capability():
    assert zw.has_write_capability(_settings_with_write(write=True), "monitoring")
    assert not zw.has_write_capability(_settings_with_write(write=False), "monitoring")
    assert zw.build_write_client(_settings_with_write(False), "monitoring", None) is None


async def _pending_incident(memory, eventid=1):
    iid = await tf.create_incident(
        memory, instance="monitoring", eventid=eventid, hostid=50,
        hostname="ping-host", severity=5, trigger_name="Unavailable by ICMP",
        problem_type="other", investigation_id=None, ticket_kind="internal",
        hostbill_client_id=None, slack_channel="C1")
    await tf.update_incident(memory, iid, state="disable_pending",
                             disable_scope="icmp")
    return iid


async def test_perform_disable_icmp_disables_host(memory, monkeypatch):
    s = _settings_with_write()
    z = ClassifyZabbix(item_keys=["icmpping"])
    monkeypatch.setattr(zw, "build_write_client", lambda *a, **k: z)
    iid = await _pending_incident(memory)
    res = await zw.perform_disable(settings=s, memory=memory,
                                   incident_id=iid, approver="U1")
    assert res["ok"]
    assert any(c[0] == "host.update" and c[1]["status"] == 1 for c in z.calls)
    inc = await tf.get_incident(memory, iid)
    assert inc["state"] == "disabled" and inc["disable_approved_by"] == "U1"
    n = (await memory.fetchone(
        "SELECT COUNT(*) FROM audit_log WHERE event_type='zabbix_disable_host'"))[0]
    assert n == 1


async def test_perform_disable_agent_disables_trigger(memory, monkeypatch):
    s = _settings_with_write()
    z = ClassifyZabbix(item_keys=["system.cpu.load"])
    monkeypatch.setattr(zw, "build_write_client", lambda *a, **k: z)
    iid = await _pending_incident(memory)
    res = await zw.perform_disable(settings=s, memory=memory,
                                   incident_id=iid, approver="U1")
    assert res["ok"]
    assert any(c[0] == "trigger.update" and c[1]["status"] == 1 for c in z.calls)


async def test_perform_disable_no_token(memory):
    s = _settings_with_write(write=False)
    iid = await _pending_incident(memory)
    res = await zw.perform_disable(settings=s, memory=memory,
                                   incident_id=iid, approver="U1")
    assert not res["ok"] and "write token" in res["msg"]


async def test_perform_disable_rejects_non_pending(memory, monkeypatch):
    s = _settings_with_write()
    monkeypatch.setattr(zw, "build_write_client",
                        lambda *a, **k: ClassifyZabbix(["icmpping"]))
    iid = await tf.create_incident(
        memory, instance="monitoring", eventid=2, hostid=50, hostname="h",
        severity=5, trigger_name="t", problem_type="other",
        investigation_id=None, ticket_kind="internal", hostbill_client_id=None,
        slack_channel="C1")  # state stays 'drafted'
    res = await zw.perform_disable(settings=s, memory=memory,
                                   incident_id=iid, approver="U1")
    assert not res["ok"]


async def test_dismiss_disable(memory):
    iid = await _pending_incident(memory)
    await zw.dismiss_disable(memory, iid, by="U2")
    inc = await tf.get_incident(memory, iid)
    assert inc["state"] == "quiet" and inc["disable_scope"] == "dismissed"


# ── arming via process_incident at 6+ days ───────────────────────────────────

class FakeSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, *, channel, text="", thread_ts=None, blocks=None):
        self.posts.append({"text": text, "blocks": blocks})
        return {"ts": "1.1"}

    async def aclose(self):
        pass


async def _old_created_incident(memory):
    iid = await tf.create_incident(
        memory, instance="monitoring", eventid=1, hostid=50,
        hostname="ping-host", severity=5, trigger_name="ICMP",
        problem_type="other", investigation_id=None, ticket_kind="internal",
        hostbill_client_id=None, slack_channel="C1")
    old = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    await tf.update_incident(memory, iid, state="quiet", slack_thread_ts="1.0",
                             created_at=old, baseline_reply_count=0)
    return iid


async def test_arm_disable_posts_and_sets_pending(memory):
    s = _settings_with_write()
    iid = await _old_created_incident(memory)
    inc = await tf.get_incident(memory, iid)
    slack = FakeSlack()
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc,
        zabbix=ClassifyZabbix(item_keys=["icmpping"]),
        slack=slack, hostbill=None, now=datetime.now(UTC))
    assert action == "disable_armed"
    out = await tf.get_incident(memory, iid)
    assert out["state"] == "disable_pending" and out["disable_scope"] == "icmp"
    assert slack.posts and slack.posts[0]["blocks"]


async def test_arm_disable_unavailable_without_write_token(memory):
    s = _settings_with_write(write=False)
    iid = await _old_created_incident(memory)
    inc = await tf.get_incident(memory, iid)
    slack = FakeSlack()
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc,
        zabbix=ClassifyZabbix(item_keys=["icmpping"]),
        slack=slack, hostbill=None, now=datetime.now(UTC))
    assert action == "disable_armed"
    out = await tf.get_incident(memory, iid)
    assert out["disable_scope"] == "unavailable"
    assert any("unavailable" in p["text"] for p in slack.posts)
