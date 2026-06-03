from pathlib import Path

import pytest

from zabbix_ai.config import Settings, TicketFlowSettings
from zabbix_ai.memory import Memory
from zabbix_ai.services import ticket_flow as tf


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "t.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()


async def _mk(memory, state="drafted"):
    iid = await tf.create_incident(
        memory, instance="monitoring", eventid=900, hostid=1, hostname="h",
        severity=5, trigger_name="t", problem_type="other",
        investigation_id=None, ticket_kind="internal", hostbill_client_id=None,
        slack_channel="C1")
    if state != "drafted":
        await tf.update_incident(memory, iid, state=state)
    return iid


# ── claim_transition ─────────────────────────────────────────────────────────

async def test_claim_transition_wins_once(memory):
    iid = await _mk(memory)
    assert await tf.claim_transition(memory, iid, "drafted", "approving") is True
    # second attempt from the now-stale 'drafted' loses
    assert await tf.claim_transition(memory, iid, "drafted", "approving") is False
    inc = await tf.get_incident(memory, iid)
    assert inc["state"] == "approving"


async def test_claim_transition_sets_extra_columns(memory):
    iid = await _mk(memory)
    ok = await tf.claim_transition(memory, iid, "drafted", "created",
                                   approved_by="U1")
    assert ok
    inc = await tf.get_incident(memory, iid)
    assert inc["state"] == "created" and inc["approved_by"] == "U1"


async def test_claim_transition_wrong_from_state(memory):
    iid = await _mk(memory, state="quiet")
    assert await tf.claim_transition(memory, iid, "drafted", "approving") is False


# ── approve idempotency under concurrency ────────────────────────────────────

async def test_double_approve_creates_once(memory):
    settings = Settings(ticket_flow=TicketFlowSettings(enabled=True, dry_run=True))
    iid = await _mk(memory)
    r1 = await tf.approve_and_create_ticket(
        settings=settings, memory=memory, incident_id=iid, approver="U1")
    r2 = await tf.approve_and_create_ticket(
        settings=settings, memory=memory, incident_id=iid, approver="U2")
    assert r1["ok"] is True
    assert r2["ok"] is False  # second approval is rejected
    inc = await tf.get_incident(memory, iid)
    assert inc["state"] == "created" and inc["approved_by"] == "U1"


# ── runner public API (no I/O in __init__) ───────────────────────────────────

def test_runner_public_api():
    from unittest.mock import MagicMock
    from zabbix_ai.services.investigation_runner import InvestigationRunner
    r = InvestigationRunner(Settings())
    r._zabbix_clients = {"monitoring": "ZC"}
    r._mem = "MEM"
    orch = MagicMock()
    r._orch = orch
    assert r.memory == "MEM"
    assert r.client("monitoring") == "ZC"
    assert r.client("nope") is None
    assert r.has_client("monitoring") is True
    assert r.has_client("nope") is False
    r.set_model("claude-x")
    assert orch.model == "claude-x"
