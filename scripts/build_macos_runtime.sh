#!/bin/bash
# Build and publish a versioned macOS headless-bootstrap Runtime package.
# Usage: ./scripts/build_macos_runtime.sh [--store DIR] [--build-id ID]
# See --help for resource inputs. Requires Apple Silicon macOS and Xcode tools.
set -euo pipefail
HUSK_RUNTIME_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$HUSK_RUNTIME_ROOT/tools/runtime/package_macos.py" "$@"
