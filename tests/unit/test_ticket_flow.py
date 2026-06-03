from dataclasses import dataclass
from pathlib import Path

import pytest

from zabbix_ai.config import Settings, TicketFlowSettings
from zabbix_ai.memory import Memory
from zabbix_ai.services import ticket_flow as tf


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "test.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()


@dataclass
class FakeLink:
    is_linked: bool
    hostbill_client_id: int | None
    confidence: str


async def _seed_investigation(memory, summary="RCA: disk full on /var") -> int:
    await memory.execute(
        "INSERT INTO investigations (source, started_at, summary) VALUES (?,?,?)",
        ("auto_webhook", "2026-06-03T00:00:00+00:00", summary),
    )
    row = await memory.fetchone("SELECT last_insert_rowid()")
    return int(row[0])


async def _mk_incident(memory, **over):
    inv = await _seed_investigation(memory)
    kw = dict(
        instance="monitoring", eventid=9001, hostid=42, hostname="web-1",
        severity=5, trigger_name="Disk full", problem_type="agent",
        investigation_id=inv, ticket_kind="internal", hostbill_client_id=None,
        slack_channel="C1",
    )
    kw.update(over)
    return await tf.create_incident(memory, **kw)


async def test_create_incident_idempotent(memory):
    a = await _mk_incident(memory)
    b = await _mk_incident(memory)  # same eventid → same row
    assert a == b
    n = (await memory.fetchone("SELECT COUNT(*) FROM incidents"))[0]
    assert n == 1


async def test_create_incident_hostmode_not_deduped(memory):
    a = await _mk_incident(memory, eventid=None)
    b = await _mk_incident(memory, eventid=None)
    assert a != b
    n = (await memory.fetchone("SELECT COUNT(*) FROM incidents"))[0]
    assert n == 2


def test_resolve_target_internal_when_unlinked():
    cfg = TicketFlowSettings(internal_department_id=3, internal_client_id=7)
    kind, client, dept = tf.resolve_ticket_target(cfg, None)
    assert (kind, client, dept) == ("internal", 7, 3)


def test_resolve_target_internal_when_low_confidence():
    cfg = TicketFlowSettings(internal_department_id=3)
    link = FakeLink(is_linked=True, hostbill_client_id=88, confidence="low")
    assert tf.resolve_ticket_target(cfg, link)[0] == "internal"


def test_resolve_target_customer_when_linked():
    cfg = TicketFlowSettings(internal_department_id=3)
    link = FakeLink(is_linked=True, hostbill_client_id=88, confidence="high")
    kind, client, dept = tf.resolve_ticket_target(cfg, link)
    assert (kind, client) == ("customer", 88)


def test_resolve_target_dryrun_forces_internal():
    cfg = TicketFlowSettings(dry_run=True, internal_client_id=7)
    link = FakeLink(is_linked=True, hostbill_client_id=88, confidence="high")
    assert tf.resolve_ticket_target(cfg, link)[0] == "internal"


def test_compute_next_nudge_clamps():
    cfg = TicketFlowSettings(nudge_schedule_minutes=[15, 30, 60])
    # Should not raise and should produce ISO strings for counts past the end.
    for c in (0, 1, 2, 5, 99):
        assert "T" in tf.compute_next_nudge_at(cfg, c)


async def test_approve_dry_run_creates_state_no_ticket(memory):
    settings = Settings(ticket_flow=TicketFlowSettings(
        enabled=True, dry_run=True, internal_department_id=2))
    iid = await _mk_incident(memory)
    res = await tf.approve_and_create_ticket(
        settings=settings, memory=memory, incident_id=iid, approver="U1")
    assert res["ok"] and res["dry_run"] and res["ticket_id"] is None
    inc = await tf.get_incident(memory, iid)
    assert inc["state"] == "created"
    assert inc["approved_by"] == "U1"
    assert inc["next_nudge_at"]  # follow-up armed
    # audit row written
    n = (await memory.fetchone(
        "SELECT COUNT(*) FROM audit_log WHERE event_type='ticket_created'"))[0]
    assert n == 1


async def test_approve_rejects_non_drafted(memory):
    settings = Settings(ticket_flow=TicketFlowSettings(enabled=True, dry_run=True))
    iid = await _mk_incident(memory)
    await tf.approve_and_create_ticket(
        settings=settings, memory=memory, incident_id=iid, approver="U1")
    again = await tf.approve_and_create_ticket(
        settings=settings, memory=memory, incident_id=iid, approver="U2")
    assert not again["ok"] and "already" in again["msg"]


async def test_approve_unknown_incident(memory):
    settings = Settings(ticket_flow=TicketFlowSettings(enabled=True, dry_run=True))
    res = await tf.approve_and_create_ticket(
        settings=settings, memory=memory, incident_id=123456, approver="U1")
    assert not res["ok"]


async def test_discard(memory):
    iid = await _mk_incident(memory)
    res = await tf.discard_incident(memory, iid, by="U9")
    assert res["ok"]
    inc = await tf.get_incident(memory, iid)
    assert inc["state"] == "discarded"


# ── trigger-name scope filter (rollout by problem type) ──────────────────────

def test_patterns_empty_matches_all():
    cfg = TicketFlowSettings()
    assert tf.matches_trigger_patterns(cfg, "anything at all")
    assert tf.matches_trigger_patterns(cfg, "")


def test_icmp_patterns_match_icmp_only():
    cfg = TicketFlowSettings(trigger_name_patterns=["unavailable by icmp", "icmp ping"])
    assert tf.matches_trigger_patterns(cfg, "Unavailable by ICMP ping")
    assert tf.matches_trigger_patterns(cfg, "web-1: Unavailable by ICMP ping")
    # disk problems are NOT in scope yet
    assert not tf.matches_trigger_patterns(cfg, "/: Disk space is low (used > 90%)")
    assert not tf.matches_trigger_patterns(cfg, "High CPU utilization")


def test_adding_disk_patterns_extends_scope():
    cfg = TicketFlowSettings(trigger_name_patterns=[
        "unavailable by icmp", r"disk space", r"filesystem.*(low|full)"])
    assert tf.matches_trigger_patterns(cfg, "Unavailable by ICMP ping")
    assert tf.matches_trigger_patterns(cfg, "/: Disk space is low (used > 90%)")
    assert tf.matches_trigger_patterns(cfg, "Filesystem /var is full")


def test_bad_regex_degrades_to_substring():
    cfg = TicketFlowSettings(trigger_name_patterns=["(unbalanced"])  # invalid regex
    assert tf.matches_trigger_patterns(cfg, "an (unbalanced thing")
    assert not tf.matches_trigger_patterns(cfg, "something else")
