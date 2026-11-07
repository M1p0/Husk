#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# macOS host test; unrelated runtime entry points are not invoked.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
HUSK_TASK_TMPDIR=$(python3 -B "$ROOT/tools/task_paths.py" font-ca-paths)
TMPDIR="$HUSK_TASK_TMPDIR/tmp"
TMP="$TMPDIR"; TEMP="$TMPDIR"
CLANG_MODULE_CACHE_PATH="$TMPDIR/clang-module-cache"
export HUSK_TASK_TMPDIR TMPDIR TMP TEMP CLANG_MODULE_CACHE_PATH
OUT=$(mktemp -d "$TMPDIR/font-ca-paths-XXXXXX")
trap 'case "$OUT" in "$HUSK_TASK_TMPDIR"/tmp/font-ca-paths-*) rm -rf "$OUT";; esac' EXIT
clang -O1 -ffunction-sections -fdata-sections -I"$ROOT/src/translation-layer-next" -I"$ROOT/src/translation-layer" \
  "$ROOT/tests/translation-layer/font_paths.c" "$ROOT/src/translation-layer-next/husk-tl-bionic-io.c" \
  -Wl,-dead_strip -Wl,-undefined,dynamic_lookup -o "$OUT/font_paths"
"$OUT/font_paths"
