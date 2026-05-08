CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    instance TEXT,
    eventid INTEGER,
    ticket_id INTEGER,
    customer_id INTEGER,
    hostid INTEGER,
    hostname TEXT,
    started_at TEXT NOT NULL,
    duration_ms INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    model TEXT,
    summary TEXT,
    root_cause TEXT,
    suggested_actions TEXT,
    confidence TEXT,
    pattern_signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_pattern ON investigations(pattern_signature);
CREATE INDEX IF NOT EXISTS idx_inv_host ON investigations(hostid);

CREATE TABLE IF NOT EXISTS host_facts (
    hostid INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    source_investigation_id INTEGER,
    learned_at TEXT NOT NULL,
    PRIMARY KEY (hostid, key)
);

CREATE TABLE IF NOT EXISTS patterns (
    signature TEXT PRIMARY KEY,
    first_seen TEXT,
    last_seen TEXT,
    occurrences INTEGER NOT NULL DEFAULT 0,
    typical_root_cause TEXT,
    typical_fix TEXT,
    confidence_score REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ticket_resolutions (
    ticket_id INTEGER PRIMARY KEY,
    alert_pattern TEXT,
    resolution_text TEXT,
    customer_id INTEGER,
    hostname TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticket_pattern ON ticket_resolutions(alert_pattern);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    investigation_id INTEGER,
    event_type TEXT NOT NULL,
    tool_name TEXT,
    tool_input TEXT,
    tool_output TEXT,
    user TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_inv ON audit_log(investigation_id);

INSERT OR IGNORE INTO schema_version(version) VALUES (1);
