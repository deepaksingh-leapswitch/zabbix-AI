"""Unit tests for zabbix_ai.services.outcome_inference."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zabbix_ai.memory import Memory
from zabbix_ai.services.outcome_inference import (
    _good_delta,
    _metric_for_pattern,
    _pick_sample_after,
    _pick_sample_before,
    infer_outcome,
)

_MIGRATIONS = Path(__file__).resolve().parent.parent.parent / "migrations"


@pytest.fixture
async def memory(tmp_path):
    m = Memory(tmp_path / "outcome.db")
    await m.connect()
    await m.run_migrations(_MIGRATIONS)
    yield m
    await m.close()


class _FakeZabbixClient:
    """Stand-in for ZabbixClient.get_history. Returns whatever we put in."""

    def __init__(self, history: dict[str, list[dict]]):
        self.history = history
        self.calls: list[tuple[int, list[str], int]] = []

    async def get_history(self, hostid: int, keys: list[str],
                           range_seconds: int = 3600) -> dict:
        self.calls.append((hostid, keys, range_seconds))
        return self.history


async def _seed_investigation(memory: Memory, *, summary: str,
                               resolution_at: datetime,
                               started_at: datetime | None = None,
                               hostid: int = 101) -> int:
    started_at = started_at or (resolution_at - timedelta(hours=2))
    await memory.execute(
        """INSERT INTO investigations
           (source, started_at, hostid, hostname, summary,
            resolution_at, resolution_source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("cli", started_at.isoformat(), hostid, "h1", summary,
         resolution_at.isoformat(), "manual"),
    )
    row = await memory.fetchone("SELECT last_insert_rowid()")
    return int(row[0])


# ─── helpers ─────────────────────────────────────────────────────────────


def test_metric_for_pattern_disk():
    res = _metric_for_pattern("sig", "Disk space C: at 92%")
    assert res is not None
    assert res[0].startswith("vfs.fs.size")
    assert res[1] == "down_good"


def test_metric_for_pattern_memory():
    res = _metric_for_pattern("sig", "Memory available is low")
    assert res is not None
    assert "vm.memory" in res[0]
    assert res[1] == "up_good"


def test_metric_for_pattern_cpu():
    res = _metric_for_pattern("sig", "high CPU utilization on host")
    assert res is not None
    assert res[0].startswith("system.cpu.util")
    assert res[1] == "down_good"


def test_metric_for_pattern_fallback_key_token():
    res = _metric_for_pattern("sig", "agent.ping check failed earlier")
    assert res is not None
    assert res[0].startswith("agent.")


def test_metric_for_pattern_returns_none_when_no_signal():
    res = _metric_for_pattern("sig", "Some prose about nothing relevant")
    assert res is None


def test_pick_sample_before_returns_closest_prior():
    samples = [
        {"clock": 100, "value": "1"},
        {"clock": 200, "value": "2"},
        {"clock": 300, "value": "3"},
    ]
    assert _pick_sample_before(samples, 250) == 2.0
    assert _pick_sample_before(samples, 100) is None  # strictly less


def test_pick_sample_after_returns_closest_at_or_after():
    samples = [
        {"clock": 100, "value": "1"},
        {"clock": 200, "value": "2"},
        {"clock": 300, "value": "3"},
    ]
    assert _pick_sample_after(samples, 150) == 2.0
    assert _pick_sample_after(samples, 200) == 2.0
    assert _pick_sample_after(samples, 1000) is None


def test_good_delta_down_recovery():
    # 92 → 68 = -26% (down_good ⇒ recovered)
    assert _good_delta(92.0, 68.0, direction="down_good") is True
    # 92 → 88 = -4% (down_good ⇒ NOT enough)
    assert _good_delta(92.0, 88.0, direction="down_good") is False


def test_good_delta_up_recovery():
    assert _good_delta(100.0, 130.0, direction="up_good") is True
    assert _good_delta(100.0, 105.0, direction="up_good") is False


# ─── infer_outcome happy paths ───────────────────────────────────────────


async def test_disk_pattern_with_metric_drop_marks_recovered(memory):
    res_at = datetime.now(UTC) - timedelta(hours=4)
    inv_id = await _seed_investigation(
        memory,
        summary="Disk space C: at 92% — clear logs in /var/log",
        resolution_at=res_at,
    )
    res_clock = int(res_at.timestamp())
    fake = _FakeZabbixClient(history={
        "vfs.fs.size[C:,pused]": [
            {"clock": res_clock - 60, "value": "92.6"},
            {"clock": res_clock + 2 * 3600 + 5, "value": "68.1"},
        ],
    })
    out = await infer_outcome(memory, fake, investigation_id=inv_id)
    assert out is not None
    assert out["recovered"] is True
    assert out["metric"] == "vfs.fs.size[C:,pused]"
    assert abs(out["before"] - 92.6) < 0.01
    assert abs(out["after"] - 68.1) < 0.01
    assert out["direction"] == "down_good"
    # Row was persisted.
    row = await memory.fetchone(
        "SELECT outcome_inferred FROM investigations WHERE id=?", (inv_id,),
    )
    saved = json.loads(row[0])
    assert saved["recovered"] is True


async def test_disk_pattern_no_drop_marks_not_recovered(memory):
    res_at = datetime.now(UTC) - timedelta(hours=4)
    inv_id = await _seed_investigation(
        memory,
        summary="Disk space C: still 92%",
        resolution_at=res_at,
    )
    res_clock = int(res_at.timestamp())
    fake = _FakeZabbixClient(history={
        "vfs.fs.size[C:,pused]": [
            {"clock": res_clock - 60, "value": "92.0"},
            {"clock": res_clock + 2 * 3600 + 5, "value": "91.5"},
        ],
    })
    out = await infer_outcome(memory, fake, investigation_id=inv_id)
    assert out is not None
    assert out["recovered"] is False
    # Persistence still happens — null only means "not yet analysed".
    row = await memory.fetchone(
        "SELECT outcome_inferred FROM investigations WHERE id=?", (inv_id,),
    )
    assert row[0] is not None


async def test_no_matching_metric_returns_none(memory):
    res_at = datetime.now(UTC) - timedelta(hours=4)
    inv_id = await _seed_investigation(
        memory,
        summary="An unrelated investigation about config drift.",
        resolution_at=res_at,
    )
    fake = _FakeZabbixClient(history={})
    out = await infer_outcome(memory, fake, investigation_id=inv_id)
    assert out is None
    # outcome_inferred remains null.
    row = await memory.fetchone(
        "SELECT outcome_inferred FROM investigations WHERE id=?", (inv_id,),
    )
    assert row[0] is None


async def test_no_resolution_returns_none(memory):
    """A row without resolution_at must not be evaluated even if everything
    else is present."""
    await memory.execute(
        """INSERT INTO investigations
           (source, started_at, hostid, hostname, summary)
           VALUES (?, ?, ?, ?, ?)""",
        ("cli", datetime.now(UTC).isoformat(), 101, "h1", "Disk full"),
    )
    row = await memory.fetchone("SELECT last_insert_rowid()")
    inv_id = int(row[0])
    fake = _FakeZabbixClient(history={
        "vfs.fs.size": [{"clock": 1, "value": "50"}],
    })
    out = await infer_outcome(memory, fake, investigation_id=inv_id)
    assert out is None


async def test_history_call_uses_3day_window(memory):
    """The runner asks Zabbix for 3 days of context around resolution_at."""
    res_at = datetime.now(UTC) - timedelta(hours=2)
    inv_id = await _seed_investigation(
        memory, summary="Disk space at 90%", resolution_at=res_at,
    )
    fake = _FakeZabbixClient(history={
        "vfs.fs.size": [
            {"clock": int(res_at.timestamp()) - 60, "value": "90"},
            {"clock": int(res_at.timestamp()) + 7200, "value": "60"},
        ],
    })
    await infer_outcome(memory, fake, investigation_id=inv_id)
    assert fake.calls
    _hostid, _keys, range_seconds = fake.calls[0]
    assert range_seconds == 86_400 * 3
