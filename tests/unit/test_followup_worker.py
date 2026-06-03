from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zabbix_ai.config import Settings, SlackSettings, TicketFlowSettings
from zabbix_ai.memory import Memory
from zabbix_ai.services import followup_worker as fw
from zabbix_ai.services import ticket_flow as tf


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()


def _settings(dry_run=False):
    from pydantic import SecretStr
    s = Settings(
        slack=SlackSettings(bot_token_env="B", signing_secret_env="S"),
        ticket_flow=TicketFlowSettings(
            enabled=True, dry_run=dry_run, quiet_after_days=3,
            disable_after_days=6, nudge_schedule_minutes=[15, 30, 60]),
    )
    s.slack.bot_token = SecretStr("x")
    s.slack.signing_secret = SecretStr("y")
    return s


class FakeSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, *, channel, text="", thread_ts=None, blocks=None):
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ts": "1.1"}

    async def aclose(self):
        pass


class FakeZabbix:
    def __init__(self, resolved=False):
        self.resolved = resolved

    async def call(self, method, params):
        if self.resolved:
            return [{"eventid": "1", "r_clock": "123456", "value": "0"}]
        return [{"eventid": "1", "r_clock": "0", "value": "1"}]


class FakeHostBill:
    def __init__(self, reply_count=0):
        self.reply_count = reply_count
        self.closed = []

    async def get_ticket_reply_count(self, ticket_id):
        return self.reply_count

    async def close_ticket(self, *, ticket_id, body=""):
        self.closed.append(ticket_id)

    async def add_ticket_reply(self, *, ticket_id, body,
                               status_change=None, reply_type="Admin"):
        pass


async def _make_created_incident(memory, *, created_at=None, next_nudge_at=None,
                                  ticket_id=111, baseline=0, eventid=1):
    iid = await tf.create_incident(
        memory, instance="monitoring", eventid=eventid, hostid=5,
        hostname="web-1", severity=5, trigger_name="Disk full",
        problem_type="agent", investigation_id=None, ticket_kind="internal",
        hostbill_client_id=None, slack_channel="C1")
    await tf.update_incident(
        memory, iid, state="created", hostbill_ticket_id=ticket_id,
        baseline_reply_count=baseline, slack_thread_ts="1.0",
        created_at=created_at or fw._now_iso(),
        next_nudge_at=next_nudge_at or fw._now_iso())
    return iid


async def test_resolution_closes_ticket(memory):
    s = _settings()
    iid = await _make_created_incident(memory)
    inc = await tf.get_incident(memory, iid)
    hb = FakeHostBill()
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc, zabbix=FakeZabbix(resolved=True),
        slack=FakeSlack(), hostbill=hb, now=datetime.now(UTC))
    assert action == "resolved"
    assert hb.closed == [111]
    assert (await tf.get_incident(memory, iid))["state"] == "resolved"


async def test_reply_hands_off(memory):
    s = _settings()
    iid = await _make_created_incident(memory, baseline=1)
    inc = await tf.get_incident(memory, iid)
    slack = FakeSlack()
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc, zabbix=FakeZabbix(resolved=False),
        slack=slack, hostbill=FakeHostBill(reply_count=2), now=datetime.now(UTC))
    assert action == "handed_off"
    assert (await tf.get_incident(memory, iid))["state"] == "handed_off"
    assert any("take it forward" in p["text"] for p in slack.posts)


async def test_nudge_when_due(memory):
    s = _settings()
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    iid = await _make_created_incident(memory, next_nudge_at=past)
    inc = await tf.get_incident(memory, iid)
    slack = FakeSlack()
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc, zabbix=FakeZabbix(),
        slack=slack, hostbill=FakeHostBill(reply_count=0), now=datetime.now(UTC))
    assert action == "nudged"
    out = await tf.get_incident(memory, iid)
    assert out["state"] == "awaiting_reply"
    assert out["nudge_count"] == 1
    assert len(slack.posts) == 1


async def test_not_nudged_before_due(memory):
    s = _settings()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    iid = await _make_created_incident(memory, next_nudge_at=future)
    inc = await tf.get_incident(memory, iid)
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc, zabbix=FakeZabbix(),
        slack=FakeSlack(), hostbill=FakeHostBill(reply_count=0),
        now=datetime.now(UTC))
    assert action == "noop"


async def test_goes_quiet_after_3_days(memory):
    s = _settings()
    old = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    iid = await _make_created_incident(memory, created_at=old)
    inc = await tf.get_incident(memory, iid)
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc, zabbix=FakeZabbix(),
        slack=FakeSlack(), hostbill=FakeHostBill(reply_count=0),
        now=datetime.now(UTC))
    assert action == "quiet"
    assert (await tf.get_incident(memory, iid))["state"] == "quiet"


async def test_six_days_no_write_token_unavailable(memory):
    # No Zabbix write token configured -> 6-day backstop posts an alert-only
    # "unavailable" notice and arms (so it does not re-post each cycle).
    s = _settings()
    old = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    iid = await _make_created_incident(memory, created_at=old)
    inc = await tf.get_incident(memory, iid)
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc, zabbix=FakeZabbix(),
        slack=FakeSlack(), hostbill=FakeHostBill(reply_count=0),
        now=datetime.now(UTC))
    assert action == "disable_armed"
    assert (await tf.get_incident(memory, iid))["disable_scope"] == "unavailable"


async def test_dry_run_skips_reply_check_but_resolves(memory):
    s = _settings(dry_run=True)
    iid = await _make_created_incident(memory, ticket_id=None)
    inc = await tf.get_incident(memory, iid)
    # No hostbill in dry-run; resolution still detected via Zabbix.
    action = await fw.process_incident(
        settings=s, memory=memory, inc=inc, zabbix=FakeZabbix(resolved=True),
        slack=FakeSlack(), hostbill=None, now=datetime.now(UTC))
    assert action == "resolved"


async def test_poll_followups_processes_active(memory):
    s = _settings(dry_run=True)
    await _make_created_incident(memory, ticket_id=None)
    # No Zabbix clients passed → resolution check skipped, but pass still runs.
    n = await fw.poll_followups(s, memory, clients={})
    assert n == 1
