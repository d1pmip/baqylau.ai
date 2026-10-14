#!/usr/bin/env bash
# Baqylau Web — teacher/admin panel (Linux/macOS).
# Run on the teacher PC:  ./start_server.sh  -> http://<lan-ip>:5050
# First login: admin / admin123
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "[Baqylau] python3 not found in PATH" >&2
  exit 1
fi
echo "[Baqylau] Panel at http://127.0.0.1:5050  (LAN: check 'ip addr' IPv4)"
echo "[Baqylau] First login: admin / admin123"
exec python3 -m server.app
