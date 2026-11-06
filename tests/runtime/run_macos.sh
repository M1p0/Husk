#!/bin/sh
# Local-only M1 integration checks; no CI service, root, or device is required.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
exec python3 "$ROOT/tests/runtime/test_macos_runtime.py" "$@"
