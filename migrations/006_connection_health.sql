-- Migration 006: connection_health (v1.4)
-- Tracks last successful / last failing call to external systems so the
-- /admin/status page can show "Zabbix monitoring: ✓ 3 seconds ago" or
-- "Anthropic: ✗ HTTP 529 (overloaded) 5 minutes ago".

CREATE TABLE IF NOT EXISTS connection_health (
    kind TEXT NOT NULL,         -- 'zabbix' | 'slack' | 'anthropic'
    name TEXT NOT NULL,         -- instance name or 'primary'
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    PRIMARY KEY (kind, name)
);

INSERT OR IGNORE INTO schema_version(version) VALUES (6);
