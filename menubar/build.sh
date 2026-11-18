#!/bin/sh
# Build Gateway Studio KH (installed as gateway-kh.app) — a native macOS menu bar application.
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
APP_NAME="Gateway Studio KH"
APP="$BUILD/gateway-kh.app"

INSTALL=1
RENDER_AUTHORITY="descriptor-only"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-install)
      INSTALL=0
      shift
      ;;
    --render-authority)
      [ "$#" -ge 2 ] || { echo "ERROR: --render-authority requires a value" >&2; exit 2; }
      RENDER_AUTHORITY="$2"
      shift 2
      ;;
    *)
      echo "ERROR: unsupported build argument: $1" >&2
      exit 2
      ;;
  esac
done
case "$RENDER_AUTHORITY" in
  tool-only|descriptor-only|sealed-candidate) ;;
  *) echo "ERROR: unsupported render authority mode: $RENDER_AUTHORITY" >&2; exit 2 ;;
esac

# Preserve replaced build/install files; the owner's archive is never cleaned.
TRASH_BUILD="$ROOT/_trash/build/$(date -u +%Y%m%d%H%M%S)-$$"
preserve_path() {
  [ -e "$1" ] || [ -L "$1" ] || return 0
  case "$1" in
    "$ROOT/_trash"|"$ROOT/_trash/"*) echo "ERROR: refusing to move an archive" >&2; exit 1 ;;
    "$ROOT/"*) RELATIVE="${1#"$ROOT/"}" ;;
    *) RELATIVE="external/${1#/}" ;;
  esac
  mkdir -p "$ROOT/_trash"
  ARCHIVE_CANONICAL="$(CDPATH= cd -P -- "$ROOT/_trash" && pwd -P)"
  SOURCE_PARENT="$(CDPATH= cd -P -- "$(dirname "$1")" && pwd -P)"
  case "$SOURCE_PARENT" in
    "$ARCHIVE_CANONICAL"|"$ARCHIVE_CANONICAL/"*) echo "ERROR: refusing to move an archive" >&2; exit 1 ;;
  esac
  ARCHIVE_ROOT="${2:-$TRASH_BUILD}"
  mkdir -p "$ARCHIVE_ROOT"
  ARCHIVE_ITEM="$(mktemp -d "$ARCHIVE_ROOT/item-XXXXXX")"
  DESTINATION="$ARCHIVE_ITEM/$RELATIVE"
  mkdir -p "$(dirname "$DESTINATION")"
  mv "$1" "$DESTINATION"
}

echo "==> preserving previous build"
preserve_path "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> compiling menu bar app (Swift)"
# -swift-version 5: the code targets the Swift 5 language mode deliberately.
# Swift 6's strict concurrency checking would reject the small amount of shared
# mutable state this single-threaded AppKit app relies on, for no safety gain
# here -- everything touching the UI already runs on the main thread.
#
# **The deployment target must be explicit.** Without `-target`, `swiftc` stamps
# the executable's `LC_BUILD_VERSION` with the *SDK* version -- macOS 27 on this
# toolchain -- and LaunchServices enforces that over Info.plist's
# `LSMinimumSystemVersion`. Measured: a binary built with no target reports
# `minos 27.0`, and macOS 14 refuses it with "requires macOS 27". The same code
# compiles cleanly back to 11.0, so state the floor rather than inherit it.
# `GATEWAY_APP_ARCH` lets the same script build an Intel slice.
TARGET_ARCH="${GATEWAY_APP_ARCH:-$(uname -m)}"
MACOS_MIN="${GATEWAY_MACOS_MIN:-11.0}"
swiftc \
  -swift-version 5 \
  -O \
  -target "${TARGET_ARCH}-apple-macos${MACOS_MIN}" \
  -framework Cocoa \
  -framework WebKit \
  -framework AVFoundation \
  -framework AVKit \
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
PY_ARCH="${GATEWAY_PYTHON_ARCH:-}"
if [ -z "$PY_ARCH" ]; then
  case "$TARGET_ARCH" in
    arm64|aarch64) PY_ARCH="aarch64" ;;
    *) PY_ARCH="$TARGET_ARCH" ;;
  esac
fi
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
  preserve_path "$RUNTIME_CACHE"
  mkdir -p "$RUNTIME_CACHE"
  tar -xzf "$VENDOR/$PY_ASSET" -C "$RUNTIME_CACHE"
  "$RUNTIME_CACHE/python/bin/python3" -m pip install \
    --quiet --no-warn-script-location --disable-pip-version-check \
    -r "$ROOT/requirements.txt"
  # Drop pip's caches and bytecode; they are not part of the shipped runtime.
  find "$RUNTIME_CACHE/python" -name "__pycache__" -type d -prune -print | while IFS= read -r CACHE; do preserve_path "$CACHE"; done
  touch "$STAMP"
fi

RUNTIME_PYTHON="$RUNTIME_CACHE/python/bin/python3"
RUNTIME_VERSION="$("$RUNTIME_PYTHON" --version 2>&1)"
[ "$RUNTIME_VERSION" = "Python $PY_VERSION" ] || {
  echo "ERROR: bundled runtime is $RUNTIME_VERSION, expected Python $PY_VERSION" >&2
  exit 1
}
RUNTIME_MACHINE="$("$RUNTIME_PYTHON" -c 'import platform; print(platform.machine())')"
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
preserve_path "$APP/Contents/Resources/python"
cp -R "$RUNTIME_CACHE/python" "$APP/Contents/Resources/python"

# pip writes some console scripts with an absolute interpreter shebang into this build directory,
# which would ship the builder's home path inside the app. The runtime already ships the
# script-relative `#!/bin/sh` polyglot for its own scripts; put every entry point on it.
echo "==> console scripts use the bundled interpreter, not the build path"
"$RUNTIME_PYTHON" "$HERE/tools/strip_python_headers.py" "$APP/Contents/Resources/python"

# **The bundled interpreter never writes bytecode into its own bundle.** The app is code-signed,
# so a `.pyc` written under `Contents/Resources/python` invalidates the seal and macOS SIGKILLs
# the app at its next launch. `-B` covers the one process the app spawns; this covers every other
# caller of this interpreter — a script, an MCP client, a person at a shell — because
# `sitecustomize` is imported by `site` on every start. Measured on 1301 on 2026-11-17: 9
# `__pycache__` directories, `codesign -v` = "a sealed resource is missing or invalid", and the
# app killed at launch; a bare `bin/python3 -c "import email"` reproduced 64 files.
PY_SHORT="${PY_VERSION%.*}"
SITE_PACKAGES="$APP/Contents/Resources/python/lib/python$PY_SHORT/site-packages"
if [ -d "$SITE_PACKAGES" ]; then
  cat > "$SITE_PACKAGES/sitecustomize.py" <<'SITECUSTOMIZE'
# Generated by menubar/build.sh. The app bundle is signed: bytecode written inside it breaks the
# seal and the app is killed at its next launch. `-B` covers the process the app spawns; this
# covers every other way this interpreter is started.
import sys

sys.dont_write_bytecode = True
SITECUSTOMIZE
fi

echo "==> bundling gateway source"
preserve_path "$APP/Contents/Resources/gateway"
mkdir -p "$APP/Contents/Resources/gateway/data"
cp -R "$ROOT/gateway" "$APP/Contents/Resources/gateway/gateway"
# The MCP agent connector: a stdlib-only python3 script an MCP client launches. It ships
# beside the gateway source so an installed app has it at a stable path. Guarded like
# `data/voices.json` below: a fixture build root that copies only what it needs still builds.
[ -d "$ROOT/connectors" ] && cp -R "$ROOT/connectors" "$APP/Contents/Resources/gateway/connectors"
cp "$ROOT/requirements.txt" "$APP/Contents/Resources/gateway/"
cp "$ROOT/requirements.lock" "$APP/Contents/Resources/gateway/"
# Shipped read-only defaults. User data lives in Application Support.
[ -f "$ROOT/data/voices.json" ] && cp "$ROOT/data/voices.json" "$APP/Contents/Resources/gateway/data/"
[ -f "$ROOT/data/developer-test-v1.schema.json" ] && cp "$ROOT/data/developer-test-v1.schema.json" "$APP/Contents/Resources/gateway/data/"
[ -f "$ROOT/data/developer-gate-v1.schema.json" ] && cp "$ROOT/data/developer-gate-v1.schema.json" "$APP/Contents/Resources/gateway/data/"

# Authority is an explicit build choice. The normal build ignores even a populated source tree;
# selected states require an exact closed set and preserve the source bytes in the app bundle.
if [ "$RENDER_AUTHORITY" != "tool-only" ]; then
  AUTHORITY_SOURCE="$ROOT/data/final-render"
  [ -d "$AUTHORITY_SOURCE" ] && [ ! -L "$AUTHORITY_SOURCE" ] || {
    echo "ERROR: selected render authority source is unavailable" >&2
    exit 1
  }
  case "$RENDER_AUTHORITY" in
    descriptor-only) AUTHORITY_COUNT=3 ;;
    sealed-candidate) AUTHORITY_COUNT=3 ;;
  esac
  ACTUAL_AUTHORITY_COUNT="$(find "$AUTHORITY_SOURCE" -mindepth 1 -maxdepth 1 -print | wc -l | tr -d '[:space:]')"
  [ "$ACTUAL_AUTHORITY_COUNT" = "$AUTHORITY_COUNT" ] || {
    echo "ERROR: $RENDER_AUTHORITY requires exactly $AUTHORITY_COUNT authority files" >&2
    exit 1
  }
  for NAME in profile.json tools.json compiler.json; do
    SOURCE_FILE="$AUTHORITY_SOURCE/$NAME"
    [ -f "$SOURCE_FILE" ] && [ ! -L "$SOURCE_FILE" ] || {
      echo "ERROR: unsafe or missing render authority file: $NAME" >&2
      exit 1
    }
    SOURCE_SIZE="$(wc -c < "$SOURCE_FILE" | tr -d '[:space:]')"
    SOURCE_MODE="$(stat -f '%Lp' "$SOURCE_FILE")"
    [ "$SOURCE_SIZE" -ge 2 ] && [ "$SOURCE_SIZE" -le 1048576 ] \
      && [ "$SOURCE_MODE" = "644" ] || {
        echo "ERROR: invalid render authority file: $NAME" >&2
        exit 1
      }
  done

  PROFILE_SHA256="$(shasum -a 256 "$AUTHORITY_SOURCE/profile.json" | cut -d ' ' -f1)"
  TOOLS_SHA256="$(shasum -a 256 "$AUTHORITY_SOURCE/tools.json" | cut -d ' ' -f1)"
  COMPILER_SHA256="$(shasum -a 256 "$AUTHORITY_SOURCE/compiler.json" | cut -d ' ' -f1)"

  # Use only the pure strict JSON validator. Importing gateway.runtime here would
  # create stores/workers during a build and turn data validation into application startup.
  validate_render_authority() {
  PYTHONPATH="$ROOT" "$RUNTIME_PYTHON" - "$@" <<'PY'
import hashlib
from pathlib import Path
import sys

from gateway.render_hash_v1 import decode_render_json


def invalid(reason):
    raise ValueError("invalid render authority: " + reason)


def exact(value, keys, name):
    if type(value) is not dict or set(value) != set(keys):
        invalid(name + " has missing or unknown fields")
    return value


def digest(value, name):
    if (type(value) is not str or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        invalid(name + " is not lowercase SHA-256")
    return value


def positive(value, name):
    if type(value) is not int or not 1 <= value <= 9_007_199_254_740_991:
        invalid(name + " must be a positive safe integer")


root = Path(sys.argv[1])
raw = {name: (root / name).read_bytes() for name in (
    "profile.json", "tools.json", "compiler.json"
)}
expected = {
    "profile.json": sys.argv[2],
    "tools.json": sys.argv[3],
    "compiler.json": sys.argv[4],
}
for name, payload in raw.items():
    if hashlib.sha256(payload).hexdigest() != expected[name]:
        invalid(name + " changed before validation")
tools = exact(decode_render_json(raw["tools.json"]), (
    "format", "platform", "ffmpeg_sha256", "ffmpeg_size_bytes",
    "ffprobe_sha256", "ffprobe_size_bytes", "architecture_evidence_sha256",
    "minimum_os_evidence_sha256", "linked_libraries_evidence_sha256",
    "build_configuration_evidence_sha256", "capability_inventory_evidence_sha256",
), "tools")
if tools["format"] != "studio.render.tools.v1" or tools["platform"] != "macos-arm64":
    invalid("tools fixed fields")
for key in (
    "ffmpeg_sha256", "ffprobe_sha256", "architecture_evidence_sha256",
    "minimum_os_evidence_sha256", "linked_libraries_evidence_sha256",
    "build_configuration_evidence_sha256", "capability_inventory_evidence_sha256",
):
    digest(tools[key], "tools." + key)
positive(tools["ffmpeg_size_bytes"], "tools.ffmpeg_size_bytes")
positive(tools["ffprobe_size_bytes"], "tools.ffprobe_size_bytes")

compiler = exact(decode_render_json(raw["compiler.json"]), (
    "format", "implementation", "python_runtime_sha256", "render_hash_sha256",
    "render_contract_sha256", "final_render_compiler_sha256",
), "compiler")
fixed_compilers = {
    "studio.render.profile.v1": "hub-final-render-v1",
    "studio.render.late-label.v3": "hub-final-render-late-label-v3",
    "studio.render.subtitle.v1": "hub-final-render-subtitle-v1",
}
if sys.argv[5] not in fixed_compilers:
    invalid("authority contract")
if (compiler["format"] != "studio.render.compiler.v1"
        or compiler["implementation"] != fixed_compilers[sys.argv[5]]):
    invalid("compiler fixed fields")
for key in (
    "python_runtime_sha256", "render_hash_sha256", "render_contract_sha256",
    "final_render_compiler_sha256",
):
    digest(compiler[key], "compiler." + key)

profile = exact(decode_render_json(raw["profile.json"]), (
    "format", "implementation", "recipe_namespace", "compiler_artifact_sha256",
    "tools_artifact_sha256", "combination_set", "tier_policy", "output_policy",
    "execution_bounds", "hardware_qualification_record_sha256",
    "fidelity_record_sha256", "production_eligible",
), "profile")
fixed_profiles = {
    "studio.render.profile.v1": {
        "format": "studio.render.profile.v1", "implementation": "story-classic-no-font-v1",
        "recipe_namespace": "studio.render.final.v1",
        "combination_set": "retained-classic-no-font-candidate-v1",
        "tier_policy": "story-final-chain-v1", "output_policy": "story-final-output-v1",
    },
    "studio.render.late-label.v3": {
        "format": "studio.render.profile.v3", "implementation": "story-classic-late-label-v3",
        "recipe_namespace": "studio.render.final.v1",
        "combination_set": "retained-classic-late-label-candidate-v3",
        "tier_policy": "story-final-chain-v1", "output_policy": "story-final-output-v1",
    },
    "studio.render.subtitle.v1": {
        "format": "studio.render.profile.v4", "implementation": "story-classic-subtitle-v1",
        "recipe_namespace": "studio.render.final.v1",
        "combination_set": "retained-classic-subtitle-candidate-v1",
        "tier_policy": "story-final-chain-v1", "output_policy": "story-final-output-v1",
    },
}
if any(profile[key] != value for key, value in fixed_profiles[sys.argv[5]].items()):
    invalid("profile fixed fields")
if profile["production_eligible"] is not False:
    invalid("profile production_eligible")
for key in ("hardware_qualification_record_sha256", "fidelity_record_sha256"):
    if profile[key] is not None:
        digest(profile[key], "profile." + key)
if (profile["tools_artifact_sha256"] != hashlib.sha256(raw["tools.json"]).hexdigest()
        or profile["compiler_artifact_sha256"]
        != hashlib.sha256(raw["compiler.json"]).hexdigest()):
    invalid("descriptor dependency digests")
subtitle = sys.argv[5] == "studio.render.subtitle.v1"
bound_keys = {
    "canvas_pairs", "working_fps", "max_duration_sec", "max_operations",
    "max_assets", "max_asset_bytes", "max_total_input_bytes", "max_pixel_frames",
    "scratch_reserve_bytes", "output_limit_bytes", "process_tree_rss_limit_bytes",
    "cumulative_wall_seconds",
}
if subtitle:
    bound_keys |= {"max_subtitle_fonts", "max_subtitle_font_bytes", "max_subtitle_ass_bytes"}
bounds = profile["execution_bounds"]
if type(bounds) is dict and "bounds_version" in bounds:
    bounds = exact(bounds, bound_keys - {"canvas_pairs"} | {"bounds_version", "canvas_range"},
                   "profile.execution_bounds")
    if type(bounds["bounds_version"]) is not int or bounds["bounds_version"] != 2:
        invalid("profile execution bounds version")
    canvas = exact(bounds["canvas_range"], ("max_edge", "max_pixels"), "canvas_range")
    for key in canvas:
        positive(canvas[key], "canvas_range." + key)
    if canvas["max_edge"] < 2 or canvas["max_edge"] % 2 or canvas["max_pixels"] < 4:
        invalid("profile canvas range")
else:
    if subtitle:
        invalid("profile execution bounds version")
    bounds = exact(bounds, bound_keys, "profile.execution_bounds")
    if type(bounds["canvas_pairs"]) is not list or not bounds["canvas_pairs"]:
        invalid("profile execution canvas pairs")
    for pair in bounds["canvas_pairs"]:
        pair = exact(pair, ("width", "height"), "profile.execution_bounds.canvas")
        positive(pair["width"], "canvas.width")
        positive(pair["height"], "canvas.height")
if type(bounds["working_fps"]) is not list or not bounds["working_fps"]:
    invalid("profile execution fps")
for rate in bounds["working_fps"]:
    positive(rate, "working_fps")
for key in bound_keys - {"canvas_pairs", "working_fps", "max_duration_sec"}:
    positive(bounds[key], "profile.execution_bounds." + key)
if (type(bounds["max_duration_sec"]) not in (int, float)
        or isinstance(bounds["max_duration_sec"], bool)
        or bounds["max_duration_sec"] <= 0):
    invalid("profile max_duration_sec")

PY
  }
  validate_render_authority "$AUTHORITY_SOURCE" \
    "$PROFILE_SHA256" "$TOOLS_SHA256" "$COMPILER_SHA256" "studio.render.profile.v1"

  AUTHORITY_DESTINATION="$APP/Contents/Resources/gateway/data/final-render"
  mkdir "$AUTHORITY_DESTINATION"
  for NAME in profile.json tools.json compiler.json; do
    case "$NAME" in
      profile.json) EXPECTED_SHA256="$PROFILE_SHA256" ;;
      tools.json) EXPECTED_SHA256="$TOOLS_SHA256" ;;
      compiler.json) EXPECTED_SHA256="$COMPILER_SHA256" ;;
    esac
    cp "$AUTHORITY_SOURCE/$NAME" "$AUTHORITY_DESTINATION/$NAME"
    [ "$(shasum -a 256 "$AUTHORITY_SOURCE/$NAME" | cut -d ' ' -f1)" \
      = "$EXPECTED_SHA256" ] \
      && [ "$(shasum -a 256 "$AUTHORITY_DESTINATION/$NAME" | cut -d ' ' -f1)" \
      = "$EXPECTED_SHA256" ] || {
        echo "ERROR: render authority file changed while staging: $NAME" >&2
        exit 1
      }
  done
  # Only the three descriptors are staged. There is no qualification vector: the Gateway
  # runtime never reads one (capabilities come from the Hub-published candidate, profile
  # facts from the descriptors), and requiring a fabricated one would block the accepted
  # descriptor-only default. The exact 3-name directory is the runtime's tamper property.

  LATE_LABEL_SOURCE="$ROOT/data/final-render-late-label"
  if [ -e "$LATE_LABEL_SOURCE" ] || [ -L "$LATE_LABEL_SOURCE" ]; then
    [ -d "$LATE_LABEL_SOURCE" ] && [ ! -L "$LATE_LABEL_SOURCE" ] || {
      echo "ERROR: unsafe late-label render authority source" >&2
      exit 1
    }
    LATE_LABEL_COUNT="$(find "$LATE_LABEL_SOURCE" -mindepth 1 -maxdepth 1 -print | wc -l | tr -d '[:space:]')"
    [ "$LATE_LABEL_COUNT" = 3 ] || {
      echo "ERROR: late-label render authority requires exactly three files" >&2
      exit 1
    }
    for NAME in profile.json tools.json compiler.json; do
      SOURCE_FILE="$LATE_LABEL_SOURCE/$NAME"
      [ -f "$SOURCE_FILE" ] && [ ! -L "$SOURCE_FILE" ] \
        && [ "$(wc -c < "$SOURCE_FILE" | tr -d '[:space:]')" -ge 2 ] \
        && [ "$(wc -c < "$SOURCE_FILE" | tr -d '[:space:]')" -le 1048576 ] \
        && [ "$(stat -f '%Lp' "$SOURCE_FILE")" = 644 ] || {
          echo "ERROR: invalid late-label render authority file: $NAME" >&2
          exit 1
        }
    done
    LATE_PROFILE_SHA256="$(shasum -a 256 "$LATE_LABEL_SOURCE/profile.json" | cut -d ' ' -f1)"
    LATE_TOOLS_SHA256="$(shasum -a 256 "$LATE_LABEL_SOURCE/tools.json" | cut -d ' ' -f1)"
    LATE_COMPILER_SHA256="$(shasum -a 256 "$LATE_LABEL_SOURCE/compiler.json" | cut -d ' ' -f1)"
    validate_render_authority "$LATE_LABEL_SOURCE" \
      "$LATE_PROFILE_SHA256" "$LATE_TOOLS_SHA256" "$LATE_COMPILER_SHA256" \
      "studio.render.late-label.v3"
    LATE_LABEL_DESTINATION="$APP/Contents/Resources/gateway/data/final-render-late-label"
    mkdir "$LATE_LABEL_DESTINATION"
    for NAME in profile.json tools.json compiler.json; do
      cp "$LATE_LABEL_SOURCE/$NAME" "$LATE_LABEL_DESTINATION/$NAME"
      [ "$(shasum -a 256 "$LATE_LABEL_SOURCE/$NAME" | cut -d ' ' -f1)" \
        = "$(shasum -a 256 "$LATE_LABEL_DESTINATION/$NAME" | cut -d ' ' -f1)" ] || {
          echo "ERROR: late-label render authority changed while staging: $NAME" >&2
          exit 1
        }
    done
  fi

  SUBTITLE_SOURCE="$ROOT/data/final-render-subtitle"
  if [ -e "$SUBTITLE_SOURCE" ] || [ -L "$SUBTITLE_SOURCE" ]; then
    [ -d "$SUBTITLE_SOURCE" ] && [ ! -L "$SUBTITLE_SOURCE" ] || {
      echo "ERROR: unsafe subtitle render authority source" >&2
      exit 1
    }
    SUBTITLE_COUNT="$(find "$SUBTITLE_SOURCE" -mindepth 1 -maxdepth 1 -print | wc -l | tr -d '[:space:]')"
    [ "$SUBTITLE_COUNT" = 3 ] || {
      echo "ERROR: subtitle render authority requires exactly three files" >&2
      exit 1
    }
    for NAME in profile.json tools.json compiler.json; do
      SOURCE_FILE="$SUBTITLE_SOURCE/$NAME"
      [ -f "$SOURCE_FILE" ] && [ ! -L "$SOURCE_FILE" ] \
        && [ "$(wc -c < "$SOURCE_FILE" | tr -d '[:space:]')" -ge 2 ] \
        && [ "$(wc -c < "$SOURCE_FILE" | tr -d '[:space:]')" -le 1048576 ] \
        && [ "$(stat -f '%Lp' "$SOURCE_FILE")" = 644 ] || {
          echo "ERROR: invalid subtitle render authority file: $NAME" >&2
          exit 1
        }
    done
    SUBTITLE_PROFILE_SHA256="$(shasum -a 256 "$SUBTITLE_SOURCE/profile.json" | cut -d ' ' -f1)"
    SUBTITLE_TOOLS_SHA256="$(shasum -a 256 "$SUBTITLE_SOURCE/tools.json" | cut -d ' ' -f1)"
    SUBTITLE_COMPILER_SHA256="$(shasum -a 256 "$SUBTITLE_SOURCE/compiler.json" | cut -d ' ' -f1)"
    validate_render_authority "$SUBTITLE_SOURCE" \
      "$SUBTITLE_PROFILE_SHA256" "$SUBTITLE_TOOLS_SHA256" "$SUBTITLE_COMPILER_SHA256" \
      "studio.render.subtitle.v1"
    SUBTITLE_DESTINATION="$APP/Contents/Resources/gateway/data/final-render-subtitle"
    mkdir "$SUBTITLE_DESTINATION"
    for NAME in profile.json tools.json compiler.json; do
      cp "$SUBTITLE_SOURCE/$NAME" "$SUBTITLE_DESTINATION/$NAME"
      [ "$(shasum -a 256 "$SUBTITLE_SOURCE/$NAME" | cut -d ' ' -f1)" \
        = "$(shasum -a 256 "$SUBTITLE_DESTINATION/$NAME" | cut -d ' ' -f1)" ] || {
          echo "ERROR: subtitle render authority changed while staging: $NAME" >&2
          exit 1
        }
    done
  fi
fi

echo "==> stamping the build"
# One identity, written in two places the app and the gateway each read, so
# "is the thing running this?" is answerable instead of assumed. A stale gateway
# serving old code through a rebuild is invisible without this — measured: one ran
# for two hours across four rebuilds. The stamp is the commit plus the minute it
# was built, because two builds of the same commit are still two builds.
BUILD_ID="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)-$(date -u +%Y%m%d%H%M)"
echo "$BUILD_ID" > "$APP/Contents/Resources/gateway/BUILD"

# Launch at login is a **launchd agent plist the app writes into ~/Library/LaunchAgents**
# (`menubar/main.swift`, `LaunchAgent`). The bundled SMAppService agent that lived here could not
# spawn an ad-hoc-signed build: macOS records the requirement (the cdhash) at registration, and an
# update changes it, so launchd refused the app with `OS_REASON_CODESIGNING` on 1301 (2026-11-16).
# A plain agent runs a path and is not held to a code requirement. No certificate, no Keychain item.
#
# The bundle still carries the agent plist, but only so `SMAppService.agent(plistName:)` can find
# it: an older build registered `kh.gateway.menubar` through SMAppService, and that registration
# can only be cleared by a bundle that has the plist. With the plist gone the status was
# `.notFound`, the old row was never cleared, the label stayed taken and the plist could never be
# imported (measured on 1301, 2026-11-17). The app never registers through SMAppService again
# (owner ruling 2026-11-16: stay certificate-free); the keys below are the ones
# `LaunchAgent.writePlist` writes, so the two never disagree (`tests/test_login_default.py`).
# `BundleProgram` is Apple's documented bundle-relative program path, so the plist survives the
# app being moved.
LAGENTS="$APP/Contents/Library/LaunchAgents"
mkdir -p "$LAGENTS"
cat > "$LAGENTS/kh.gateway.menubar.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>kh.gateway.menubar</string>
	<key>BundleProgram</key>
	<string>Contents/MacOS/${APP_NAME}</string>
	<key>ProgramArguments</key>
	<array>
		<string>Contents/MacOS/${APP_NAME}</string>
		<string>--background</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>ThrottleInterval</key>
	<integer>15</integer>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
</dict>
</plist>
PLIST
# `gateway.__version__` (and so `/api/version`) reads `VERSION` from beside the package.
[ -f "$ROOT/VERSION" ] && cp "$ROOT/VERSION" "$APP/Contents/Resources/gateway/VERSION"
echo "    build $BUILD_ID"

# The version a person names ("which one is installed?"), separate from the exact
# build stamp. One file, one place to bump; the app shows both, and the DMG name
# carries both so the latest is obvious in dist/.
MARKETING_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || true)"
[ -n "$MARKETING_VERSION" ] || MARKETING_VERSION="0.0.0"
echo "    version $MARKETING_VERSION"

echo "==> bundling what's new"
cp "$ROOT/CHANGELOG.md" "$APP/Contents/Resources/CHANGELOG.md"
cp "$ROOT/CHANGELOG.md" "$APP/Contents/Resources/gateway/CHANGELOG.md"

echo "==> writing Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Gateway Studio KH</string>
    <key>CFBundleDisplayName</key><string>Gateway Studio KH</string>
    <key>CFBundleIdentifier</key><string>kh.gateway.menubar</string>
    <key>CFBundleExecutable</key><string>Gateway Studio KH</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$MARKETING_VERSION</string>
    <key>CFBundleVersion</key><string>$BUILD_ID</string>
    <key>GatewayBuild</key><string>$BUILD_ID</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>$MACOS_MIN</string>
    <key>NSHighResolutionCapable</key><true/>
    <!-- A regular app: Dock icon and Cmd-Tab, with the menu bar bolt still there
         as a convenience. Closing the window keeps the app (and gateway) alive. -->
    <key>LSUIElement</key><false/>
</dict>
</plist>
PLIST

# The app is installed as `gateway-kh.app`, so its Login Items row and Finder name come from
# these two keys; a packaging run must not ship a bundle whose visible name has drifted back to
# the file name. (Owner, 2026-11-13: Login Items showed "gateway-kh" with a generic icon.)
for KEY in CFBundleName CFBundleDisplayName; do
  VALUE=$(/usr/libexec/PlistBuddy -c "Print :$KEY" "$APP/Contents/Info.plist" 2>/dev/null)
  [ "$VALUE" = "Gateway Studio KH" ] || {
    echo "ERROR: $KEY is '$VALUE', not 'Gateway Studio KH'" >&2
    exit 1
  }
done

echo "==> generating icon"
"$RUNTIME_PYTHON" "$HERE/make_icon.py" "$APP/Contents/Resources/AppIcon.icns"
[ -s "$APP/Contents/Resources/AppIcon.icns" ] || {
  echo "ERROR: icon generation produced no AppIcon.icns" >&2
  exit 1
}

# A proper helper bundle carries the gateway service, so macOS names and icons it
# instead of showing a bare "Python". The bundle identifier is the helper's; the
# executable keeps `kh.gateway.python`, the stable requirement it already had.
echo "==> building the Gateway Studio KH Helper"
PY_HELPER="$APP/Contents/Resources/python/bin/python3"
if [ -L "$PY_HELPER" ]; then PY_HELPER="$(dirname "$PY_HELPER")/$(readlink "$PY_HELPER")"; fi
HELPER="$APP/Contents/Helpers/Gateway Studio KH Helper.app"
HELPER_EXECUTABLE="$HELPER/Contents/MacOS/Gateway Studio KH Helper"
mkdir -p "$HELPER/Contents/MacOS" "$HELPER/Contents/Resources"
cp "$PY_HELPER" "$HELPER_EXECUTABLE"
chmod 755 "$HELPER_EXECUTABLE"
cp "$APP/Contents/Resources/AppIcon.icns" "$HELPER/Contents/Resources/AppIcon.icns"
cat > "$HELPER/Contents/Info.plist" <<HELPER_PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Gateway Studio KH Helper</string>
    <key>CFBundleDisplayName</key><string>Gateway Studio KH Helper</string>
    <key>CFBundleIdentifier</key><string>kh.gateway.helper</string>
    <key>CFBundleExecutable</key><string>Gateway Studio KH Helper</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$MARKETING_VERSION</string>
    <key>CFBundleVersion</key><string>$BUILD_ID</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>$MACOS_MIN</string>
    <key>NSHighResolutionCapable</key><true/>
    <!-- A background service: no Dock icon, no menu, no window. -->
    <key>LSUIElement</key><true/>
</dict>
</plist>
HELPER_PLIST

# The shared KH identity (_orchestra/developer-handoff/SIGNING.md) gives a designated
# requirement that survives a rebuild, unlike an ad-hoc cdhash. Nested code is signed
# first, then the bundle; --deep is only ever a verification flag, never a signing one.
# GATEWAY_SIGN_IDENTITY="" signs ad-hoc; a named identity that is missing fails closed.
SIGN_ID="${GATEWAY_SIGN_IDENTITY-KH Studio Local Signing}"
if [ -n "$SIGN_ID" ] && ! security find-identity -p codesigning | grep -qF "\"$SIGN_ID\""; then
  echo "ERROR: signing identity \"$SIGN_ID\" is not available to codesign (GATEWAY_SIGN_IDENTITY= signs ad-hoc)" >&2
  exit 1
fi
SIGN="${SIGN_ID:--}"
# The changelog is appended every release and ships verbatim, so a later entry can put back a name
# an earlier release scrubbed (0.2.282 did exactly that). Scan the whole Resources tree rather than
# a list of paths: every leak found tonight was in text this build *copies*, so a new copy added
# later must be covered without anyone remembering to add it here. Runs last, so the tree scanned
# is the tree that is signed and imaged. Rules live in menubar/tools/no_private_strings.py,
# shared with the release test.
echo "==> checking the bundle for owner data"
"$RUNTIME_PYTHON" "$HERE/tools/no_private_strings.py" "$APP/Contents/Resources"

echo "==> signing with ${SIGN_ID:-ad-hoc}"
# Development/import probes may have created bytecode before bundling. These
# generated files are not shipped; the launcher uses -B to keep the seal intact.
find "$APP/Contents/Resources" -type d -name "__pycache__" -prune -print | while IFS= read -r CACHE; do preserve_path "$CACHE"; done

# **A `__pycache__` file in every directory that could receive one.** Python cannot create a
# directory where a file of that name exists and skips the cache silently — so not one byte can be
# written into the signed bundle, whoever starts the interpreter. Measured on a real bundle: the
# bare repro (`python3 -c "import email, urllib.request"`, environment clean) writes **0** `.pyc`,
# against 65 without it. A read-only runtime also stops the writes but makes the installed bundle
# impossible to `rm -rf`, which the updater's cleanup needs; this leaves it fully replaceable and
# removable. `sitecustomize.py` is still shipped, so no run even tries.
find "$APP/Contents/Resources/python" -name '*.py' -exec dirname {} \; 2>/dev/null | sort -u \
  | while IFS= read -r DIR; do [ -e "$DIR/__pycache__" ] || : > "$DIR/__pycache__"; done
find "$APP/Contents/Resources" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 \
  | xargs -0 -n 50 codesign --force --timestamp=none --sign "$SIGN"
# The runtime copy keeps the stable identifier it always had. The helper bundle is
# signed as a bundle: that seals its executable under the helper's identifier, so
# signing the executable alone would be undone. Nested code first, then the app.
codesign --force --timestamp=none --sign "$SIGN" --identifier kh.gateway.python "$PY_HELPER"
codesign --force --timestamp=none --sign "$SIGN" "$HELPER"
codesign --force --timestamp=none --sign "$SIGN" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

# Each build is a **candidate**, not an accepted release: it lands outside the
# canonical accepted names (`dist/Gateway Studio KH-<version>.dmg`), because a
# same-version rebuild that fails must not cost either of the two verified DMGs
# kept in canonical dist/. `scripts/accept_release.py` promotes the exact
# candidate after install, launch and acceptance through the shared
# release-dist helper. The build stamp still lives inside the app
# (`CFBundleVersion` / `GatewayBuild`, which Check Gateway reads) and in the
# candidate's name, so the accepted source can always be named exactly.
# `hdiutil` ships with macOS, so this needs nothing installed. The app is ad-hoc
# signed and the image is not notarised, so a Mac that did not build it still
# needs the right-click Open — the same step the other projects' DMGs need.
DIST="$ROOT/dist"
CANDIDATES="$DIST/.candidates"
mkdir -p "$CANDIDATES"
# Two builds can share the minute-based stamp; never overwrite a candidate under verification.
CANDIDATE_DIR="$(mktemp -d "$CANDIDATES/${BUILD_ID}.XXXXXX")"
CANDIDATE="$CANDIDATE_DIR/Gateway Studio KH-${MARKETING_VERSION}-${BUILD_ID}.dmg"
DMG_STAGE="$(mktemp -d "$CANDIDATE_DIR/stage.XXXXXX")"
cp -R "$APP" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create -volname "Gateway Studio KH" -srcfolder "$DMG_STAGE" -format UDZO "$CANDIDATE" >/dev/null
preserve_path "$DMG_STAGE"
echo "Candidate DMG: $CANDIDATE"
echo "     (canonical dist/ is untouched until this build is accepted)"

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
  preserve_path "$DEST"
  cp -R "$APP" "$DEST"
  # Drop any quarantine flag so first launch does not warn.
  xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
  codesign --verify --deep --strict --verbose=2 "$DEST"

  # If it was previously installed in the other location, preserve that copy so
  # there is never more than one, and the login item points at the survivor.
  for OTHER in "/Applications/gateway-kh.app" "$HOME/Applications/gateway-kh.app"; do
    if [ "$OTHER" != "$DEST" ]; then preserve_path "$OTHER"; fi
  done

  echo
  echo "Installed: $DEST"
  echo "It is in Launchpad and Spotlight, and shows as a bolt icon in the menu bar."
  echo "Open it with:  open \"$DEST\""
else
  echo
  echo "Built: $APP"
fi
