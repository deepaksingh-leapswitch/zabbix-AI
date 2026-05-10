# Security fixes status — v1.3.1 (2026-05-10)

Post-fix status for all 21 findings from `SECURITY-REVIEW-2026-05-10.md`.

| # | Sev | Title | Status | File(s) |
|---|-----|-------|--------|---------|
| 1 | High | CSRF protection + POST logout | ✓ Done | `zabbix_ai/admin/csrf.py`, `admin/__init__.py`, all form templates, `auth_routes.py` |
| 2 | High | Cost-amplification / token replay | ✓ Done | `url_signing.py`, `adapters/zabbix_ui.py`, `admin/routes/zabbix_link.py`, `config.py`, `migrations/005_security_hardening.sql` |
| 3 | High | Rate limiting | ✓ Done | `zabbix_ai/admin/rate_limit.py`, `admin/__init__.py`, all rate-limited routes |
| 4 | Med | HTTP security headers | ✓ Done | `zabbix_ai/admin/security_headers.py`, `admin/__init__.py` |
| 5 | Med | OAuth iss validation + default_role restriction | ✓ Done | `admin/routes/oauth_google.py`, `admin/routes/connections.py` |
| 6 | Med | Login timing leak | ✓ Done | `admin/routes/auth_routes.py` (dummy hash) |
| 7 | Med | URL token replayability / log exposure | ✓ Done | `url_signing.py` (jti), `adapters/zabbix_ui.py` (single-use), `templates/investigate.html` (no-referrer), `docs/RUNBOOK.md` |
| 8 | Med | Audit log for admin events | ✓ Done | `zabbix_ai/admin/admin_audit.py`, all admin POST routes |
| 9 | Med | TOTP enrolment plaintext secret | ✓ Done | `templates/admin/enroll_totp.html` (QR only), `admin/routes/auth_routes.py` (enrolled check) |
| 10 | Low | Slack event_id dedupe | ✓ Done | `adapters/slack.py` (in-memory LRU cache) |
| 11 | Low | authlib.jose deprecated | ✓ Done | `admin/routes/oauth_google.py` (warning suppressed + note) |
| 12 | Low | htmx without SRI | ✓ Done | `zabbix_ai/static/htmx.min.js` self-hosted, `templates/admin/base.html` updated |
| 13 | Low | Real client IP | ✓ Done | `admin/rate_limit.py` (`_real_ip`), `admin/routes/auth_routes.py`, `admin/routes/oauth_google.py` |
| 14 | Low | OAuth default_role not validated | ✓ Done | Covered by #5 — `connections.py` now validates allowlist |
| 15 | Low | Sessions never cleaned up | ✓ Done | `admin/auth.py` (`resolve_session` probabilistic purge + lifespan task) |
| 16 | Low | TOTP replay window | ✓ Done | `admin/users.py` (last_totp_code/at check), `migrations/005_security_hardening.sql` |
| 17 | Info | LLM prompt injection | Doc-only | `docs/SECURITY.md` |
| 18 | Info | systemctl_status validator allows space | ✓ Done | `tools/diag.py` (regex tightened) |
| 19 | Info | SSRF on admin URL fields | ✓ Done | `admin/routes/connections.py` (`_validate_url` deny-list) |
| 20 | Info | .env.local secrets on disk | Operator action | Rotate `ANTHROPIC_API_KEY` and `ZABBIX_TOKEN_MONITORING`; see `docs/SECURITY.md` |
| 21 | Info | BOOTSTRAP_ADMIN_PASSWORD retained | ✓ Done | `admin/__init__.py` (startup warning + background task) |
