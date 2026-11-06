#!/usr/bin/env python3
"""Exercise the real source-owned Java/WebKit APK through its installed Mac app.

Each run owns its children, control files and evidence directory. The second
generation intentionally reuses the first generation's data directory. Window
captures include WKWebView; separately named EGL captures do not.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path
spec = importlib.util.spec_from_file_location("husk_macos_acceptance", ROOT / "tools/macos/acceptance.py")
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class FixtureApp(acceptance.App):
    def __init__(self, bundle, output, runtime, state):
        task_directory(label="macos-fixture-acceptance")
        output, state = temporary_path(output), temporary_path(state)
        # Separate control/log/capture paths for each launch; preserve only state.
        self.bundle, self.output, self.state = bundle, output, state
        output.mkdir()
        self.frames = state / "cache/frames"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.control_file = output / "control.jsonl"
        self.control_file.touch(exist_ok=False)
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        command = [str(bundle / "Contents/MacOS" / info["CFBundleExecutable"]),
                   "--state-root", str(state), "--frame-dir", str(self.frames),
                   "--control", str(self.control_file), "--quit-after", "600",
                   "--runtime", str(runtime), "--no-activate"]
        self.log = (output / "launcher.log").open("wb")
        self.process = subprocess.Popen(command, stdout=self.log, stderr=self.log)
        self.commands = []
        self.ready = None

    def logs(self):
        paths = [self.output / "launcher.log"]
        if self.ready:
            paths.append(self.state / "logs" / (self.ready["generation"] + ".log"))
        return "\n".join(p.read_text(errors="replace") for p in paths if p.exists())

    def wait_log(self, expression, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = re.search(expression, self.logs())
            if match:
                return match.group(0)
            self.status()
            time.sleep(.1)
        raise TimeoutError(f"missing log {expression!r}; see {self.output}")

    def click(self, x, y):
        self.send({"action": "native_click", "x": x, "y": y})
        time.sleep(.8)  # Wait for host timer, activation and mouse up before text.


def lock_available(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def build_inspector(output):
    task = task_directory(label="macos-fixture-acceptance")
    work = Path(tempfile.mkdtemp(prefix="window-inspector-", dir=task / "tmp"))
    source = work / "inspect.swift"
    source.write_text(r'''import AppKit
import ImageIO
import Foundation
let args = CommandLine.arguments
if args.count != 3 { exit(2) }
if args[1] == "pixels" {
    let url = URL(fileURLWithPath: args[2])
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { exit(3) }
    let width = image.width, height = image.height
    var bytes = [UInt8](repeating: 0, count: width * height * 4)
    let rendered = bytes.withUnsafeMutableBytes { memory -> Bool in
        guard let context = CGContext(data: memory.baseAddress, width: width, height: height,
            bitsPerComponent: 8, bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue) else { return false }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return true
    }
    if !rendered { exit(5) }
    var green = 0, cyan = 0
    for offset in stride(from: 0, to: bytes.count, by: 4) {
        let r = bytes[offset], g = bytes[offset + 1], b = bytes[offset + 2]
        if r < 100 && g > 120 && b < 150 { green += 1 }
        if r < 100 && g > 140 && b > 150 { cyan += 1 }
    }
    let result = ["width": width, "height": height, "green_pixels": green, "cyan_pixels": cyan]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
} else {
    guard let pid = Int32(args[2]), let app = NSRunningApplication(processIdentifier: pid) else { exit(4) }
    let accepted: Bool
    if args[1] == "hide" { accepted = app.hide() }
    else if args[1] == "show" { accepted = app.unhide(); app.activate(options: []) }
    else { exit(2) }
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.3))
    let result: [String: Any] = ["accepted": accepted, "hidden": app.isHidden, "active": app.isActive, "pid": pid]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}
''')
    binary = work / "inspect"
    subprocess.run(["xcrun", "swiftc", "-O", str(source), "-framework", "AppKit", "-framework", "ImageIO",
                    "-module-cache-path", str(task / "tmp/swift-module-cache"),
                    "-o", str(binary)], check=True, capture_output=True, text=True)
    return binary


def inspect(binary, action, value):
    result = subprocess.run([str(binary), action, str(value)], check=True, capture_output=True, text=True, timeout=20)
    return result.stdout.strip()


def capture(app, inspector, name):
    shot = app.shot(name)
    shot["window_pixels"] = json.loads(inspect(inspector, "pixels", shot["window_path"]))
    return shot


def finish(app):
    app.click(360, 496)
    app.wait_log(r"HUSK_FIXTURE onDestroy_complete")
    result = app.process.wait(timeout=20)
    final = acceptance.read_json(app.state / "status.json")
    assert result == 0, result
    assert final.get("state") == 5, final
    assert "stop pending" not in app.logs(), "guest did not drain successfully"
    assert lock_available(app.state / "instance.lock"), "instance lock remains held"
    return {"exit_code": result, "final_status": final, "onDestroy": True,
            "guest_drain": "success", "instance_lock_released": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "build/macos-apps/Husk Java Fixture.app")
    parser.add_argument("--runtime", type=Path, default=ROOT / "build/macos-runtime/macos-app-candidate-20261106-c")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    task = task_directory(label="macos-fixture-acceptance")
    output = temporary_path(args.out or task / "evidence" /
                            ("fixture-acceptance-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")))
    output.mkdir(parents=True, exist_ok=False)
    bundle, runtime = args.app.resolve(strict=True), args.runtime.resolve(strict=True)
    state = output / "shared-state"
    apps = []
    result = {"schema": 1, "kind": "macos-functional", "app": str(bundle), "runtime": str(runtime),
              "task_directory": str(task),
              "temporary_environment": {key: os.environ.get(key) for key in ["TMPDIR", "TMP", "TEMP"]},
              "window_capture": "real macOS window including WKWebView", "egl_capture": "raw EGL, excludes WKWebView",
              "system_memory_limit": "unsupported", "screens": []}
    try:
        inspector = build_inspector(output)
        app = FixtureApp(bundle, output / "generation-1", runtime, state)
        apps.append(app)
        app.ready = app.wait_ready()
        app.wait_log(r"HUSK_FIXTURE web_ready=")
        time.sleep(1)
        result["first_ready"] = app.ready
        assert not lock_available(state / "instance.lock"), "instance lock missing while live"
        first_screen = capture(app, inspector, "01-ready")
        result["screens"].append(first_screen)
        assert first_screen["window_pixels"]["green_pixels"] > 20000

        app.click(360, 304)
        app.wait_log(r"HUSK_FIXTURE java_click=1")
        result["java_button"] = "passed"
        result["screens"].append(capture(app, inspector, "02-java-click"))

        app.click(360, 400)
        app.send({"action": "native_text", "text": "Initial Latin"})
        app.wait_log(r"HUSK_FIXTURE java_text=Initial Latin")
        result["screens"].append(capture(app, inspector, "03-java-latin"))
        app.send({"action": "select_all"})
        time.sleep(.5)
        app.send({"action": "native_text", "text": "替换成功 中文输入"})
        app.wait_log(r"HUSK_FIXTURE java_text=替换成功 中文输入(?:\n|$)")
        time.sleep(.5)
        result["screens"].append(capture(app, inspector, "04-java-chinese-replaced"))
        app.send({"action": "native_submit"})
        time.sleep(.7)
        result["java_chinese_select_all_replace"] = "passed"

        app.click(360, 746)
        time.sleep(1)
        web = capture(app, inspector, "05-native-web-click")
        result["screens"].append(web)
        assert web["window_pixels"]["cyan_pixels"] > first_screen["window_pixels"]["cyan_pixels"] + 20000, web
        result["native_web_button"] = "passed"
        result["web_click_text_review"] = "inspect 05-native-web-click.window.png for WEB clicks=1"

        ticks = [int(n) for n in re.findall(r"HUSK_FIXTURE tick=(\d+)", app.logs())]
        before = max(ticks)
        hidden = json.loads(inspect(inspector, "hide", app.process.pid))
        # hide() may return false when the target is already hidden; the
        # observed hidden/inactive state is what establishes this condition.
        assert hidden["hidden"] and not hidden["active"], hidden
        time.sleep(3.5)
        app.status()
        after = max(int(n) for n in re.findall(r"HUSK_FIXTURE tick=(\d+)", app.logs()))
        assert after >= before + 3, (before, after)
        result["background"] = {"host_state": hidden, "before_ticks": before, "after_ticks": after, "passed": True}
        inspect(inspector, "show", app.process.pid)
        time.sleep(.8)
        result["screens"].append(capture(app, inspector, "06-background-return"))
        result["first_stop"] = finish(app)

        app2 = FixtureApp(bundle, output / "generation-2", runtime, state)
        apps.append(app2)
        app2.ready = app2.wait_ready()
        assert app2.ready["generation"] != app.ready["generation"]
        assert app2.ready["pid"] != app.ready["pid"]
        app2.wait_log(r"HUSK_FIXTURE web_ready=")
        time.sleep(1)
        result["second_ready"] = app2.ready
        result["screens"].append(capture(app2, inspector, "07-reopened"))
        app2.click(360, 304)
        app2.wait_log(r"HUSK_FIXTURE java_click=1")
        app2.click(360, 400)
        app2.send({"action": "native_text", "text": "重开成功"})
        app2.wait_log(r"HUSK_FIXTURE java_text=重开成功")
        result["screens"].append(capture(app2, inspector, "08-reopened-interaction"))
        result["second_stop"] = finish(app2)
        result["reopen_same_state"] = "passed"
        lease = runtime.parent / ".leases" / (runtime.name + ".lock")
        result["runtime_lease_exclusive_available_after_exit"] = lock_available(lease)
        result["passed"] = True
    except BaseException as error:
        result["error"] = str(error)
        raise
    finally:
        for app in apps:
            app.stop()
        (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(output / "results.json", flush=True)


if __name__ == "__main__":
    main()
