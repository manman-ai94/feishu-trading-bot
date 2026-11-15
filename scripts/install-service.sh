#!/usr/bin/env bash
# Installs the bot as a systemd service on Linux: it starts now, starts again
# if it ever stops, and starts by itself after a reboot.
# Usage, as root:  bash /path/to/feishu-trading-bot/scripts/install-service.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
NAME=feishu-trading-bot
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
RUN_AS="$(stat -c %U "$DIR")"  # the service runs as the owner of the project folder

if [ ! -x "$DIR/.venv/bin/python" ]; then
    echo "[X] Run scripts/setup.sh first."
    exit 1
fi
if [ ! -f "$DIR/.env" ]; then
    echo "[X] $DIR/.env is missing. Run scripts/setup.sh, then fill it in."
    exit 1
fi
if [ "$UNIT_DIR" = /etc/systemd/system ] && [ "$(id -u)" -ne 0 ]; then
    echo "[X] This script must be run as root."
    exit 1
fi

{
    echo "[Unit]"
    echo "Description=Feishu trading assistant"
    echo "After=network-online.target"
    echo "Wants=network-online.target"
    echo
    echo "[Service]"
    echo "Type=simple"
    echo "User=$RUN_AS"
    echo "WorkingDirectory=$DIR"
    echo "ExecStart=$DIR/.venv/bin/python -m bot run"
    echo "Restart=always"
    echo "RestartSec=15"
    echo "NoNewPrivileges=true"
    echo
    echo "[Install]"
    echo "WantedBy=multi-user.target"
} > "$UNIT_DIR/$NAME.service"
echo "[OK] Wrote $UNIT_DIR/$NAME.service (the bot runs as user: $RUN_AS)"

if [ "$UNIT_DIR" = /etc/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable "$NAME"
    systemctl restart "$NAME"
    sleep 3
    systemctl --no-pager --lines=5 status "$NAME" || true
    echo
    echo "[OK] The bot is installed as a service."
    echo "     Look at it:   systemctl status $NAME"
    echo "     Its log:      tail -n 50 $DIR/logs/bot.log"
    echo "     Restart it:   systemctl restart $NAME"
    echo "     Stop it:      systemctl stop $NAME"
fi
