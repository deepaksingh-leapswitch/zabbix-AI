# zabbix-rca-AI

AI-assisted root-cause analysis for Leapswitch Zabbix monitoring.

On-demand AI investigation triggered from:
- Slack mention (`@zabbix-ai`) in alert threads
- Zabbix UI right-click ("Investigate with AI") on any problem
- HostBill ticket webhook for customer-raised issues

The AI is **read-only by construction** — runs only an allowlisted set of
diagnostics via Zabbix agent UserParameters, never gets shell access, never
mutates state. Customer replies are *staged as drafts* for L1 review.

See `docs/superpowers/specs/` for the design.

## Status

Design phase — see latest spec in `docs/superpowers/specs/`.
