#!/usr/bin/env python3
"""Build and run real guest-facing temporary-path adapters; no APK or GUI."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, help="Evidence directory inside the current task directory")
args = parser.parse_args()
TASK = task_directory("bionic-temp-paths")
OUT = temporary_path(args.out if args.out is not None else TASK / "evidence" / "bionic-temp-paths")
OUT.mkdir(parents=True, exist_ok=True)
CHILD_ENV = dict(os.environ, TMPDIR=str(TASK / "tmp"), TMP=str(TASK / "tmp"), TEMP=str(TASK / "tmp"),
                 TL_TMP=str(TASK / "tmp"), HUSK_TASK_TMPDIR=str(TASK))
binary = OUT / "temp-paths-test"
with (OUT / "build.log").open("w") as log:
    subprocess.run(["bash", ROOT / "tools/regress/build.sh", "tests/translation-layer/temp_paths.c", binary],
                   cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, env=CHILD_ENV)
with (OUT / "results.log").open("w") as log:
    result = subprocess.run([binary, TASK / "tmp"], cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=30, env=CHILD_ENV)
    log.write(result.stdout)
    print(result.stdout, end="")
    result.check_returncode()
print(OUT / "results.log")
