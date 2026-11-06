#!/usr/bin/env python3
"""Real APK install/list/uninstall and native manager acceptance in task scratch.

Only the supplied temporary manager bundle is reconfigured. Existing installed
apps and user data are never removed. GUI controls press actual native buttons;
APK delivery uses Launch Services. Own-view PNGs verify layout when the desktop
is locked, and are explicitly not WindowServer/compositor captures.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path

APKS = [Path.home() / "Downloads/pikapika-v1.8.23-android-arm64-flutter_3.13.9.apk", Path.home() / "Downloads/jasmine-arm64-v1.7.22.apk"]


def digest(path):
    with path.open("rb") as stream: return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return {}


def wait_for(predicate, timeout=45):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value: return value
        time.sleep(.2)
    raise TimeoutError("acceptance condition did not become true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    task = task_directory("apk-installer-test")
    app, out = temporary_path(args.app), temporary_path(args.out)
    assert app.is_dir()
    out.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env.update(HUSK_APPS_REGISTRY=str(out / "registry.json"), HUSK_APPS_DATA_BASE=str(out / "backend-data"), HUSK_APPS_ROOTS=json.dumps([str(ROOT / "build/macos-apps")]))
    resources = app / "Contents/Resources"
    backend = resources / "engine/tools/macos/install_apk.py"
    host = resources / "HuskAppHost"
    binary = app / "Contents/MacOS/HuskInstaller"
    config_path = resources / "installer.json"
    config = read(config_path)
    result = {"task_directory": str(task), "manager": str(app), "checks": [], "gui": {}, "success": False}
    hashes = [digest(p) for p in APKS]
    def record(name, **extra):
        result["checks"].append({"name": name, **extra})
        (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(name, flush=True)
    def run(*values, expected=0):
        command = [sys.executable, "-B", str(backend), *map(str, values)]
        completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        assert completed.returncode == expected, (command, completed.returncode, completed.stdout, completed.stderr)
        if expected: return completed.stderr
        return json.loads(completed.stdout)
    process = None
    guest_pid = None
    control = out / "gui-control.jsonl"
    control.touch()
    state_root = out / "gui-state"
    state_file = state_root / "state.json"
    def state(): return read(state_file)
    def send(action, **fields):
        with control.open("a") as stream: stream.write(json.dumps({"action": action, **fields}) + "\n")
    def idle(): return not state().get("busy", True)
    def start_manager():
        log = (out / ("gui-reopen.log" if state_file.exists() else "gui-launch.log")).open("wb")
        child = subprocess.Popen([str(binary), "--test-root", str(state_root), "--test-control", str(control), "--no-activate"], stdout=log, stderr=log)
        log.close()
        wait_for(lambda: state().get("pid") == child.pid and idle())
        return child
    try:
        # Use the bundled backend/host, not source-checkout installation code.
        identities = [run("inspect", apk) for apk in APKS]
        assert [row["package"] for row in identities] == ["opensource.pic2acg", "opensource.jmtt2mic"]
        record("inspect-two-original-downloads", apk_sha256=hashes)
        invalid = out / "malformed.apk"; invalid.write_bytes(b"not an APK")
        assert "Husk installer:" in run("inspect", invalid, expected=1)
        ordinary = out / "Not Managed.app"; ordinary.mkdir(); (ordinary / "keep").write_text("keep")
        run("uninstall", ordinary, expected=1); assert (ordinary / "keep").exists()
        record("invalid-apk-and-unmanaged-uninstall-rejected")
        directory = out / "backend-apps"
        installed = [run("install", apk, "--host", host, "--applications", directory, "--runtime-store", config["runtime_store"]) for apk in APKS]
        paths = [Path(row["application"]) for row in installed]
        for path in paths:
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(path)], check=True)
            # Signing the containing .app assigns the main executable its new
            # bundle identifier, so compare actual code rather than signatures.
            def text_section(executable):
                return subprocess.check_output(["otool", "-s", "__TEXT", "__text", str(executable)], text=True).splitlines()[1:]
            assert text_section(path / "Contents/MacOS/HuskAppHost") == text_section(host)
        all_rows = run("list", "--all")
        assert set(map(str, paths)).issubset({row["application"] for row in all_rows})
        assert all(str(ROOT / "build/macos-apps" / (name + ".app")) in {row["application"] for row in all_rows} for name in ["Pikapika", "Jasmine"])
        record("signed-installs-using-bundled-host-and-discovery", applications=list(map(str, paths)))
        run("install", APKS[0], "--host", host, "--applications", directory, "--runtime-store", config["runtime_store"], expected=1)
        link = out / "Linked.app"; link.symlink_to(paths[0], target_is_directory=True)
        run("uninstall", link, expected=1); assert paths[0].exists()
        data = out / "backend-data" / installed[0]["bundle_id"]
        data.mkdir(parents=True); marker = data / "retained-user-data"; marker.write_text("keep unchanged")
        # A second process really holds the host's lock; no fabricated status PID.
        lock = data / "instance.lock"
        holder = subprocess.Popen([sys.executable, "-B", "-c", "import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(30)", str(lock)], env=env, stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "ready"
            run("uninstall", paths[0], expected=1); assert paths[0].is_dir() and marker.read_text() == "keep unchanged"
        finally:
            holder.terminate(); holder.wait(timeout=5); holder.stdout.close()
        record("duplicate-symlink-and-live-instance-uninstall-rejected")
        run("uninstall", paths[0]); assert not paths[0].exists() and marker.read_text() == "keep unchanged"
        again = run("install", APKS[0], "--host", host, "--applications", directory, "--runtime-store", config["runtime_store"])
        assert again["bundle_id"] == installed[0]["bundle_id"] and marker.exists()
        run("uninstall", Path(again["application"]), "--remove-data")
        assert not data.exists() and paths[1].is_dir()
        run("uninstall", paths[1], "--remove-data")
        record("uninstall-preserves-data-reinstall-reuses-identity-explicit-data-removal")
        # Scope this native manager acceptance to private installations only.
        config["scan_roots"] = []
        config["applications"] = str(out / "gui-apps")
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(app)], check=True)
        process = start_manager(); assert not state()["apps"]
        subprocess.run(["open", "-a", str(app), *map(str, APKS)], check=True, env=env, timeout=15)
        wait_for(lambda: idle() and isinstance(state().get("pending"), dict))
        assert state()["pending"]["package"] == identities[0]["package"]
        send("render"); wait_for(lambda: (state_root / "window.png").exists())
        (out / "pending-apk-own-view.png").write_bytes((state_root / "window.png").read_bytes())
        send("install")
        wait_for(lambda: idle() and len(state().get("apps", [])) == 1 and isinstance(state().get("pending"), dict))
        assert state()["pending"]["package"] == identities[1]["package"]
        send("install"); wait_for(lambda: idle() and len(state().get("apps", [])) == 2 and state().get("pending") is None)
        record("launch-services-open-files-native-install-buttons-and-multiple-apk-queue")
        subprocess.run(["open", "-a", str(app), str(APKS[0])], check=True, env=env)
        wait_for(lambda: idle() and "已安装" in state().get("message", ""))
        assert len(state()["apps"]) == 2
        send("search", query="jmtt2mic"); wait_for(lambda: state().get("visible_count") == 1)
        send("search", query=""); wait_for(lambda: state().get("visible_count") == 2)
        send("render"); time.sleep(1)
        (out / "installed-apps-own-view.png").write_bytes((state_root / "window.png").read_bytes())
        record("duplicate-open-selects-existing-app-and-search-filters")
        pika = next(row for row in state()["apps"] if row["android_package"] == identities[0]["package"])
        send("select", application=pika["application"]); time.sleep(.6); send("launch")
        guest_pid = wait_for(lambda: state().get("last_launched_pid"))
        guest_status = Path(pika["data_directory"]) / "status.json"
        wait_for(lambda: read(guest_status).get("pid") == guest_pid and read(guest_status).get("state") == 2)
        send("uninstall"); wait_for(lambda: state().get("uninstall_prompt"))
        send("confirm-uninstall", remove_data=False)
        wait_for(lambda: "正在运行" in state().get("message", ""))
        assert Path(pika["application"]).exists()
        send("quit"); process.wait(timeout=10); process = None
        os.kill(guest_pid, 0)
        result["gui"]["independent_guest_pid"] = guest_pid
        result["gui"]["guest_status"] = read(guest_status)
        record("manager-launches-real-apk-refuses-live-uninstall-and-exits-independently")
        # Terminate only our recorded private test app, never by process name.
        assert str(Path(pika["application"]).resolve()).startswith(str(out.resolve()) + os.sep)
        os.kill(guest_pid, signal.SIGTERM)
        wait_for(lambda: subprocess.run(["kill", "-0", str(guest_pid)], capture_output=True).returncode != 0)
        guest_pid = None
        control.write_text("")
        process = start_manager()
        marker = Path(pika["data_directory"]) / "retained-user-data"; marker.write_text("gui data")
        send("select", application=pika["application"]); time.sleep(.6)
        send("uninstall"); wait_for(lambda: state().get("uninstall_prompt")); send("confirm-uninstall", remove_data=False)
        wait_for(lambda: idle() and len(state().get("apps", [])) == 1)
        assert marker.read_text() == "gui data" and not Path(pika["application"]).exists()
        other = state()["apps"][0]; other_data = Path(other["data_directory"]); other_data.mkdir(parents=True, exist_ok=True)
        (other_data / "delete-me").write_text("only this app")
        send("select", application=other["application"]); time.sleep(.6)
        send("uninstall"); wait_for(lambda: state().get("uninstall_prompt")); send("confirm-uninstall", remove_data=True)
        wait_for(lambda: idle() and not state().get("apps"))
        assert not other_data.exists() and marker.exists()
        record("native-uninstall-confirmations-preserve-or-remove-own-data")
        subprocess.run(["open", "-a", str(app), str(invalid)], check=True, env=env)
        wait_for(lambda: idle() and "Husk installer:" in state().get("message", ""))
        assert not state()["apps"]
        record("native-invalid-apk-error-without-install-side-effects")
        send("quit"); process.wait(timeout=10); process = None
        assert [digest(p) for p in APKS] == hashes
        result["success"] = True
        record("original-apks-unchanged-and-all-owned-processes-closed")
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if guest_pid:
            try: os.kill(guest_pid, signal.SIGTERM)
            except ProcessLookupError: pass
        if process and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__": main()
