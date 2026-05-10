from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from zabbix_ai.admin.crypto import decrypt, encrypt
from zabbix_ai.memory import Memory


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ─── secrets ───

async def secret_set(memory: Memory, *, key: str, value: str,
                      crypto_key: bytes, updated_by: str = "") -> None:
    nonce, ct = encrypt(value, crypto_key)
    await memory.execute(
        """INSERT INTO secrets_kv (key, nonce, ciphertext, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET nonce=excluded.nonce,
             ciphertext=excluded.ciphertext, updated_at=excluded.updated_at,
             updated_by=excluded.updated_by""",
        (key, nonce, ct, _now(), updated_by),
    )


async def secret_get(memory: Memory, *, key: str,
                      crypto_key: bytes) -> str | None:
    row = await memory.fetchone(
        "SELECT nonce, ciphertext FROM secrets_kv WHERE key=?", (key,),
    )
    if not row:
        return None
    return decrypt(row[0], row[1], crypto_key)


async def secret_delete(memory: Memory, *, key: str) -> None:
    await memory.execute("DELETE FROM secrets_kv WHERE key=?", (key,))


# ─── connections ───

async def conn_list(memory: Memory, *, type_filter: str | None = None,
                     ) -> list[dict[str, Any]]:
    sql = "SELECT id, type, name, config_json, enabled, updated_at, updated_by FROM connections"
    args: tuple = ()
    if type_filter:
        sql += " WHERE type=?"
        args = (type_filter,)
    sql += " ORDER BY type, name"
    rows = await memory.fetchall(sql, args)
    return [
        {"id": r[0], "type": r[1], "name": r[2],
         "config": json.loads(r[3]), "enabled": bool(r[4]),
         "updated_at": r[5], "updated_by": r[6]}
        for r in rows
    ]


async def conn_get(memory: Memory, *, type_: str, name: str,
                    ) -> dict[str, Any] | None:
    row = await memory.fetchone(
        "SELECT id, config_json, enabled, updated_at, updated_by"
        " FROM connections WHERE type=? AND name=?",
        (type_, name),
    )
    if not row:
        return None
    return {"id": row[0], "type": type_, "name": name,
            "config": json.loads(row[1]), "enabled": bool(row[2]),
            "updated_at": row[3], "updated_by": row[4]}


async def conn_upsert(memory: Memory, *, type_: str, name: str,
                       config: dict[str, Any], enabled: bool = True,
                       updated_by: str = "") -> None:
    await memory.execute(
        """INSERT INTO connections (type, name, config_json, enabled, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(type, name) DO UPDATE SET
             config_json=excluded.config_json, enabled=excluded.enabled,
             updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (type_, name, json.dumps(config, sort_keys=True),
         1 if enabled else 0, _now(), updated_by),
    )


async def conn_delete(memory: Memory, *, type_: str, name: str) -> None:
    await memory.execute(
        "DELETE FROM connections WHERE type=? AND name=?", (type_, name),
    )
