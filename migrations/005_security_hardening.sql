-- Migration 005: security hardening (v1.3.1)
-- Adds used_tokens table (jti single-use enforcement),
-- last_totp_code + last_totp_at columns on users (TOTP replay cache),
-- and admin_audit_log table for admin-side events.

CREATE TABLE IF NOT EXISTS used_tokens (
    jti TEXT PRIMARY KEY,
    used_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_used_tokens_exp ON used_tokens(expires_at);

-- Add TOTP replay-cache columns to users (idempotent via separate ALTER TABLE)
-- SQLite does not support ADD COLUMN IF NOT EXISTS, so we guard with a
-- try-by-migration-version approach: the INSERT OR IGNORE at the bottom
-- means this file only runs once anyway.
ALTER TABLE users ADD COLUMN last_totp_code TEXT;
ALTER TABLE users ADD COLUMN last_totp_at TEXT;

-- Admin audit log (separate from investigation audit_log)
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,   -- login_success, login_failure, logout,
                                --  totp_enroll, conn_upsert, conn_delete,
                                --  secret_set, token_issued, oauth_login, etc.
    by_user TEXT,               -- username performing the action (NULL for failures before auth)
    target TEXT,                -- e.g. connection name, secret key, username
    ip TEXT,
    details TEXT                -- freeform JSON (never contains secret values)
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit_log(ts);

INSERT OR IGNORE INTO schema_version(version) VALUES (5);
