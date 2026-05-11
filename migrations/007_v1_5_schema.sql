-- Migration 008: v1.5 — auto-investigate, resolution feed-forward, budget, hostbill linkage
-- All schema for the v1.5 "overcome L1/L2 time" workstream lives here so subagents
-- can build in parallel without stepping on each other's migrations.

-- Where this investigation came from (manual click vs auto webhook etc.)
ALTER TABLE investigations ADD COLUMN trigger_source TEXT NOT NULL DEFAULT 'manual';
-- 'manual' | 'auto' | 'webhook' | 'shadow'
CREATE INDEX IF NOT EXISTS idx_inv_trigger ON investigations(trigger_source);

-- Resolution tracking — populated by Zabbix-ack polling, Slack capture,
-- HostBill ticket close, or operator typing.
ALTER TABLE investigations ADD COLUMN resolution_notes TEXT;
ALTER TABLE investigations ADD COLUMN resolution_at TEXT;
ALTER TABLE investigations ADD COLUMN resolution_by TEXT;
ALTER TABLE investigations ADD COLUMN resolution_source TEXT;
-- 'zabbix_ack' | 'hostbill_ticket' | 'slack' | 'manual' | 'auto_inferred'

-- Outcome inference: AI's `suggested_actions` cross-referenced against the
-- host's metric delta after resolution. Stores a JSON list of action-indexes
-- that correlated with recovery.
ALTER TABLE investigations ADD COLUMN outcome_inferred TEXT;
-- JSON e.g. {"effective_action_indexes":[3], "metric":"vfs.fs.size[C:,pused]",
--           "delta_before":92.6, "delta_after":68.1}

-- Daily Anthropic budget enforcement audit log.
CREATE TABLE IF NOT EXISTS budget_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,             -- 'allowed' | 'downgraded_haiku' | 'paused'
    daily_spent_inr REAL NOT NULL,
    daily_limit_inr REAL NOT NULL,
    model_requested TEXT,
    model_effective TEXT,
    investigation_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_budget_audit_ts ON budget_audit(ts);

-- Zabbix host  -->  HostBill service / client linkage. Populated lazily on
-- first investigation, refreshed daily by a background sync.
CREATE TABLE IF NOT EXISTS host_hostbill_link (
    zabbix_instance TEXT NOT NULL,
    zabbix_hostid INTEGER NOT NULL,
    hostbill_service_id INTEGER,
    hostbill_client_id INTEGER,
    hostbill_client_name TEXT,
    hostbill_domain TEXT,
    linked_at TEXT,
    linked_by TEXT,                   -- 'auto:tag' | 'auto:ip' | 'auto:hostname' | 'manual'
    confidence TEXT,                  -- 'high' | 'medium' | 'low'
    PRIMARY KEY (zabbix_instance, zabbix_hostid)
);
CREATE INDEX IF NOT EXISTS idx_hb_link_service ON host_hostbill_link(hostbill_service_id);

INSERT OR IGNORE INTO schema_version(version) VALUES (7);
