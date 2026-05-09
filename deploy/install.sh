#!/usr/bin/env bash
# zabbix-ai bootstrap installer — Ubuntu 22.04/24.04
#
# Idempotent: safe to re-run. Creates user, installs deps, lays out
# /etc/zabbix-ai/, installs systemd unit, nginx site, certbot.
# Does NOT start the service yet — secrets must be filled into the env
# file first. Run `systemctl start zabbix-ai && systemctl enable zabbix-ai`
# once secrets are in place.
#
# Usage (run as root on the target VM, repo already cloned at /opt/zabbix-ai):
#   sudo bash /opt/zabbix-ai/deploy/install.sh <hostname>
# Example:
#   sudo bash /opt/zabbix-ai/deploy/install.sh zabbix-ai.lsnw.io

set -euo pipefail

HOSTNAME="${1:?usage: install.sh <hostname>}"
APP_USER="zabbix-ai"
APP_HOME="/opt/zabbix-ai"
VENV_DIR="$APP_HOME/.venv"
ETC_DIR="/etc/zabbix-ai"
DATA_DIR="/var/lib/zabbix-ai"
LOG_DIR="/var/log/zabbix-ai"
NGINX_SITE="/etc/nginx/sites-available/zabbix-ai"
SYSTEMD_UNIT="/etc/systemd/system/zabbix-ai.service"

log() { printf '[install] %s\n' "$*" >&2; }

[ "$EUID" -eq 0 ] || { log "must run as root"; exit 1; }

log "OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3.12 python3.12-venv python3.12-dev \
    nginx certbot python3-certbot-nginx \
    git curl ca-certificates \
    >/dev/null

log "system user $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || \
    useradd -r -s /usr/sbin/nologin -d "$APP_HOME" "$APP_USER"

log "directories"
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_HOME"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$ETC_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$LOG_DIR"

log "ownership of repo"
chown -R "$APP_USER:$APP_USER" "$APP_HOME"

log "Python venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    sudo -u "$APP_USER" python3.12 -m venv "$VENV_DIR"
fi
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip --quiet
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -e "$APP_HOME" --quiet

log "config files"
if [ ! -f "$ETC_DIR/config.yaml" ]; then
    install -o "$APP_USER" -g "$APP_USER" -m 0640 \
        "$APP_HOME/config.example.yaml" "$ETC_DIR/config.yaml"
fi
if [ ! -f "$ETC_DIR/env" ]; then
    install -o "$APP_USER" -g "$APP_USER" -m 0640 \
        "$APP_HOME/.env.example" "$ETC_DIR/env"
fi

log "systemd unit"
cat > "$SYSTEMD_UNIT" <<UNIT
[Unit]
Description=Zabbix RCA AI service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_HOME
Environment=ZABBIX_AI_CONFIG=$ETC_DIR/config.yaml
EnvironmentFile=$ETC_DIR/env
ExecStart=$VENV_DIR/bin/uvicorn zabbix_ai.app:app --host 127.0.0.1 --port 8088
Restart=on-failure
RestartSec=5s
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA_DIR $LOG_DIR
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload

log "nginx site"
cat > "$NGINX_SITE" <<NGINX
# zabbix-ai — HTTPS reverse proxy
# Certbot will rewrite this with TLS lines on first run.
server {
    listen 80;
    server_name $HOSTNAME;

    # ACME http-01 challenges
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # Slack-only path: HMAC-verified by the app, no upstream auth
    location /slack/events {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 90s;
    }

    # SSE endpoint for the Zabbix UI investigation page
    location /investigate/stream {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
    }

    # Everything else (admin UI, /investigate page, /healthz, /hostbill)
    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 90s;
    }
}
NGINX
ln -sf "$NGINX_SITE" "/etc/nginx/sites-enabled/zabbix-ai"
nginx -t
systemctl reload nginx

log "certbot — issue cert for $HOSTNAME"
if [ ! -d "/etc/letsencrypt/live/$HOSTNAME" ]; then
    certbot --nginx --non-interactive --agree-tos \
        --email "noc@leapswitch.com" -d "$HOSTNAME" --redirect \
        || { log "certbot failed — service will run on http until cert issued"; }
fi

log "DONE — next steps:"
log "  1. Edit $ETC_DIR/env and put real secrets:"
log "       ANTHROPIC_API_KEY, ZABBIX_TOKEN_*, URL_SIGNING_KEY,"
log "       SESSION_SECRET, BOOTSTRAP_ADMIN_PASSWORD, SLACK_BOT_TOKEN, ..."
log "  2. Edit $ETC_DIR/config.yaml — adjust zabbix_instances, enable"
log "       admin: / slack: / zabbix_ui: blocks as needed."
log "  3. systemctl enable --now zabbix-ai"
log "  4. Visit https://$HOSTNAME/healthz and https://$HOSTNAME/admin/login"
