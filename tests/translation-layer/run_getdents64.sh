#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
HUSK_TASK_TMPDIR=$(python3 -B "$ROOT/tools/task_paths.py" getdents64)
TMPDIR="$HUSK_TASK_TMPDIR/tmp"
TMP="$TMPDIR"; TEMP="$TMPDIR"
CLANG_MODULE_CACHE_PATH="$TMPDIR/clang-module-cache"
export HUSK_TASK_TMPDIR TMPDIR TMP TEMP CLANG_MODULE_CACHE_PATH
OUT=$(mktemp -d "$TMPDIR/getdents64-XXXXXX")
trap 'case "$OUT" in "$HUSK_TASK_TMPDIR"/tmp/getdents64-*) rm -rf "$OUT";; esac' EXIT
"$ROOT/tools/regress/build.sh" tests/translation-layer/getdents64.c "$OUT/getdents64"
"$OUT/getdents64" "$ROOT/build/android-system/cacerts"
