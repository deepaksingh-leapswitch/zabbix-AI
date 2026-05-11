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


async def verify_totp_with_replay_check(
    memory: Memory, user_id: int, secret: str, code: str
) -> bool:
    """Verify a TOTP code and reject replay within the same 30-s window (#16).

    Returns True only if the code is valid AND it hasn't been used by this
    user in the current (or immediately adjacent) TOTP window.
    """
    if not code or not verify_totp(secret, code):
        return False

    # Check if the same code was already used by this user recently (≤ 60 s)
    row = await memory.fetchone(
        "SELECT last_totp_code, last_totp_at FROM users WHERE id=?", (user_id,)
    )
    if row:
        last_code, last_at = row
        if last_code == code and last_at:
            try:
                last_dt = datetime.fromisoformat(last_at)
                delta = (datetime.now(UTC) - last_dt).total_seconds()
                if delta < 60:
                    # Same code within 60 s window — reject replay
                    return False
            except ValueError:
                pass

    # Record this code as used
    await memory.execute(
        "UPDATE users SET last_totp_code=?, last_totp_at=? WHERE id=?",
        (code, _now_iso(), user_id),
    )
    return True


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


async def get_user_by_oauth(memory: Memory, *, provider: str,
                            subject: str) -> dict[str, Any] | None:
    row = await memory.fetchone(
        """SELECT id, username, role, disabled FROM users
           WHERE oauth_provider=? AND oauth_subject=?""",
        (provider, subject),
    )
    if not row:
        return None
    return {"id": row[0], "username": row[1], "role": row[2],
            "disabled": bool(row[3])}


async def create_oauth_user(memory: Memory, *, username: str, provider: str,
                             subject: str, role: str = "viewer",
                             ) -> dict[str, Any]:
    await memory.execute(
        """INSERT INTO users
           (username, totp_enrolled, role, oauth_provider, oauth_subject,
            created_at)
           VALUES (?, 1, ?, ?, ?, ?)""",
        (username, role, provider, subject, _now_iso()),
    )
    row = await memory.fetchone(
        "SELECT id FROM users WHERE username=?", (username,),
    )
    assert row is not None
    return {"id": row[0], "username": username, "role": role}


async def ensure_bootstrap_admin(memory: Memory, *, username: str,
                                  password: str) -> dict[str, Any] | None:
    """Create a single admin user if no users exist. Idempotent."""
    row = await memory.fetchone("SELECT COUNT(*) FROM users")
    if row and row[0] > 0:
        return None
    return await create_user(memory, username=username,
                              password=password, role="admin")


# ── User management (v1.4) ──────────────────────────────────────────────────

_VALID_ROLES = {"admin", "operator", "viewer"}


async def list_users(memory: Memory) -> list[dict[str, Any]]:
    """Return all users, ordered by id ascending."""
    rows = await memory.fetchall(
        """SELECT id, username, role, totp_enrolled, disabled,
                  created_at, last_login_at
           FROM users ORDER BY id ASC""",
    )
    return [
        {
            "id": r[0],
            "username": r[1],
            "role": r[2],
            "totp_enrolled": bool(r[3]),
            "disabled": bool(r[4]),
            "created_at": r[5],
            "last_login_at": r[6],
        }
        for r in rows
    ]


async def get_user_by_id(memory: Memory,
                          user_id: int) -> dict[str, Any] | None:
    row = await memory.fetchone(
        """SELECT id, username, role, totp_enrolled, disabled,
                  created_at, last_login_at
           FROM users WHERE id=?""",
        (user_id,),
    )
    if not row:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "role": row[2],
        "totp_enrolled": bool(row[3]),
        "disabled": bool(row[4]),
        "created_at": row[5],
        "last_login_at": row[6],
    }


async def set_password(memory: Memory, user_id: int,
                        new_password: str) -> None:
    await memory.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (hash_password(new_password), user_id),
    )


async def set_role(memory: Memory, user_id: int, role: str) -> None:
    if role not in _VALID_ROLES:
        raise ValueError(
            f"invalid role '{role}'; must be one of {sorted(_VALID_ROLES)}"
        )
    await memory.execute(
        "UPDATE users SET role=? WHERE id=?", (role, user_id),
    )


async def set_disabled(memory: Memory, user_id: int,
                        disabled: bool) -> None:
    await memory.execute(
        "UPDATE users SET disabled=? WHERE id=?",
        (1 if disabled else 0, user_id),
    )


async def reset_totp(memory: Memory, user_id: int) -> str:
    """Generate a fresh TOTP secret, clear enrollment + replay cache."""
    new_secret = generate_totp_secret()
    await memory.execute(
        """UPDATE users SET totp_secret=?, totp_enrolled=0,
                              last_totp_code=NULL, last_totp_at=NULL
           WHERE id=?""",
        (new_secret, user_id),
    )
    return new_secret


async def delete_user(memory: Memory, user_id: int) -> None:
    await memory.execute("DELETE FROM users WHERE id=?", (user_id,))


async def count_admins(memory: Memory) -> int:
    """Number of enabled admin accounts."""
    row = await memory.fetchone(
        "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0"
    )
    return int(row[0]) if row else 0
