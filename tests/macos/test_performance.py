#!/usr/bin/env python3
"""Verify native metrics sampling and performance window lifecycle; render UI evidence."""
import sys
sys.dont_write_bytecode = True

from pathlib import Path
import resource
import subprocess

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory


def main():
    task = task_directory("macos-performance")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    output = task / "evidence/performance"
    output.mkdir(parents=True, exist_ok=True)
    binary = task / "tmp/test-performance"
    subprocess.run(["xcrun", "--sdk", "macosx", "clang", "-arch", "arm64", "-fobjc-arc",
                    "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations",
                    str(ROOT / "tests/macos/test_performance.m"),
                    "-framework", "AppKit", "-framework", "QuartzCore", "-framework", "Metal",
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary), str(output)], check=True, timeout=30)
    print(f"AppKit view renders (not compositor captures): {output}")


if __name__ == "__main__":
    main()
