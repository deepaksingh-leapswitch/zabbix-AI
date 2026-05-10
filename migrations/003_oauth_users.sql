PRAGMA foreign_keys=OFF;

CREATE TABLE users_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    totp_secret TEXT,
    totp_enrolled INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'viewer',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    oauth_provider TEXT,
    oauth_subject TEXT,
    UNIQUE(oauth_provider, oauth_subject)
);

INSERT INTO users_new (id, username, password_hash, totp_secret, totp_enrolled, role, disabled, created_at, last_login_at)
    SELECT id, username, password_hash, totp_secret, totp_enrolled, role, disabled, created_at, last_login_at FROM users;
DROP TABLE users;
ALTER TABLE users_new RENAME TO users;

PRAGMA foreign_keys=ON;

INSERT OR IGNORE INTO schema_version(version) VALUES (3);
