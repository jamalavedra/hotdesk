#!/bin/bash
set -euo pipefail
export DISPLAY=:1
until xdpyinfo >/dev/null 2>&1; do sleep 1; done
exec /opt/venv/bin/python3 -m computer_server --host 127.0.0.1 --port 8000
