from __future__ import annotations
import re
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

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        assert self._conn
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()
