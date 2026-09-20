#!/bin/sh
# Build gateway-kh.app — a native macOS menu bar application.
#
# Produces a real .app bundle in /Applications, so the gateway can be run,
# stopped and configured without ever opening a terminal.
#
# Usage:
#   ./menubar/build.sh              build and install to /Applications
#   ./menubar/build.sh --no-install build only, into ./menubar/build

set -eu

HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(dirname -- "$HERE")"
BUILD="$HERE/build"
APP_NAME="GatewayKH"
APP="$BUILD/gateway-kh.app"

INSTALL=1
[ "${1:-}" = "--no-install" ] && INSTALL=0

echo "==> cleaning"
# A plain `rm -rf` intermittently fails with "Directory not empty": something
# outside this script (Spotlight indexing the freshly written bundle, or a
# Finder window on it) recreates entries inside the tree while it is being
# removed. That aborted an entire build under `set -e`. Retry, and treat a
# stubborn leftover as non-fatal because mkdir and the later copy will fill it.
for _ in 1 2 3 4 5; do
    rm -rf "$BUILD" 2>/dev/null || true
    [ -d "$BUILD" ] || break
    sleep 0.5
done
rm -rf "$BUILD" 2>/dev/null || true
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> compiling menu bar app (Swift)"
# -swift-version 5: the code targets the Swift 5 language mode deliberately.
# Swift 6's strict concurrency checking would reject the small amount of shared
# mutable state this single-threaded AppKit app relies on, for no safety gain
# here -- everything touching the UI already runs on the main thread.
swiftc \
  -swift-version 5 \
  -O \
  -framework Cocoa \
  -framework WebKit \
  -o "$APP/Contents/MacOS/$APP_NAME" \
  "$HERE"/*.swift

# ---------------------------------------------------------------------------
# Bundle a relocatable Python and the gateway source.
#
# The venv in a checkout is a symlink to Homebrew's Python and its stdlib lives
# outside it, so it cannot be copied into a bundle. python-build-standalone
# ships a self-contained interpreter that runs from wherever it is unpacked --
# which is what makes the finished .app movable to another Mac.
# ---------------------------------------------------------------------------
PY_VERSION="${GATEWAY_PYTHON_VERSION:-3.11.16}"
PY_TAG="${GATEWAY_PYTHON_TAG:-20260901}"
PY_ARCH="${GATEWAY_PYTHON_ARCH:-aarch64}"
PY_ASSET="${GATEWAY_PYTHON_ASSET:-cpython-${PY_VERSION}+${PY_TAG}-${PY_ARCH}-apple-darwin-install_only.tar.gz}"
PY_URL="${GATEWAY_PYTHON_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/${PY_ASSET}}"

VENDOR="$HERE/vendor"
RUNTIME_CACHE="$VENDOR/runtime"
mkdir -p "$VENDOR"

if [ ! -f "$VENDOR/$PY_ASSET" ]; then
  echo "==> fetching relocatable Python $PY_VERSION (once, ~26 MB)"
  curl -fL --retry 2 -o "$VENDOR/$PY_ASSET.part" "$PY_URL"
  mv "$VENDOR/$PY_ASSET.part" "$VENDOR/$PY_ASSET"
fi

# GitHub publishes this digest for the default release asset. Custom assets can
# provide their published digest through GATEWAY_PYTHON_SHA256 instead.
PYTHON_SHA256="${GATEWAY_PYTHON_SHA256:-}"
case "$PYTHON_SHA256" in
  sha256:*) PYTHON_SHA256="${PYTHON_SHA256#sha256:}" ;;
esac
if [ -z "$PYTHON_SHA256" ] \
    && [ "$PY_URL" = "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.11.16+20260901-aarch64-apple-darwin-install_only.tar.gz" ]; then
  PYTHON_SHA256="50424fa409e8ae84b82a3052522f64695b47dff2158b70bb7358e0ebd6c085c9"
fi
PYTHON_ASSET_SHA256="$(shasum -a 256 "$VENDOR/$PY_ASSET" | cut -d ' ' -f1)"
[ -z "$PYTHON_SHA256" ] || [ "$PYTHON_ASSET_SHA256" = "$PYTHON_SHA256" ] || {
  echo "ERROR: downloaded Python archive checksum is $PYTHON_ASSET_SHA256, expected $PYTHON_SHA256" >&2
  exit 1
}

# Rebuild the cached runtime when either the tested dependency input or the
# selected Python asset changes. The lock is included because requirements.txt
# points at it, and the asset/architecture are included because the same
# dependency list can be installed into different runtimes.
[ -f "$ROOT/requirements.lock" ] || {
  echo "ERROR: missing dependency lock $ROOT/requirements.lock" >&2
  exit 1
}
REQS_HASH="$({ cat "$ROOT/requirements.txt"; printf '\0'; cat "$ROOT/requirements.lock"; } | shasum -a 256 | cut -c1-16)"
STAMP="$RUNTIME_CACHE/.runtime-${PY_ASSET}-${PYTHON_ASSET_SHA256}-${PY_ARCH}-${REQS_HASH}"

if [ ! -x "$RUNTIME_CACHE/python/bin/python3" ] || [ ! -f "$STAMP" ]; then
  echo "==> preparing bundled Python runtime (once per dependency change)"
  rm -rf "$RUNTIME_CACHE"
  mkdir -p "$RUNTIME_CACHE"
  tar -xzf "$VENDOR/$PY_ASSET" -C "$RUNTIME_CACHE"
  "$RUNTIME_CACHE/python/bin/python3" -m pip install \
    --quiet --no-warn-script-location --disable-pip-version-check \
    -r "$ROOT/requirements.txt"
  # Drop pip's caches and bytecode; they are not part of the shipped runtime.
  find "$RUNTIME_CACHE/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  touch "$STAMP"
fi

RUNTIME_PYTHON="$RUNTIME_CACHE/python/bin/python3"
RUNTIME_VERSION="$($RUNTIME_PYTHON --version 2>&1)"
[ "$RUNTIME_VERSION" = "Python $PY_VERSION" ] || {
  echo "ERROR: bundled runtime is $RUNTIME_VERSION, expected Python $PY_VERSION" >&2
  exit 1
}
RUNTIME_MACHINE="$($RUNTIME_PYTHON -c 'import platform; print(platform.machine())')"
case "$PY_ARCH" in
  aarch64|arm64) EXPECTED_MACHINE="arm64" ;;
  *) EXPECTED_MACHINE="$PY_ARCH" ;;
esac
[ "$RUNTIME_MACHINE" = "$EXPECTED_MACHINE" ] || {
  echo "ERROR: bundled runtime architecture is $RUNTIME_MACHINE, expected $EXPECTED_MACHINE ($PY_ARCH)" >&2
  exit 1
}
echo "    runtime $RUNTIME_VERSION (machine $RUNTIME_MACHINE; asset $PY_ARCH; $PY_ASSET)"
echo "    archive sha256:$PYTHON_ASSET_SHA256"
echo "    dependency input sha256:$REQS_HASH (requirements.txt + requirements.lock)"

echo "==> bundling Python runtime"
rm -rf "$APP/Contents/Resources/python"
cp -R "$RUNTIME_CACHE/python" "$APP/Contents/Resources/python"

echo "==> bundling gateway source"
rm -rf "$APP/Contents/Resources/gateway"
mkdir -p "$APP/Contents/Resources/gateway/data"
cp -R "$ROOT/gateway" "$APP/Contents/Resources/gateway/gateway"
cp "$ROOT/requirements.txt" "$APP/Contents/Resources/gateway/"
cp "$ROOT/requirements.lock" "$APP/Contents/Resources/gateway/"
# Shipped read-only defaults. User data lives in Application Support.
[ -f "$ROOT/data/voices.json" ] && cp "$ROOT/data/voices.json" "$APP/Contents/Resources/gateway/data/"

echo "==> stamping the build"
# One identity, written in two places the app and the gateway each read, so
# "is the thing running this?" is answerable instead of assumed. A stale gateway
# serving old code through a rebuild is invisible without this — measured: one ran
# for two hours across four rebuilds. The stamp is the commit plus the minute it
# was built, because two builds of the same commit are still two builds.
BUILD_ID="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)-$(date -u +%Y%m%d%H%M)"
echo "$BUILD_ID" > "$APP/Contents/Resources/gateway/BUILD"
echo "    build $BUILD_ID"

echo "==> bundling what's new"
cp "$HERE/whats-new.md" "$APP/Contents/Resources/whats-new.md"

echo "==> writing Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>gateway-kh</string>
    <key>CFBundleDisplayName</key><string>gateway-kh</string>
    <key>CFBundleIdentifier</key><string>kh.gateway.menubar</string>
    <key>CFBundleExecutable</key><string>GatewayKH</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>$BUILD_ID</string>
    <key>GatewayBuild</key><string>$BUILD_ID</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <!-- Background app: no Dock icon, no main window. -->
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

echo "==> generating icon"
"$RUNTIME_PYTHON" "$HERE/make_icon.py" "$APP/Contents/Resources/AppIcon.icns"
[ -s "$APP/Contents/Resources/AppIcon.icns" ] || {
  echo "ERROR: icon generation produced no AppIcon.icns" >&2
  exit 1
}

echo "==> ad-hoc signing"
# Unsigned bundles are quarantined by Gatekeeper when copied around. An ad-hoc
# signature is enough for local use and keeps the LaunchAgent happy.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

if [ "$INSTALL" = "1" ]; then
  # Prefer the system /Applications folder. `~/Applications` is hidden by
  # default in Finder, absent from the sidebar, and easy to miss — an app in
  # there is installed and running but effectively unfindable, which defeats
  # the point of shipping an app at all.
  if [ -w /Applications ]; then
    DEST="/Applications/gateway-kh.app"
  else
    DEST="$HOME/Applications/gateway-kh.app"
    mkdir -p "$HOME/Applications"
  fi

  echo "==> installing to $DEST"
  rm -rf "$DEST"
  cp -R "$APP" "$DEST"
  # Drop any quarantine flag so first launch does not warn.
  xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
  codesign --verify --deep --strict --verbose=2 "$DEST"

  # If it was previously installed in the other location, remove that copy so
  # there is never more than one, and the login item points at the survivor.
  for OTHER in "/Applications/gateway-kh.app" "$HOME/Applications/gateway-kh.app"; do
    [ "$OTHER" != "$DEST" ] && rm -rf "$OTHER"
  done

  echo
  echo "Installed: $DEST"
  echo "It is in Launchpad and Spotlight, and shows as a bolt icon in the menu bar."
  echo "Open it with:  open \"$DEST\""
else
  echo
  echo "Built: $APP"
fi
