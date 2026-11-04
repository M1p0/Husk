#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# macOS: exercise the real dual-mapped allocator and the Foundation-only persistence policy.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
clang -Wall -Wextra -Werror -pthread -I"$ROOT/src/translation-layer-next" -I"$ROOT/src/translation-layer" \
    "$ROOT/tests/translation-layer/jit_budget.c" "$ROOT/src/translation-layer-next/husk-tl-xmem.c" -o "$OUT/allocator"
"$OUT/allocator" "$OUT/exhaustion.json"
swiftc "$ROOT/src/app/Husk/JITMemoryBudget.swift" "$ROOT/tests/translation-layer/jit_budget.swift" -o "$OUT/policy"
"$OUT/policy"
