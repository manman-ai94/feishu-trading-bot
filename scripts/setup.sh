#!/usr/bin/env bash
# One-time setup on Linux: creates the virtual environment and installs the packages.
# Usage:  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
    echo "[X] python3 was not found. On Ubuntu, as root: apt install -y python3 python3-venv"
    exit 1
fi
if [ ! -x .venv/bin/pip ]; then
    rm -rf .venv
    if ! python3 -m venv .venv; then
        rm -rf .venv
        echo "[X] Could not create the virtual environment. On Ubuntu, as root: apt install -y python3-venv"
        exit 1
    fi
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
if [ ! -f .env ]; then
    cp .env.example .env
fi
chmod 600 .env  # only this user may read the keys
echo
echo "[OK] Setup finished. Next: fill in the keys (nano .env), then run:"
echo "     .venv/bin/python -m bot selfcheck"
