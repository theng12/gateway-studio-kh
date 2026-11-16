#!/bin/bash
# A drivable Fleet Trash page, offscreen, with fixture trash and no real Mac touched.
#
#   menubar/render/trash-fixture.sh [port]        # then open the URL it prints
#
# It runs `fixture_server.py`: a disposable real Gateway on a loopback port, its Fleet read
# answered from `fleet_fixture.py` (which now carries Hub Trash / macOS Trash figures: full,
# stated empty, a Trash the Hub cannot read, and Macs that report nothing), and its
# `POST /admin/api/fleet/trash/empty` answered with one of each outcome — emptied, skipped
# (REMOTE_EMPTY_OFF, NODE_UNREACHABLE, QUEUED) and a partial failure that says what is left.
#
# Nothing is installed, no window opens, and the owner's Gateway is never read or written.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../.." && pwd)
PORT="${1:-0}"
PY="${GATEWAY_RENDER_PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in "$ROOT/.venv/bin/python" "$ROOT/../repository/.venv/bin/python"; do
    [ -x "$candidate" ] && PY="$candidate" && break
  done
fi
[ -n "$PY" ] || { echo "no Python with the gateway's requirements; set GATEWAY_RENDER_PYTHON" >&2; exit 1; }
if [ "$PORT" = "0" ]; then
  PORT=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
fi
WORK=$(mktemp -d)
SUPPORT="$WORK/support"
mkdir -p "$SUPPORT"
printf '{"gatewayDir":"%s","port":%s,"autoStartGateway":false,"hasLaunchedBefore":true,"keepGatewayRunning":false}' \
  "$ROOT" "$PORT" > "$SUPPORT/menubar.json"
echo "fixture work dir: $WORK"
echo "opening at http://127.0.0.1:$PORT/admin/ once the gateway answers (Fleet → Trash)"
echo "stop it with Ctrl-C"
exec "$PY" "$HERE/fixture_server.py" --port "$PORT" --support "$SUPPORT"
