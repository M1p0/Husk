#!/bin/bash
# Build the lightweight AppKit launcher; it uses only the public Runtime ABI.
# Usage: scripts/build_macos_app_host.sh [output]
set -euo pipefail
HUSK_APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HUSK_APP_TASK="$(python3 -B "$HUSK_APP_ROOT/tools/task_paths.py" macos-host)"
export HUSK_TASK_TMPDIR="$HUSK_APP_TASK" TMPDIR="$HUSK_APP_TASK/tmp" TMP="$HUSK_APP_TASK/tmp" TEMP="$HUSK_APP_TASK/tmp"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$HUSK_APP_TASK/tmp/pycache"
export CLANG_MODULE_CACHE_PATH="$HUSK_APP_TASK/tmp/clang-module-cache" SWIFT_MODULECACHE_PATH="$HUSK_APP_TASK/tmp/swift-module-cache"
export CFFIXED_USER_HOME="$HUSK_APP_TASK/tmp/cocoa-home" XDG_CACHE_HOME="$HUSK_APP_TASK/tmp/cache"
HUSK_APP_OUTPUT="${1:-$HUSK_APP_TASK/tmp/host/HuskAppHost}"
if [ "$#" -gt 1 ]; then
    echo "usage: $0 [output]" >&2
    exit 2
fi
HUSK_APP_OUTPUT="$(PYTHONPATH="$HUSK_APP_ROOT/tools" python3 -B -c 'import sys; from task_paths import temporary_path; print(temporary_path(sys.argv[1]))' "$HUSK_APP_OUTPUT")"
mkdir -p "$(dirname "$HUSK_APP_OUTPUT")"
xcrun --sdk macosx clang -arch arm64 -fobjc-arc -O1 -g -Wall -Wextra -Werror \
    -Wno-deprecated-declarations "$HUSK_APP_ROOT/tools/macos/HuskAppHost.m" \
    -framework AppKit -framework Foundation -framework QuartzCore -framework Metal \
    -o "$HUSK_APP_OUTPUT"
codesign --force --sign - --timestamp=none "$HUSK_APP_OUTPUT"
codesign --verify --strict "$HUSK_APP_OUTPUT"
echo "$HUSK_APP_OUTPUT"
