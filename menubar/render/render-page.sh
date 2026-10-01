#!/bin/bash
# Render pages of the Gateway app to PNG, offscreen. Never opens the app, never touches the owner's Gateway.
#
#   menubar/render/render-page.sh [page[,page...]] [output-dir] [WxH ...]
#
# Pages are the sidebar names: overview, audio, subtitle, image, music, video, chat, jobs, analytics, voices,
# models, providers, fleet, settings, content-packs, whatsnew - `fleet:overview|locations|machines|models|jobs`
# picks a Fleet tab, and `all` renders every one. Defaults: overview, /tmp/gateway-render, 1100x800 and 1600x1000.
# GATEWAY_RENDER_APPEARANCE=light|dark|both (default light).
#
# It starts fixture_server.py (a disposable real gateway plus the synthetic fleet in fleet_fixture.py) on a
# random local port, points a throwaway copy of the app at it through a temp GATEWAY_APP_SUPPORT, and stops it.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../.." && pwd)
PAGE="${1:-overview}"
[ "$PAGE" = "all" ] && PAGE="overview,audio,subtitle,image,music,video,chat,jobs,analytics,voices,models,providers,fleet:overview,fleet:locations,fleet:machines,fleet:models,fleet:jobs,fleet-groups,fleet-groups:problems,settings,content-packs,whatsnew"
OUT="${2:-/tmp/gateway-render}"
shift 2 2>/dev/null || true
PY="${GATEWAY_RENDER_PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in "$ROOT/.venv/bin/python" "$ROOT/../repository/.venv/bin/python"; do
    [ -x "$candidate" ] && PY="$candidate" && break
  done
fi
[ -n "$PY" ] || { echo "no Python with the gateway's requirements; set GATEWAY_RENDER_PYTHON" >&2; exit 1; }

WORK=$(mktemp -d)
SERVER_PID=""
trap '[ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null; true' EXIT
PORT=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
SUPPORT="$WORK/support"
mkdir -p "$SUPPORT"
printf '{"gatewayDir":"%s","port":%s,"autoStartGateway":false,"hasLaunchedBefore":true,"keepGatewayRunning":false}' \
  "$ROOT" "$PORT" > "$SUPPORT/menubar.json"

(cd "$ROOT" && "$PY" menubar/render/fixture_server.py --port "$PORT" --support "$SUPPORT") > "$WORK/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 150); do grep -q '^READY' "$WORK/server.log" 2>/dev/null && break; sleep 0.2; done
grep -q '^READY' "$WORK/server.log" || { cat "$WORK/server.log" >&2; exit 1; }

mkdir -p "$WORK/src"
cp "$ROOT/menubar/"*.swift "$WORK/src/"
"$PY" "$HERE/patch_sources.py" "$WORK/src"
cp "$HERE/render_main.swift" "$WORK/src/main.swift"
cp "$HERE/FleetGroupsMock.swift" "$WORK/src/FleetGroupsMock.swift"
SDK=$(xcrun --sdk macosx --show-sdk-path)
xcrun swiftc -swift-version 5 -sdk "$SDK" -target "$(uname -m)-apple-macos13.0" \
  -framework Cocoa -framework WebKit -framework AVFoundation -framework AVKit \
  "$WORK/src"/*.swift -o "$WORK/render-gateway"

for MODE in $([ "${GATEWAY_RENDER_APPEARANCE:-light}" = both ] && echo "light dark" || echo "${GATEWAY_RENDER_APPEARANCE:-light}"); do
  GATEWAY_RENDER_APPEARANCE=$MODE GATEWAY_APP_SUPPORT="$SUPPORT" "$WORK/render-gateway" "$PAGE" "$OUT" "$@"
done
