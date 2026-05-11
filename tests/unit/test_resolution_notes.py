"""Unit tests for zabbix_ai.services.resolution_notes."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zabbix_ai.memory import Memory, find_similar_past_investigations
from zabbix_ai.services.resolution_notes import (
    capture_resolution_from_zabbix_event,
    poll_zabbix_for_resolutions,
)


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "mem.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()


def _make_client(
    *,
    event_rows: list | None = None,
    user_rows: list | None = None,
) -> MagicMock:
    """Mock ZabbixClient with canned event.get and user.get responses."""
    async def _call(method, params=None):
        if method == "event.get":
            return event_rows or []
        if method == "user.get":
            return user_rows or []
        return []

    client = MagicMock()
    client.call = AsyncMock(side_effect=_call)
    return client


async def _insert_investigation(
    mem: Memory, *, instance: str = "primary",
    eventid: int = 1000, hostid: int = 42,
    started_at: str = "2026-05-09T00:00:00+00:00",
) -> int:
    await mem.execute(
        """INSERT INTO investigations (source, instance, eventid, hostid,
                                        hostname, started_at,
                                        pattern_signature, summary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cli", instance, eventid, hostid, "srv01", started_at,
         "sig-A", "ran out of disk"),
    )
    row = await mem.fetchone("SELECT last_insert_rowid()")
    return int(row[0])


# ─── capture_resolution_from_zabbix_event ───────────────────────────────────

async def test_capture_single_ack_message_updates_row(mem):
    inv_id = await _insert_investigation(mem, eventid=1001)
    now = int(time.time())
    client = _make_client(
        event_rows=[{
            "eventid": "1001",
            "name": "Disk space critical /var",
            "clock": str(now - 3600),
            "r_clock": str(now - 60),
            "acknowledges": [
                {
                    "acknowledgeid": "5001",
                    "message": "Rotated apache logs, freed 14 GB.",
                    "action": "6",   # 2 (ack) + 4 (message) = 6
                    "userid": "10",
                    "clock": str(now - 120),
                },
            ],
        }],
        user_rows=[{"userid": "10", "username": "deepak",
                     "name": "Deepak", "surname": "Singh"}],
    )

    ok = await capture_resolution_from_zabbix_event(
        client, mem, investigation_id=inv_id, eventid=1001,
    )
    assert ok is True

    row = await mem.fetchone(
        """SELECT resolution_notes, resolution_at, resolution_by,
                  resolution_source
           FROM investigations WHERE id=?""",
        (inv_id,),
    )
    notes, resolved_at, by, source = row
    assert "Rotated apache logs" in notes
    assert "deepak" in notes
    assert resolved_at  # ISO timestamp
    assert by == "deepak"
    assert source == "zabbix_ack"


async def test_capture_multiple_acks_concatenated_in_order(mem):
    inv_id = await _insert_investigation(mem, eventid=1002)
    now = int(time.time())
    client = _make_client(
        event_rows=[{
            "eventid": "1002",
            "acknowledges": [
                # Note: deliberately out-of-order to ensure we sort by clock
                {"acknowledgeid": "2",
                 "message": "Second message — actual fix applied.",
                 "action": "5",  # 1 (close) + 4 (message)
                 "userid": "11",
                 "clock": str(now - 60)},
                {"acknowledgeid": "1",
                 "message": "First message — initial triage.",
                 "action": "6",
                 "userid": "10",
                 "clock": str(now - 600)},
            ],
        }],
        user_rows=[
            {"userid": "10", "username": "alice"},
            {"userid": "11", "username": "bob"},
        ],
    )

    # user.get returns both in one call in real life; mock returns the
    # first match irrespective of userids. Override side_effect to
    # filter on params for this test.
    async def _call(method, params=None):
        if method == "event.get":
            return client.call.side_effect.event_rows  # type: ignore[attr-defined]
        if method == "user.get":
            requested = (params or {}).get("userids") or []
            return [u for u in [{"userid": "10", "username": "alice"},
                                 {"userid": "11", "username": "bob"}]
                    if u["userid"] in requested]
        return []

    # Easier: rebuild the mock with a userid-aware user.get
    event_rows = [{
        "eventid": "1002",
        "acknowledges": [
            {"acknowledgeid": "2",
             "message": "Second message — actual fix applied.",
             "action": "5",
             "userid": "11",
             "clock": str(now - 60)},
            {"acknowledgeid": "1",
             "message": "First message — initial triage.",
             "action": "6",
             "userid": "10",
             "clock": str(now - 600)},
        ],
    }]

    async def _smart_call(method, params=None):
        if method == "event.get":
            return event_rows
        if method == "user.get":
            requested = (params or {}).get("userids") or []
            pool = [{"userid": "10", "username": "alice"},
                    {"userid": "11", "username": "bob"}]
            return [u for u in pool if u["userid"] in requested]
        return []

    client = MagicMock()
    client.call = AsyncMock(side_effect=_smart_call)

    ok = await capture_resolution_from_zabbix_event(
        client, mem, investigation_id=inv_id, eventid=1002,
    )
    assert ok is True

    row = await mem.fetchone(
        "SELECT resolution_notes, resolution_by FROM investigations WHERE id=?",
        (inv_id,),
    )
    notes, by = row
    # Oldest first, then newest
    assert notes.index("First message") < notes.index("Second message")
    # Separator present
    assert "\n---\n" in notes
    # Latest ack's userid → bob (id=11)
    assert by == "bob"
    # Close marker present on the bob ack
    assert "[closed]" in notes


async def test_capture_no_acks_leaves_row_untouched(mem):
    inv_id = await _insert_investigation(mem, eventid=1003)
    client = _make_client(event_rows=[{
        "eventid": "1003",
        "acknowledges": [],
    }])

    ok = await capture_resolution_from_zabbix_event(
        client, mem, investigation_id=inv_id, eventid=1003,
    )
    assert ok is False

    row = await mem.fetchone(
        "SELECT resolution_notes FROM investigations WHERE id=?",
        (inv_id,),
    )
    assert row[0] is None


async def test_capture_ignores_acks_without_message_or_close(mem):
    """Pure ack (action=2) with no message should not produce notes."""
    inv_id = await _insert_investigation(mem, eventid=1004)
    client = _make_client(event_rows=[{
        "eventid": "1004",
        "acknowledges": [
            {"acknowledgeid": "1",
             "message": "",
             "action": "2",   # ack only, no message bit, no close bit
             "userid": "10",
             "clock": "1700000000"},
        ],
    }])

    ok = await capture_resolution_from_zabbix_event(
        client, mem, investigation_id=inv_id, eventid=1004,
    )
    assert ok is False


async def test_capture_event_not_found_returns_false(mem):
    inv_id = await _insert_investigation(mem, eventid=1005)
    client = _make_client(event_rows=[])

    ok = await capture_resolution_from_zabbix_event(
        client, mem, investigation_id=inv_id, eventid=1005,
    )
    assert ok is False


# ─── poll_zabbix_for_resolutions ────────────────────────────────────────────

async def test_poll_updates_only_eligible_rows(mem):
    """Poll skips rows that already have resolution_notes or that
    belong to an unknown Zabbix instance."""
    from datetime import UTC, datetime
    fresh = datetime.now(UTC).isoformat()

    # Eligible — has eventid, no resolution_notes, instance is known.
    inv_a = await _insert_investigation(
        mem, instance="primary", eventid=2001, started_at=fresh,
    )
    # Already resolved — skip
    inv_b = await _insert_investigation(
        mem, instance="primary", eventid=2002, started_at=fresh,
    )
    await mem.execute(
        "UPDATE investigations SET resolution_notes='already done' WHERE id=?",
        (inv_b,),
    )
    # Unknown instance — skip
    await _insert_investigation(
        mem, instance="unknown", eventid=2003, started_at=fresh,
    )

    now = int(time.time())

    async def _call(method, params=None):
        if method == "event.get":
            eids = (params or {}).get("eventids") or []
            if "2001" in eids:
                return [{
                    "eventid": "2001",
                    "acknowledges": [{
                        "acknowledgeid": "1",
                        "message": "Cleaned up.",
                        "action": "6",
                        "userid": "10",
                        "clock": str(now - 60),
                    }],
                }]
            return []
        if method == "user.get":
            return [{"userid": "10", "username": "carol"}]
        return []

    client = MagicMock()
    client.call = AsyncMock(side_effect=_call)
    clients = {"primary": client}

    count = await poll_zabbix_for_resolutions(clients, mem, settings=None)
    assert count == 1

    row_a = await mem.fetchone(
        "SELECT resolution_notes, resolution_by FROM investigations WHERE id=?",
        (inv_a,),
    )
    assert "Cleaned up" in row_a[0]
    assert row_a[1] == "carol"


async def test_poll_skips_old_rows_outside_lookback(mem):
    """Investigations older than _LOOKBACK_DAYS (7) are not polled."""
    # 30 days ago is well outside the 7-day window.
    old_ts = "2026-04-09T00:00:00+00:00"
    inv = await _insert_investigation(
        mem, instance="primary", eventid=3001, started_at=old_ts,
    )

    client = _make_client(event_rows=[{"eventid": "3001",
                                          "acknowledges": [{
                                              "action": "4",
                                              "message": "shouldn't be reached",
                                              "userid": "1",
                                              "clock": "1700000000",
                                          }]}])
    count = await poll_zabbix_for_resolutions({"primary": client}, mem)
    assert count == 0
    row = await mem.fetchone(
        "SELECT resolution_notes FROM investigations WHERE id=?", (inv,),
    )
    assert row[0] is None


# ─── find_similar_past_investigations returns resolution fields ───────────

async def test_find_similar_returns_resolution_notes(mem):
    """Memory helper exposes the new resolution_* columns."""
    await mem.execute(
        """INSERT INTO investigations
           (source, started_at, hostid, pattern_signature, summary,
            resolution_notes, resolution_at, resolution_by,
            resolution_source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cli", "2026-05-09T00:00:00+00:00", 99, "sig-Z",
         "disk full /var",
         "rotated logs", "2026-05-10T03:00:00+00:00",
         "deepak", "zabbix_ack"),
    )
    rows = await find_similar_past_investigations(
        mem, hostid=99, pattern_signature="sig-Z", limit=5,
    )
    assert len(rows) == 1
    inv = rows[0]
    assert inv["resolution_notes"] == "rotated logs"
    assert inv["resolution_at"] == "2026-05-10T03:00:00+00:00"
    assert inv["resolution_by"] == "deepak"
    assert inv["resolution_source"] == "zabbix_ack"
