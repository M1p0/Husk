#!/usr/bin/env python3
"""Test concurrent native audio output with a deterministic AudioQueue device."""
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory

out = task_directory("audio-mix") / "evidence/audio-mix"
out.mkdir(parents=True, exist_ok=True)
binary = out / "audio-mix"
command = ["xcrun", "--sdk", "macosx", "clang", "-g", "-O1", "-Wall", "-Wextra", "-Wno-deprecated-declarations",
           "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(ROOT / "tests/translation-layer/audio_mix.c"), "-o", str(binary)]
with (out / "build.log").open("w") as log:
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
if result.returncode:
    print((out / "build.log").read_text())
    result.check_returncode()
result = subprocess.run([str(binary)], env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"),
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
(out / "results.log").write_text(result.stdout)
print(result.stdout, end="")
result.check_returncode()
