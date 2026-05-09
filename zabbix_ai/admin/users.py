from __future__ import annotations

import secrets  # noqa: F401
from datetime import UTC, datetime
from typing import Any

import bcrypt
import pyotp

from zabbix_ai.memory import Memory


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plaintext.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_provisioning_uri(username: str, secret: str,
                          issuer: str = "Zabbix RCA AI") -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name=issuer,
    )


async def create_user(memory: Memory, *, username: str, password: str,
                      role: str = "viewer") -> dict[str, Any]:
    secret = generate_totp_secret()
    await memory.execute(
        """INSERT INTO users
           (username, password_hash, totp_secret, role, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (username, hash_password(password), secret, role, _now_iso()),
    )
    row = await memory.fetchone(
        "SELECT id, username, role, totp_secret FROM users WHERE username=?",
        (username,),
    )
    assert row is not None
    return {"id": row[0], "username": row[1], "role": row[2],
            "totp_secret": row[3]}


async def get_user_by_username(memory: Memory,
                               username: str) -> dict[str, Any] | None:
    row = await memory.fetchone(
        """SELECT id, username, password_hash, totp_secret, totp_enrolled,
                  role, disabled FROM users WHERE username=?""", (username,),
    )
    if not row:
        return None
    return dict(zip(
        ("id", "username", "password_hash", "totp_secret",
         "totp_enrolled", "role", "disabled"), row, strict=False,
    ))


async def set_totp_enrolled(memory: Memory, user_id: int) -> None:
    await memory.execute(
        "UPDATE users SET totp_enrolled=1 WHERE id=?", (user_id,),
    )


async def update_last_login(memory: Memory, user_id: int) -> None:
    await memory.execute(
        "UPDATE users SET last_login_at=? WHERE id=?", (_now_iso(), user_id),
    )


async def ensure_bootstrap_admin(memory: Memory, *, username: str,
                                  password: str) -> dict[str, Any] | None:
    """Create a single admin user if no users exist. Idempotent."""
    row = await memory.fetchone("SELECT COUNT(*) FROM users")
    if row and row[0] > 0:
        return None
    return await create_user(memory, username=username,
                              password=password, role="admin")
