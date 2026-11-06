#!/usr/bin/env python3
"""Verify native metrics sampling and performance window lifecycle; render UI evidence."""
import sys
sys.dont_write_bytecode = True

from pathlib import Path
import resource
import subprocess
import os
import plistlib
import shutil

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
                    "-framework", "AppKit", "-framework", "QuartzCore", "-framework", "Metal", "-framework", "WebKit",
                    "-o", str(binary)], check=True)
    # LaunchServices gives this App its own responsibility identity, as with a
    # Finder launch. A bare subprocess inherits the terminal/IDE's identity and
    # cannot safely attribute its WebKit helpers independently of sibling apps.
    app = task / "tmp/Performance Test.app"
    executable = app / "Contents/MacOS/test-performance"
    executable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, executable)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "org.husk.performance-test", "CFBundleExecutable": executable.name,
        "CFBundleName": "Husk Performance Test", "CFBundlePackageType": "APPL", "NSHighResolutionCapable": True,
    }))
    subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(app)], check=True)
    marker = output / "passed.txt"
    marker.unlink(missing_ok=True)
    for name in ["test.log", "test-errors.log"]:
        (output / name).write_text("")
    foreign = subprocess.Popen([str(binary), "--worker", str(os.getpid()), "0"], stdout=subprocess.PIPE)
    try:
        assert foreign.stdout.readline().strip() == b"ready"
        command = ["open", "-n", "-W", "--stdout", str(output / "test.log"), "--stderr", str(output / "test-errors.log")]
        for key in ["HUSK_TASK_TMPDIR", "TMPDIR", "TMP", "TEMP", "CFFIXED_USER_HOME", "XDG_CACHE_HOME"]:
            command += ["--env", key + "=" + os.environ[key]]
        command += [str(app), "--args", str(output), str(foreign.pid)]
        subprocess.run(command, check=True, timeout=40)
        print((output / "test.log").read_text())
        errors = (output / "test-errors.log").read_text()
        if errors:
            print(errors, file=sys.stderr)
        assert marker.is_file() and marker.read_text() == "passed\n", "native App did not complete successfully"
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)
    print(f"AppKit view renders (not compositor captures): {output}")


if __name__ == "__main__":
    main()
