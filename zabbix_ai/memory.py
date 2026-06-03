from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


class Memory:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def run_migrations(self, migrations_dir: Path) -> None:
        assert self._conn
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        async with self._conn.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
        current = row[0] or 0
        files = sorted(migrations_dir.glob("*.sql"))
        for f in files:
            m = re.match(r"(\d+)_", f.name)
            if not m:
                continue
            v = int(m.group(1))
            if v <= current:
                continue
            await self._conn.executescript(f.read_text())
        await self._conn.commit()

    async def execute(self, sql: str, params: tuple = ()) -> None:
        assert self._conn
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def execute_write(self, sql: str, params: tuple = ()) -> int:
        """Like execute() but returns affected row count — used for guarded
        state transitions: UPDATE ... WHERE id=? AND state=?."""
        assert self._conn
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur.rowcount

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def compute_pattern_signature(*, problem_name: str, hostgroup: str = "") -> str:
    """Stable, lowercase, whitespace-collapsed hash of (problem, hostgroup).

    Deterministic so re-occurrences of the same alert on the same kind of
    host produce the same signature. Returns a hex string (16 chars) — short
    enough to read in logs, wide enough for collisions to be ignorable at
    the volume we expect (<<1M patterns).
    """
    norm = lambda s: re.sub(r"\s+", " ", (s or "").lower()).strip()  # noqa: E731
    raw = f"{norm(problem_name)}|{norm(hostgroup)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def write_investigation_summary(
    memory: Memory, *, investigation_id: int,
    summary: str = "", root_cause: str = "", suggested_actions: str = "",
    confidence: str = "", pattern_signature: str = "",
) -> None:
    await memory.execute(
        """UPDATE investigations
           SET summary=?, root_cause=?, suggested_actions=?, confidence=?,
               pattern_signature=?
           WHERE id=?""",
        (summary, root_cause, suggested_actions, confidence,
         pattern_signature, investigation_id),
    )


async def upsert_host_facts(
    memory: Memory, *, hostid: int, facts: dict[str, str],
    source_investigation_id: int | None = None,
) -> None:
    ts = _now_iso()
    for key, value in facts.items():
        await memory.execute(
            """INSERT INTO host_facts (hostid, key, value,
                                        source_investigation_id, learned_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(hostid, key) DO UPDATE SET
                 value=excluded.value,
                 source_investigation_id=excluded.source_investigation_id,
                 learned_at=excluded.learned_at""",
            (hostid, key, value, source_investigation_id, ts),
        )


async def upsert_pattern(
    memory: Memory, *, signature: str,
    typical_root_cause: str = "", typical_fix: str = "",
) -> None:
    ts = _now_iso()
    await memory.execute(
        """INSERT INTO patterns (signature, first_seen, last_seen, occurrences,
                                  typical_root_cause, typical_fix,
                                  confidence_score)
           VALUES (?, ?, ?, 1, ?, ?, 0.5)
           ON CONFLICT(signature) DO UPDATE SET
             last_seen=excluded.last_seen,
             occurrences=patterns.occurrences + 1,
             typical_root_cause=excluded.typical_root_cause,
             typical_fix=excluded.typical_fix""",
        (signature, ts, ts, typical_root_cause, typical_fix),
    )


async def find_similar_past_investigations(
    memory: Memory, *, hostid: int | None,
    pattern_signature: str | None, limit: int = 5,
) -> list[dict]:
    where = []
    params: list = []
    if hostid is not None:
        where.append("hostid = ?")
        params.append(hostid)
    if pattern_signature:
        where.append("pattern_signature = ?")
        params.append(pattern_signature)
    if not where:
        return []
    sql = (
        "SELECT id, started_at, hostid, hostname, pattern_signature, "
        "       summary, root_cause, confidence, "
        "       resolution_notes, resolution_at, resolution_by, "
        "       resolution_source "
        "FROM investigations "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY id DESC LIMIT ?"
    )
    params.append(limit)
    rows = await memory.fetchall(sql, tuple(params))
    keys = ("id", "started_at", "hostid", "hostname", "pattern_signature",
            "summary", "root_cause", "confidence",
            "resolution_notes", "resolution_at", "resolution_by",
            "resolution_source")
    return [dict(zip(keys, r, strict=False)) for r in rows]


async def find_pattern(memory: Memory, *, signature: str) -> dict | None:
    row = await memory.fetchone(
        "SELECT signature, first_seen, last_seen, occurrences, "
        "       typical_root_cause, typical_fix, confidence_score "
        "FROM patterns WHERE signature=?",
        (signature,),
    )
    if not row:
        return None
    return dict(zip(
        ("signature", "first_seen", "last_seen", "occurrences",
         "typical_root_cause", "typical_fix", "confidence_score"), row,
        strict=False,
    ))
