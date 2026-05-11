#!/bin/sh
# zabbix-rca-AI — Linux agent enablement script.
# Idempotent: re-running is safe.
#
# Run as root on each Linux host you want zabbix-rca-AI to be able to
# diagnose. Assumes zabbix-agent2 is already installed and the basic
# Server= / TLS PSK / firewall hardening is in place
# (see docs/AGENT-SETUP.md §2 and §9).
#
# Usage:
#   sudo ./install-linux.sh
#
# What it does:
#   1. Drops zabbix-ai-diag.conf into /etc/zabbix/zabbix_agent2.d/
#   2. Installs smartmontools + sysstat (skipped if already present)
#   3. Installs sudoers entry for smartctl (NOPASSWD, no shell)
#   4. Restarts zabbix-agent2
#   5. Prints a quick verify command
set -eu

CONF_SRC="$(dirname "$0")/zabbix-ai-diag.conf"
CONF_DST="/etc/zabbix/zabbix_agent2.d/zabbix-ai-diag.conf"
SUDOERS_DST="/etc/sudoers.d/zabbix-smartctl"

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

[ -f "$CONF_SRC" ] || { echo "missing $CONF_SRC" >&2; exit 1; }

echo "[1/4] installing $CONF_DST"
install -d -m 0755 /etc/zabbix/zabbix_agent2.d
install -m 0644 "$CONF_SRC" "$CONF_DST"

echo "[2/4] installing extra packages (smartmontools, sysstat)"
if   command -v apt >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt -qq install -y smartmontools sysstat >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf -q install -y smartmontools sysstat >/dev/null
elif command -v yum >/dev/null 2>&1; then
    yum -q install -y smartmontools sysstat >/dev/null
else
    echo "  no apt/dnf/yum found — install smartmontools + sysstat manually" >&2
fi

echo "[3/4] installing sudoers for smartctl"
SMARTCTL_PATH="$(command -v smartctl 2>/dev/null || echo /usr/sbin/smartctl)"
cat > "$SUDOERS_DST" <<EOF
# Allow zabbix agent to read SMART health for diag.smart (NOPASSWD, exact path).
zabbix ALL=(root) NOPASSWD: $SMARTCTL_PATH
EOF
chmod 0440 "$SUDOERS_DST"
visudo -cf "$SUDOERS_DST" >/dev/null

echo "[4/4] restarting zabbix-agent2"
systemctl restart zabbix-agent2
sleep 1
systemctl is-active zabbix-agent2 >/dev/null && echo "  ✓ agent is active"

cat <<EOF

Done. Verify from the Zabbix server:
  zabbix_get -s <this-host-ip> -k agent.version
  zabbix_get -s <this-host-ip> -k agent.variant     # 2 = Agent 2

Then trigger a test investigation from the Zabbix frontend
(right-click a host → Investigate with AI).
EOF
