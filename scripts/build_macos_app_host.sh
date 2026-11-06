#!/bin/bash
# Build the lightweight AppKit launcher; it uses only the public Runtime ABI.
# Usage: scripts/build_macos_app_host.sh [output]
set -euo pipefail
HUSK_APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HUSK_APP_OUTPUT="${1:-$HUSK_APP_ROOT/build/macos-host/HuskAppHost}"
if [ "$#" -gt 1 ]; then
    echo "usage: $0 [output]" >&2
    exit 2
fi
mkdir -p "$(dirname "$HUSK_APP_OUTPUT")"
xcrun --sdk macosx clang -arch arm64 -fobjc-arc -O1 -g -Wall -Wextra -Werror \
    -Wno-deprecated-declarations "$HUSK_APP_ROOT/tools/macos/HuskAppHost.m" \
    -framework AppKit -framework Foundation -framework QuartzCore -framework Metal \
    -o "$HUSK_APP_OUTPUT"
codesign --force --sign - --timestamp=none "$HUSK_APP_OUTPUT"
codesign --verify --strict "$HUSK_APP_OUTPUT"
echo "$HUSK_APP_OUTPUT"
