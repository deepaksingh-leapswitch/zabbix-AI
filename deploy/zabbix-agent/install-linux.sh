#!/bin/sh
# zabbix-rca-AI — Linux agent enablement script.
# Detects Agent 1 vs Agent 2 and configures accordingly. Idempotent.
#
# Run as root on each Linux host you want zabbix-rca-AI to be able to
# diagnose. Assumes the agent is already installed and basic hardening
# (Server= / TLS PSK / firewall) is in place — see docs/AGENT-SETUP.md.
#
# Usage:
#   sudo ./install-linux.sh
#
# What it does:
#   1. Detects which agent variant is installed (agent2 preferred).
#   2. Drops AllowKey config into the agent's *.d/ include directory.
#   3. For Agent 1, sets EnableRemoteCommands=1 in the main config
#      (Agent 2 doesn't need it — AllowKey controls system.run directly).
#   4. Installs smartmontools + sysstat (skipped if already present).
#   5. Installs sudoers entry for smartctl (NOPASSWD, exact path).
#   6. Restarts the right service.
set -eu

CONF_SRC="$(cd "$(dirname "$0")" && pwd)/zabbix-ai-diag.conf"
SUDOERS_DST="/etc/sudoers.d/zabbix-smartctl"

[ "$(id -u)" -eq 0 ] || { echo "must run as root (try: sudo $0)" >&2; exit 1; }
[ -f "$CONF_SRC" ]   || { echo "missing $CONF_SRC" >&2; exit 1; }

# ── 1. Detect agent variant ────────────────────────────────────────────────
detect_agent() {
    # Prefer Agent 2 if both are installed (newer, supports more plugins).
    if systemctl list-unit-files zabbix-agent2.service >/dev/null 2>&1 \
       && systemctl is-enabled zabbix-agent2 >/dev/null 2>&1; then
        echo "agent2"; return
    fi
    if systemctl list-unit-files zabbix-agent.service >/dev/null 2>&1 \
       && systemctl is-enabled zabbix-agent >/dev/null 2>&1; then
        echo "agent1"; return
    fi
    # Fallback: check binaries (in case systemd unit is custom-named)
    if command -v zabbix_agent2 >/dev/null 2>&1; then echo "agent2"; return; fi
    if command -v zabbix_agentd >/dev/null 2>&1; then echo "agent1"; return; fi
    echo "none"
}

VARIANT="$(detect_agent)"
case "$VARIANT" in
    agent2)
        SVC="zabbix-agent2"
        MAIN_CONF="/etc/zabbix/zabbix_agent2.conf"
        D_DIR="/etc/zabbix/zabbix_agent2.d"
        NEEDS_ENABLE_REMOTE=0
        ;;
    agent1)
        SVC="zabbix-agent"
        MAIN_CONF="/etc/zabbix/zabbix_agentd.conf"
        D_DIR="/etc/zabbix/zabbix_agentd.d"
        NEEDS_ENABLE_REMOTE=1
        ;;
    none)
        echo "no Zabbix agent found." >&2
        echo "  Install one of:" >&2
        echo "    sudo apt install zabbix-agent2     # recommended" >&2
        echo "    sudo apt install zabbix-agent      # legacy v1" >&2
        exit 1
        ;;
esac

echo "[1/5] detected $VARIANT  (service: $SVC, config: $MAIN_CONF)"

# ── 2. Drop AllowKey include file ──────────────────────────────────────────
echo "[2/5] installing $D_DIR/zabbix-ai-diag.conf"
install -d -m 0755 "$D_DIR"
install -m 0644 "$CONF_SRC" "$D_DIR/zabbix-ai-diag.conf"

# Make sure the main config actually reads from the .d directory.
if [ -f "$MAIN_CONF" ] && ! grep -qE "^[[:space:]]*Include[[:space:]]*=[[:space:]]*$D_DIR" "$MAIN_CONF"; then
    echo "      adding Include= line to $MAIN_CONF"
    {
        echo ""
        echo "# Added by zabbix-rca-AI installer"
        echo "Include=$D_DIR/*.conf"
    } >> "$MAIN_CONF"
fi

# ── 3. Agent 1 only: enable system.run ─────────────────────────────────────
if [ "$NEEDS_ENABLE_REMOTE" -eq 1 ] && [ -f "$MAIN_CONF" ]; then
    if grep -qE "^[[:space:]]*EnableRemoteCommands[[:space:]]*=" "$MAIN_CONF"; then
        # Force to 1 in case it's currently 0
        sed -i -E "s|^[[:space:]]*EnableRemoteCommands[[:space:]]*=.*|EnableRemoteCommands=1|" "$MAIN_CONF"
        echo "[3/5] ensured EnableRemoteCommands=1 in $MAIN_CONF (Agent 1 requirement)"
    else
        {
            echo ""
            echo "# Added by zabbix-rca-AI installer — Agent 1 needs this for system.run"
            echo "EnableRemoteCommands=1"
        } >> "$MAIN_CONF"
        echo "[3/5] added EnableRemoteCommands=1 to $MAIN_CONF (Agent 1 requirement)"
    fi
else
    echo "[3/5] EnableRemoteCommands not needed (Agent 2 uses AllowKey only)"
fi

# ── 4. Install runtime dependencies ────────────────────────────────────────
echo "[4/5] installing extra packages (smartmontools, sysstat)"
if   command -v apt >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt -qq install -y smartmontools sysstat >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf -q install -y smartmontools sysstat >/dev/null
elif command -v yum >/dev/null 2>&1; then
    yum -q install -y smartmontools sysstat >/dev/null
else
    echo "      no apt/dnf/yum found — install smartmontools + sysstat manually" >&2
fi

# Sudoers entry — exact binary path, NOPASSWD, validated with visudo.
SMARTCTL_PATH="$(command -v smartctl 2>/dev/null || echo /usr/sbin/smartctl)"
cat > "$SUDOERS_DST" <<EOF
# Allow zabbix agent to read SMART health for diag.smart (NOPASSWD, exact path).
zabbix ALL=(root) NOPASSWD: $SMARTCTL_PATH
EOF
chmod 0440 "$SUDOERS_DST"
visudo -cf "$SUDOERS_DST" >/dev/null

# ── 5. Restart ─────────────────────────────────────────────────────────────
echo "[5/5] restarting $SVC"
systemctl restart "$SVC"
sleep 1
systemctl is-active "$SVC" >/dev/null && echo "      ✓ $SVC is active"

cat <<EOF

Done ($VARIANT). Verify from the Zabbix server:
  zabbix_get -s <this-host-ip> -k agent.version
  zabbix_get -s <this-host-ip> -k agent.variant     # 1 = Agent 1, 2 = Agent 2

Then trigger a test investigation from the Zabbix frontend
(right-click a host → Investigate with AI).
EOF
