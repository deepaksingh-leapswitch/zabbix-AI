-- Migration 008: ticket-flow — auto HostBill ticket + Slack follow-up state machine.
-- One row per qualifying Zabbix problem. Drives draft→approve→create→follow-up→disable.

CREATE TABLE IF NOT EXISTS incidents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    zabbix_instance      TEXT NOT NULL,
    eventid              INTEGER,
    hostid               INTEGER,
    hostname             TEXT,
    severity             INTEGER NOT NULL DEFAULT 0,
    trigger_name         TEXT,
    problem_type         TEXT NOT NULL DEFAULT 'other',   -- 'icmp' | 'agent' | 'other' (disable scope)
    investigation_id     INTEGER,                       -- FK -> investigations.id
    ticket_kind          TEXT,                          -- 'customer' | 'internal'
    hostbill_client_id   INTEGER,
    hostbill_ticket_id   INTEGER,                        -- null until approved + created
    slack_channel        TEXT,
    slack_thread_ts      TEXT,
    state                TEXT NOT NULL DEFAULT 'drafted',
        -- drafted|approved|created|awaiting_reply|handed_off|quiet|resolved|
        -- disable_pending|disabled|discarded
    nudge_count          INTEGER NOT NULL DEFAULT 0,
    last_nudge_at        TEXT,
    next_nudge_at        TEXT,
    baseline_reply_count INTEGER NOT NULL DEFAULT 0,     -- ticket reply count at creation
    problem_active       INTEGER NOT NULL DEFAULT 1,     -- bool: still firing in Zabbix
    resolved_at          TEXT,
    created_at           TEXT NOT NULL,
    approved_at          TEXT,
    approved_by          TEXT,                           -- slack user id who approved
    ticket_created_at    TEXT,
    disable_scope        TEXT,                           -- 'host' | 'trigger' | 'maintenance'
    disable_approved_by  TEXT,
    disabled_at          TEXT
);

-- Idempotency: a retried webhook for the same event must not create a 2nd row.
-- (eventid NULL host-mode rows are treated as distinct by SQLite, which is fine.)
CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_event
    ON incidents(zabbix_instance, eventid);
CREATE INDEX IF NOT EXISTS idx_incidents_state      ON incidents(state);
CREATE INDEX IF NOT EXISTS idx_incidents_next_nudge ON incidents(next_nudge_at);

INSERT OR IGNORE INTO schema_version(version) VALUES (8);
