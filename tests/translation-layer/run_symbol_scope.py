#!/usr/bin/env python3
"""Verify strong load-root overrides for a dependency's weak allocator pair."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--runtime", type=Path, required=True)
parser.add_argument("--linker", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--preload-dependency", action="store_true", help="verify an existing library keeps its first load group")
args = parser.parse_args()
task_directory("symbol-scope-test")
out = temporary_path(args.out); out.mkdir(parents=True, exist_ok=False)
linker = args.linker.resolve(strict=True)
environment = dict(os.environ)
flavor = ["-flavor", "gnu"] if linker.name == "rust-lld" else []
if flavor:
    environment["DYLD_LIBRARY_PATH"] = str(linker.parents[3])

def run(command):
    result = subprocess.run([str(value) for value in command], env=environment, capture_output=True, text=True, timeout=30)
    with (out / "build.log").open("a") as log: log.write(result.stdout + result.stderr)
    result.check_returncode()

for name in ("scope_child", "scope_owner"):
    run(["xcrun", "clang", "--target=aarch64-linux-android29", "-O1", "-fPIC", "-fno-builtin", "-c",
         ROOT / "tests/translation-layer/fixtures" / (name + ".c"), "-o", out / (name + ".o")])
run([linker, *flavor, "-shared", "--hash-style=both", "-soname", "libscope-child.so", out / "scope_child.o", "-o", out / "libscope-child.so"])
run([linker, *flavor, "-shared", "--hash-style=both", "-soname", "libscope-owner.so", out / "scope_owner.o",
     "--no-as-needed", "-L" + str(out), "-l:libscope-child.so", "-o", out / "libscope-owner.so"])
runtime = ctypes.CDLL(str(args.runtime.resolve(strict=True) / "lib/libHuskRuntime.dylib"))
runtime.tl_set_data_dir.argtypes = [ctypes.c_char_p]
runtime.tl_set_data_dir(bytes(out))
runtime.tl_ld_load.argtypes = [ctypes.c_char_p]; runtime.tl_ld_load.restype = ctypes.c_void_p
runtime.tl_ld_sym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]; runtime.tl_ld_sym.restype = ctypes.c_void_p
if args.preload_dependency:
    if not runtime.tl_ld_load(bytes(out / "libscope-child.so")):
        raise RuntimeError("preloaded child failed to load")
owner = runtime.tl_ld_load(bytes(out / "libscope-owner.so"))
if not owner: raise RuntimeError("fixture failed to load")
test = runtime.tl_ld_sym(owner, b"scope_owner_test")
if not test: raise RuntimeError("fixture test symbol missing")
result = ctypes.CFUNCTYPE(ctypes.c_int)(test)()
if args.preload_dependency:
    runtime.tl_ld_find_lib.argtypes = [ctypes.c_char_p]; runtime.tl_ld_find_lib.restype = ctypes.c_void_p
    child = runtime.tl_ld_find_lib(b"libscope-child.so")
    own_test = runtime.tl_ld_sym(child, b"child_own_test")
    passed = result == 0 and own_test and ctypes.CFUNCTYPE(ctypes.c_int)(own_test)() == 1
    report = {"preloaded_dependency_kept_original_allocator_pair": bool(passed)}
else:
    passed = result == 1
    report = {"strong_owner_allocator_pair": passed, "protected_definition": passed}
(out / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
if not passed: raise RuntimeError("allocator binding did not match the original load group")
print("PASS: preloaded dependency retained its original group" if args.preload_dependency else
      "PASS: load-root allocator and deallocator stay paired; protected dependency symbols stay local")
