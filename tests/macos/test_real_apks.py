#!/usr/bin/env python3
"""Verify two installed Downloads APKs through real independent AppKit processes.

This changes the Runtime store's default with the real installer and leaves v2
selected. It controls only its own child processes and newly created evidence
directory. Screenshots require final visual review; authentication is not tested.
No account is submitted, no APK/data is patched, and no --runtime launch override
is used. Each host has its existing 600-second automatic exit bound.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import resource
import signal
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path
SPEC = importlib.util.spec_from_file_location("husk_real_apk_acceptance", ROOT / "tools/macos/acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)
DOWNLOADS = {
    "pika": ("pikapika-v1.8.23-android-arm64-flutter_3.13.9.apk", "684b204ac3cdd6a3d5650debdd7dda31158b841b4d1dc13b3562e5ff4984f20a"),
    "jasmine": ("jasmine-arm64-v1.7.22.apk", "d5b6eb4bbfbca5f7ab62b9f09707d51045c71566a7a9e281df1a59554c0ae0fa"),
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {}


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def lease_available(runtime: Path) -> bool:
    path = runtime.parent / ".leases" / (runtime.name + ".lock")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        held, named = os.fstat(descriptor), path.lstat()
        check(stat.S_ISREG(held.st_mode) and stat.S_ISREG(named.st_mode)
              and held.st_nlink == named.st_nlink == 1
              and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino), "unsafe Runtime lease object")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


class Run:
    def __init__(self, output: Path, v1: Path, v2: Path):
        self.output, self.v1, self.v2 = output, v1, v2
        self.deadline = time.monotonic() + 600
        self.apps = []
        self.report = {"schema": 1, "scope": "real installed APK AppKit acceptance",
                       "task_directory": str(task_directory()),
                       "temporary_environment": {key: os.environ.get(key) for key in ["TMPDIR", "TMP", "TEMP"]},
                       "started_utc": datetime.now(timezone.utc).isoformat(),
                       "runtime_v1": str(v1), "runtime_v2": str(v2),
                       "launch_runtime_override": False, "visual_review": "required for every window capture",
                       "account_authentication": "not attempted; test text is cancelled without submission",
                       "system_hard_memory_limit": "unsupported", "sandbox": "unverified",
                       "steps": [], "launches": [], "cleanup": []}

    def save(self):
        (self.output / "results.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")

    def record(self, step, **value):
        item = {"step": step, "elapsed_seconds": round(600 - (self.deadline - time.monotonic()), 3), **value}
        self.report["steps"].append(item)
        self.save()
        print(json.dumps(item, ensure_ascii=False), flush=True)

    def remaining(self, limit=90):
        available = self.deadline - time.monotonic()
        if available <= 0:
            raise TimeoutError("acceptance exceeded its 600-second bound")
        return min(limit, available)

    def wait(self, seconds, *apps):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.remaining()
            for app in apps:
                app.status()
            time.sleep(min(.1, max(0, end - time.monotonic())))

    def select(self, runtime: Path):
        result = subprocess.run([sys.executable, str(ROOT / "tools/macos/install_apk.py"), "select", str(runtime)],
                                cwd=ROOT, capture_output=True, text=True, timeout=self.remaining(90))
        (self.output / ("select-" + runtime.name + ".log")).write_text(result.stdout + result.stderr)
        check(result.returncode == 0, "installer Runtime select failed: " + result.stderr[-1500:])
        selected = read_json(runtime.parent / "default.json")
        check(selected.get("build_id") == runtime.name, "installer selection readback disagrees")
        self.record("select-default", runtime=str(runtime), installer_exit_code=result.returncode, selection=selected)

    def launch(self, name, bundle: Path, state: Path, expected: Path):
        directory = self.output / name
        directory.mkdir(mode=0o700)
        # Deliberately omit App's runtime argument. Only default.json selects it.
        app = acceptance.App(bundle, directory, state=state)
        self.apps.append(app)
        ready = app.wait_ready(timeout=self.remaining(90))
        app.acceptance_generation = ready["generation"]
        app.acceptance_last_status = ready
        self.version(app, expected)
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        argv = [str(bundle / "Contents/MacOS" / info["CFBundleExecutable"]),
                "--state-root", str(state), "--frame-dir", str(app.frames),
                "--control", str(app.control_file), "--quit-after", "600", "--no-activate"]
        item = {"name": name, "pid": app.process.pid, "generation": ready["generation"],
                "state": str(state), "output": str(directory), "argv": argv, "ready": ready}
        self.report["launches"].append(item)
        self.record("launch", **item)
        return app

    def version(self, app, expected):
        value = app.status()
        check(Path(value.get("runtime", "")).resolve() == expected, f"{app.bundle.name} loaded the wrong Runtime")
        check(value.get("pid") == app.process.pid and value.get("state") == 2, "host status is not its running process")
        app.acceptance_last_status = value
        return value

    def send(self, app, action, wait=0, **values):
        self.remaining()
        app.send({"action": action, **values})
        if wait:
            self.wait(wait, app)

    def shot(self, app, name):
        evidence = app.shot(name)
        self.record("capture", label=name, pid=app.process.pid, status=app.status(),
                    egl=evidence["egl_path"], egl_sha256=evidence["egl_sha256"],
                    window=evidence["window_path"], window_sha256=evidence["window_sha256"])
        return evidence

    def exit(self, app, name):
        before = app.status()
        app.send({"action": "quit"})
        app.process.wait(timeout=self.remaining(25))
        app.acceptance_last_status = read_json(app.state / "status.json")
        app.log.close()
        self.record(name, before=before, exit_code=app.process.returncode,
                    final_status=read_json(app.state / "status.json"))

    def cleanup(self):
        for app in self.apps:
            try:
                if app.process.poll() is None:
                    app.send({"action": "quit"})
                    try:
                        app.process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        app.process.terminate()
                        try:
                            app.process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            app.process.kill()
                            app.process.wait(timeout=5)
                app.log.close()
                current = read_json(app.state / "status.json")
                generation = getattr(app, "acceptance_generation", "")
                self.report["cleanup"].append({"pid": app.process.pid, "exit_code": app.process.returncode,
                                                "generation": generation,
                                                "status": current if current.get("generation") == generation else getattr(app, "acceptance_last_status", {}),
                                                "current_state_generation": current.get("generation")})
            except Exception as error:
                self.report["cleanup"].append({"pid": app.process.pid, "error": str(error)})
        self.report["all_owned_children_exited"] = all(app.process.poll() is not None for app in self.apps)
        self.report["final_leases"] = {}
        for name, runtime in [("v1", self.v1), ("v2", self.v2)]:
            try:
                self.report["final_leases"][name + "_exclusive_available"] = lease_available(runtime)
            except Exception as error:
                self.report["final_leases"][name + "_error"] = str(error)
        self.report["final_default"] = read_json(self.v2.parent / "default.json")
        self.report["artifacts"] = []
        for path in sorted(self.output.rglob("*")):
            if path.name == "results.json":
                continue
            kind = path.lstat().st_mode
            if stat.S_ISREG(kind):
                self.report["artifacts"].append({"path": str(path.relative_to(self.output)),
                                                  "bytes": path.stat().st_size, "sha256": digest(path)})
        self.save()


def installed(bundle: Path, role: str, store: Path):
    config = read_json(bundle / "Contents/Resources/husk-app.json")
    check(Path(config.get("runtime_store", "")).resolve() == store, "installed apps must use the two versions' Runtime store")
    check((config.get("width"), config.get("height"), config.get("density")) == (720, 1280, 2),
          "scenario requires the verified 720x1280 density-2 guest layout")
    relative = Path(config["apk_file"])
    check(not relative.is_absolute() and ".." not in relative.parts, "unsafe installed APK path")
    apk = (bundle / "Contents/Resources/package" / relative).resolve(strict=True)
    check(bundle in apk.parents, "installed APK escapes its bundle")
    filename, expected = DOWNLOADS[role]
    source = Path.home() / "Downloads" / filename
    check(source.is_file() and digest(source) == expected and digest(apk) == expected
          and config.get("apk_sha256") == expected, "installed app does not match the specified unchanged Downloads APK")
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    executable = bundle / "Contents/MacOS" / info["CFBundleExecutable"]
    return {"bundle": str(bundle), "installed_apk": str(apk), "source_apk": str(source), "apk_sha256": expected,
            "bundle_id": config.get("bundle_id"), "host_executable_sha256": digest(executable), "width": 720, "height": 1280}


def scenario(run: Run, pika_bundle: Path, jasmine_bundle: Path):
    check(lease_available(run.v1) and lease_available(run.v2), "versions are already leased; use dedicated final test versions")
    run.report["initial_default"] = read_json(run.v1.parent / "default.json")
    run.report["manual_settings"] = read_json(run.v1.parent / "settings.json")
    run.report["runtime_manifests"] = []
    for runtime in [run.v1, run.v2]:
        manifest = read_json(runtime / "manifest.json")
        run.report["runtime_manifests"].append({"runtime": str(runtime), "sha256": digest(runtime / "manifest.json"),
                                                 "build_id": manifest.get("build_id"), "profile": manifest.get("profile"),
                                                 "source_tree_sha256": manifest.get("source_tree_sha256")})
    run.report["installed_inputs"] = [installed(pika_bundle, "pika", run.v1.parent),
                                       installed(jasmine_bundle, "jasmine", run.v1.parent)]
    check(run.report["installed_inputs"][0]["bundle_id"] != run.report["installed_inputs"][1]["bundle_id"],
          "the APKs must have independent installed app identities")
    pika_state, jasmine_state = run.output / "pika-state", run.output / "jasmine-state"
    run.select(run.v1)
    pika_first = run.launch("pika-first", pika_bundle, pika_state, run.v1)
    run.wait(12, pika_first)
    run.shot(pika_first, "calculator-first-launch")
    # Normal CalculatorScreen activation; these are the actual app's buttons.
    keys = {"AC": (108, 296), "7": (108, 562), "5": (284, 778), "4": (108, 778),
            "2": (284, 990), "=": (630, 1200)}
    for key in ["AC", *"77554422", "="]:
        x, y = keys[key]
        run.send(pika_first, "tap", wait=.8, x=x, y=y)
    run.wait(2, pika_first)
    run.shot(pika_first, "activation-tips-before-own-close")
    # Verified in the actual 720x1280/density-2 window: the centered body
    # button sits below the 56dp AppBar, at surface y=696.
    pika_first.send({"action": "tap", "x": 360, "y": 696})
    pika_first.process.wait(timeout=run.remaining(20))
    check(pika_first.process.returncode == 0, "Pikapika Tips Close did not produce the verified app-owned exit 0")
    pika_first.log.close()
    pika_first.acceptance_last_status = read_json(pika_state / "status.json")
    run.record("pika-app-owned-tips-close", pid=pika_first.process.pid, exit_code=pika_first.process.returncode,
               final_status=read_json(pika_state / "status.json"))
    check(lease_available(run.v1), "v1 lease did not release after app-owned exit")
    pika = run.launch("pika-reopened-v1", pika_bundle, pika_state, run.v1)
    run.wait(10, pika)
    run.shot(pika, "account-after-normal-activation-and-reopen")
    jasmine = run.launch("jasmine-first-v1", jasmine_bundle, jasmine_state, run.v1)
    run.wait(10, pika, jasmine)
    run.shot(jasmine, "browser-first-launch")
    run.send(jasmine, "native_click", wait=1.2, x=200, y=50)
    run.send(jasmine, "select_all", wait=.8)
    run.send(jasmine, "native_text", wait=.8, text="jm://start")
    run.shot(jasmine, "activation-url-selected-and-typed")
    run.send(jasmine, "native_submit", wait=8)
    run.shot(jasmine, "login-after-normal-browser-activation")
    before_upgrade = {"pika": run.version(pika, run.v1), "jasmine": run.version(jasmine, run.v1)}
    check(pika.process.pid != jasmine.process.pid, "the APKs did not receive independent processes")
    run.select(run.v2)
    run.record("running-apps-pin-v1-after-default-v2", before=before_upgrade,
               after={"pika": run.version(pika, run.v1), "jasmine": run.version(jasmine, run.v1)},
               v1_exclusive_available=lease_available(run.v1), v2_exclusive_available=lease_available(run.v2))
    check(not lease_available(run.v1), "active v1 apps lost their version lease")
    check(lease_available(run.v2), "selecting v2 unexpectedly caused an active app to hot-load it")
    run.exit(jasmine, "jasmine-complete-exit-before-version-upgrade")
    check(not lease_available(run.v1), "Pikapika did not continue holding v1")
    jasmine = run.launch("jasmine-reopened-v2", jasmine_bundle, jasmine_state, run.v2)
    check(jasmine.process.pid != before_upgrade["jasmine"]["pid"], "Jasmine PID did not change")
    check(jasmine.status()["generation"] != before_upgrade["jasmine"]["generation"], "Jasmine generation did not change")
    run.wait(10, pika, jasmine)
    run.shot(jasmine, "login-preserved-after-reopen-on-v2")
    run.version(pika, run.v1)
    run.version(jasmine, run.v2)
    check(not lease_available(run.v1) and not lease_available(run.v2), "both active versions must stay leased")
    survivor_before = jasmine.status()
    crash_before = pika.status()
    os.kill(pika.process.pid, signal.SIGSEGV)
    pika.process.wait(timeout=run.remaining(15))
    check(pika.process.returncode != 0, "injected Pikapika SIGSEGV did not crash")
    check(lease_available(run.v1), "crashed v1 process did not release its lease")
    check(not lease_available(run.v2), "surviving Jasmine lost its v2 lease")
    run.record("injected-crash-isolates-process-and-version-leases", injected_signal="SIGSEGV",
               pika_before=crash_before, pika_exit_code=pika.process.returncode,
               jasmine_before=survivor_before, jasmine_after=jasmine.status(),
               v1_exclusive_available=True, v2_exclusive_available=False)
    run.send(jasmine, "native_click", wait=1.2, x=150, y=164)
    run.shot(jasmine, "account-dialog-after-other-process-crash")
    run.send(jasmine, "native_click", wait=1.2, x=330, y=556)
    run.send(jasmine, "native_text", wait=2, text="husk-local-ui-test")
    typed = run.shot(jasmine, "unsubmitted-account-text-after-other-process-crash")
    run.send(jasmine, "tap", wait=2, x=500, y=708)  # dialog's Cancel; never Save/Submit
    cancelled = run.shot(jasmine, "cancelled-account-text-after-other-process-crash")
    survivor_after = run.version(jasmine, run.v2)
    check(survivor_after["pid"] == survivor_before["pid"]
          and survivor_after["generation"] == survivor_before["generation"], "survivor restarted during crash test")
    check(survivor_after["frames"] > survivor_before["frames"], "survivor did not render the actual input interaction")
    check(typed["window_sha256"] != cancelled["window_sha256"], "typed/cancelled real window captures did not change")
    run.record("survivor-rendered-real-unsubmitted-text-interaction", before=survivor_before, after=survivor_after,
               submitted_to_server=False, screenshot_review="required: verify typed text and cancelled dialog")
    run.exit(jasmine, "jasmine-final-complete-exit")
    check(lease_available(run.v1) and lease_available(run.v2), "finished app processes left a Runtime leased")
    run.report["mechanical_checks_passed"] = True
    run.report["final_visual_acceptance"] = "pending independent review of all real window captures"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pika", required=True, type=Path)
    parser.add_argument("--jasmine", required=True, type=Path)
    parser.add_argument("--runtime-v1", required=True, type=Path)
    parser.add_argument("--runtime-v2", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    task_directory(label="macos-real-apks")
    pika, jasmine, v1, v2 = [path.resolve(strict=True) for path in [args.pika, args.jasmine, args.runtime_v1, args.runtime_v2]]
    check(v1 != v2 and v1.parent == v2.parent, "two distinct versions in one Runtime store are required")
    output = temporary_path(args.out)
    check(all(output != path and path not in output.parents and output not in path.parents
              for path in [pika, jasmine, v1, v2]), "output cannot overlap an installed app or Runtime version")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    run = Run(output, v1, v2)
    def interrupted(signum, frame):
        raise TimeoutError("600-second acceptance deadline" if signum == signal.SIGALRM else "acceptance interrupted")
    signal.signal(signal.SIGALRM, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    signal.alarm(600)
    try:
        scenario(run, pika, jasmine)
    except BaseException as error:
        run.report["error"] = f"{type(error).__name__}: {error}"
        print(run.report["error"], file=sys.stderr, flush=True)
    finally:
        signal.alarm(0)
        run.cleanup()
    print(output / "results.json", flush=True)
    released = run.report.get("final_leases", {})
    return 1 if (run.report.get("error") or not run.report.get("all_owned_children_exited")
                 or not released.get("v1_exclusive_available") or not released.get("v2_exclusive_available")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
