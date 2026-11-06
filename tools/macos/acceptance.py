#!/usr/bin/env python3
"""Run two installed Mac APK apps through their real AppKit/public-ABI path.

Screenshots and command receipts are evidence, not automatic proof that every
business feature works. Optional scenarios contain real JSON input commands.
Only this invocation's children and its new output directory are controlled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import resource
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


class App:
    def __init__(self, bundle: Path, output: Path, runtime: Path | None = None, state: Path | None = None):
        task_directory(label="macos-acceptance")
        output = temporary_path(output)
        if state is not None:
            state = temporary_path(state)
        self.bundle = bundle.resolve(strict=True)
        self.output = output
        self.state = state if state is not None else output / "state"
        self.frames = self.state / "cache/frames"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.control_file = output / "control.jsonl"
        self.control_file.touch()
        with (bundle / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        command = [str(bundle / "Contents/MacOS" / info["CFBundleExecutable"]),
                   "--state-root", str(self.state), "--frame-dir", str(self.frames),
                   "--control", str(self.control_file), "--quit-after", "600", "--no-activate"]
        if runtime:
            command += ["--runtime", str(runtime.resolve(strict=True))]
        self.log = (output / "launcher.log").open("wb")
        self.process = subprocess.Popen(command, stdout=self.log, stderr=self.log)
        self.commands = []

    def status(self) -> dict:
        if self.process.poll() is not None:
            raise RuntimeError(f"{self.bundle.name} exited ({self.process.returncode}); see {self.output}")
        return read_json(self.state / "status.json")

    def wait_ready(self, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.status()
            if value.get("error"):
                raise RuntimeError(f"{self.bundle.name}: {value['error']}")
            if value.get("state") == 2 and value.get("frames", 0) > 0 and (self.frames / "latest.bmp").is_file():
                return value
            time.sleep(0.2)
        raise TimeoutError(f"{self.bundle.name} did not render within {timeout}s")

    def send(self, command: dict):
        with self.control_file.open("a") as stream:
            stream.write(json.dumps(command, ensure_ascii=False) + "\n")
        self.commands.append({"time": time.time(), **command})
        (self.output / "commands.json").write_text(json.dumps(self.commands, ensure_ascii=False, indent=2))

    def shot(self, name: str) -> dict:
        path = self.output / (name + ".png")
        path.unlink(missing_ok=True)
        self.send({"action": "shot", "path": str(path)})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.status()
            if path.exists():
                window = self.output / (name + ".window.png")
                window_id = self.status().get("window_number")
                capture = subprocess.run(["screencapture", "-x", "-l", str(window_id), str(window)],
                                         capture_output=True, text=True, timeout=10)
                if capture.returncode or not window.is_file():
                    raise RuntimeError(f"real window capture failed: {capture.stderr.strip()}")
                return {"egl_path": str(path), "egl_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "window_path": str(window), "window_sha256": hashlib.sha256(window.read_bytes()).hexdigest()}
            time.sleep(0.1)
        raise TimeoutError(f"capture missing: {path}")

    def scenario(self, path: Path | None):
        if not path:
            return
        commands = json.loads(path.read_text())
        if not isinstance(commands, list):
            raise ValueError("scenario must be a list of control commands")
        for command in commands:
            wait = float(command.get("wait", 0))
            if not 0 <= wait <= 60:
                raise ValueError("scenario wait must be between 0 and 60 seconds")
            if command.get("action"):
                self.send({k: v for k, v in command.items() if k != "wait"})
            until = time.monotonic() + wait
            while time.monotonic() < until:
                self.status()
                time.sleep(0.1)

    def stop(self):
        if self.process.poll() is None:
            self.send({"action": "quit"})
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apps", type=Path, nargs=2)
    parser.add_argument("--out", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--runtime", type=Path, help="otherwise use each installed app's launch-time default")
    parser.add_argument("--scenario-a", type=Path)
    parser.add_argument("--scenario-b", type=Path)
    args = parser.parse_args()
    task_directory(label="macos-acceptance")
    output = temporary_path(args.out)
    output.mkdir(parents=True, exist_ok=False)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    apps = []
    evidence = {"schema": 1, "kind": "macos-functional", "sandbox": "unverified",
                "system_memory_limit": "unsupported", "business_acceptance": "requires-screen-review"}
    try:
        for i, bundle in enumerate(args.apps):
            case = output / ("app-a" if i == 0 else "app-b")
            case.mkdir()
            apps.append(App(bundle.resolve(strict=True), case, args.runtime))
        a, b = apps
        evidence["ready"] = [a.wait_ready(), b.wait_ready()]
        assert a.process.pid != b.process.pid
        assert evidence["ready"][0]["app_id"] != evidence["ready"][1]["app_id"]
        a.scenario(args.scenario_a)
        b.scenario(args.scenario_b)
        evidence["screens"] = [a.shot("before-crash"), b.shot("before-crash")]
        evidence["before_crash"] = [a.status(), b.status()]
        # A host SIGSEGV must not share a Java heap or lifecycle with B.
        os.kill(a.process.pid, signal.SIGSEGV)
        a.process.wait(timeout=10)
        assert a.process.returncode != 0
        evidence["a_crash_returncode"] = a.process.returncode
        b.send({"action": "tap", "x": 360, "y": 350})
        time.sleep(0.5)
        b.send({"action": "swipe", "x": 360, "y": 950, "x2": 360, "y2": 450, "seconds": 0.5})
        time.sleep(1.5)
        evidence["b_after_a_crash"] = b.status()
        evidence["b_after_screen"] = b.shot("after-other-app-crash")
        assert evidence["b_after_a_crash"]["pid"] == evidence["ready"][1]["pid"]
        assert evidence["b_after_a_crash"]["state"] == 2
        evidence["process_crash_isolation"] = "passed"
    except BaseException as error:
        evidence["error"] = str(error)
        raise
    finally:
        for app in apps:
            app.stop()
        (output / "results.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(output / "results.json")


if __name__ == "__main__":
    main()
