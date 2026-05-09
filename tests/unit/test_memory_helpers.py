from pathlib import Path

import pytest

from zabbix_ai.memory import (
    Memory,
    compute_pattern_signature,
    find_pattern,
    find_similar_past_investigations,
    upsert_host_facts,
    upsert_pattern,
    write_investigation_summary,
)


@pytest.fixture
async def mem(tmp_path):
    m = Memory(tmp_path / "mem.db")
    await m.connect()
    await m.run_migrations(Path("migrations"))
    yield m
    await m.close()

def test_signature_normalises_text():
    a = compute_pattern_signature(problem_name="Disk Space LOW on /var",
                                   hostgroup="Managed cPanel VPS")
    b = compute_pattern_signature(problem_name="disk space low on /var",
                                   hostgroup="managed cpanel vps")
    assert a == b
    # signature is a short stable string
    assert len(a) <= 64
    # different problem produces different signature
    c = compute_pattern_signature(problem_name="apache down",
                                   hostgroup="Managed cPanel VPS")
    assert a != c


async def test_write_investigation_summary_updates_row(mem):
    await mem.execute(
        "INSERT INTO investigations (source, started_at) VALUES (?, ?)",
        ("test", "2026-05-09T00:00:00+00:00"),
    )
    inv_id = (await mem.fetchone("SELECT last_insert_rowid()"))[0]
    await write_investigation_summary(
        mem, investigation_id=inv_id,
        summary="ok", root_cause="rc", suggested_actions="do x",
        confidence="high", pattern_signature="sig-1",
    )
    row = await mem.fetchone(
        "SELECT root_cause, suggested_actions, confidence, pattern_signature "
        "FROM investigations WHERE id=?", (inv_id,),
    )
    assert row == ("rc", "do x", "high", "sig-1")


async def test_upsert_host_facts_inserts_then_updates(mem):
    await upsert_host_facts(mem, hostid=12345, facts={
        "primary_role": "mysql replica",
        "rack": "DC2-R7",
    }, source_investigation_id=1)
    rows = await mem.fetchall(
        "SELECT key, value FROM host_facts WHERE hostid=?", (12345,),
    )
    assert dict(rows) == {"primary_role": "mysql replica", "rack": "DC2-R7"}
    # update overrides
    await upsert_host_facts(mem, hostid=12345, facts={"rack": "DC2-R8"},
                              source_investigation_id=2)
    rows = await mem.fetchall(
        "SELECT key, value FROM host_facts WHERE hostid=?", (12345,),
    )
    assert dict(rows) == {"primary_role": "mysql replica", "rack": "DC2-R8"}


async def test_upsert_pattern_increments_occurrences(mem):
    await upsert_pattern(mem, signature="sig-1",
                         typical_root_cause="disk full on /var",
                         typical_fix="rotate logs")
    await upsert_pattern(mem, signature="sig-1",
                         typical_root_cause="disk full on /var",
                         typical_fix="rotate logs")
    row = await mem.fetchone(
        "SELECT occurrences, typical_fix FROM patterns WHERE signature=?",
        ("sig-1",),
    )
    assert row[0] == 2
    assert row[1] == "rotate logs"


async def test_find_similar_past_investigations(mem):
    for sig, hostid, summary in [
        ("sig-A", 1, "old run 1"),
        ("sig-A", 1, "old run 2"),
        ("sig-B", 1, "different pattern"),
        ("sig-A", 2, "different host"),
    ]:
        await mem.execute(
            "INSERT INTO investigations (source, started_at, hostid, "
            " pattern_signature, summary) VALUES (?, ?, ?, ?, ?)",
            ("cli", "2026-05-09T00:00:00+00:00", hostid, sig, summary),
        )
    by_host = await find_similar_past_investigations(
        mem, hostid=1, pattern_signature=None, limit=5,
    )
    assert len(by_host) == 3  # 3 rows for hostid=1
    by_pattern = await find_similar_past_investigations(
        mem, hostid=None, pattern_signature="sig-A", limit=5,
    )
    assert len(by_pattern) == 3  # 3 rows with sig-A
    by_both = await find_similar_past_investigations(
        mem, hostid=1, pattern_signature="sig-A", limit=5,
    )
    assert len(by_both) == 2


async def test_find_pattern_returns_pattern_row(mem):
    await upsert_pattern(mem, signature="sig-x",
                         typical_root_cause="rc", typical_fix="fix")
    row = await find_pattern(mem, signature="sig-x")
    assert row is not None
    assert row["signature"] == "sig-x"
    assert row["typical_root_cause"] == "rc"
    assert row["occurrences"] == 1
    # missing signature → None
    assert await find_pattern(mem, signature="nope") is None
