#!/usr/bin/env bash
# Downloads the newest code and installs any new packages (Linux).
# Usage:  bash scripts/update.sh    then, as root:  systemctl restart feishu-trading-bot
set -euo pipefail
cd "$(dirname "$0")/.."
git pull
.venv/bin/python -m pip install -r requirements.txt
echo
echo "[OK] Updated. Now restart the bot, as root: systemctl restart feishu-trading-bot"
