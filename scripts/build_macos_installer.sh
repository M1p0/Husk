#!/bin/bash
set -euo pipefail
HUSK_INSTALLER_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 -B "$HUSK_INSTALLER_ROOT/tools/macos/build_installer.py" "$@"
