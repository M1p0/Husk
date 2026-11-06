#!/usr/bin/env python3
"""Verify AppKit window resizing, guest coordinates and native overlay routing."""
import sys
sys.dont_write_bytecode = True

from pathlib import Path
import argparse
import importlib.util
import json
import resource
import struct
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path


def fixture_regression(bundle, runtime, output):
    spec = importlib.util.spec_from_file_location("resize_fixture", ROOT / "tests/macos/test_fixture.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    output = temporary_path(output)
    output.mkdir(parents=True, exist_ok=False)
    app = fixture.FixtureApp(bundle.resolve(strict=True), output / "window", runtime.resolve(strict=True), output / "state")
    report = {"screens": [], "resizes": []}
    try:
        app.ready = app.wait_ready()
        app.wait_log("HUSK_FIXTURE web_ready=")
        inspector = fixture.build_inspector(output)
        time.sleep(.5)
        initial = fixture.capture(app, inspector, "01-initial")
        report["screens"].append(initial)
        for index, (width, height) in enumerate([(760, 640), (450, 800), (250, 700), (360, 640)]):
            app.send({"action": "resize", "width": width, "height": height})
            log = app.wait_log(f"HUSK_FIXTURE resized={width * 2}x{height * 2} dp={width}x{height}")
            expected = (width * 2, height * 2)
            deadline = time.monotonic() + 10
            actual = None
            while time.monotonic() < deadline:
                app.status()
                try:
                    with (app.frames / "latest.bmp").open("rb") as frame:
                        header = frame.read(26)
                    actual = struct.unpack_from("<ii", header, 18)
                except (OSError, struct.error):
                    pass
                if actual and (actual[0], abs(actual[1])) == expected:
                    break
                time.sleep(.1)
            assert actual and (actual[0], abs(actual[1])) == expected, (expected, actual)
            app.click(width, 304)
            app.wait_log(f"HUSK_FIXTURE java_click={index + 1}")
            shot = fixture.capture(app, inspector, f"resize-{width}x{height}")
            report["screens"].append(shot)
            if index == 0:
                # The green WebView must use the wider window, rather than a
                # centered copy of the original 360-point-wide phone surface.
                assert shot["window_pixels"]["green_pixels"] > initial["window_pixels"]["green_pixels"] * 1.8
                app.click(width, 700)
                time.sleep(.5)
                web = fixture.capture(app, inspector, "wide-native-web-click")
                assert web["window_pixels"]["cyan_pixels"] > 100000, web
                report["screens"].append(web)
            report["resizes"].append({"window_points": [width, height], "frame_pixels": list(expected), "java_configuration": log})
        report["result"] = "passed"
        print("PASS: real EGL frame dimensions, Android configuration/layout, full-width WebKit and native clicks")
    finally:
        app.stop()
        (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="also exercise the installed source-owned Java/WebKit fixture")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if bool(args.app) != bool(args.runtime):
        parser.error("--app and --runtime must be supplied together")
    task = task_directory("macos-window-resize")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    binary = task / "tmp/test-window-resize"
    subprocess.run(["xcrun", "--sdk", "macosx", "clang", "-arch", "arm64", "-fobjc-arc",
                    "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations",
                    str(ROOT / "tests/macos/test_window_resize.m"),
                    "-framework", "AppKit", "-framework", "QuartzCore", "-framework", "Metal",
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
    if args.app:
        fixture_regression(args.app, args.runtime, args.out or task / "evidence/responsive-window")


if __name__ == "__main__":
    main()
