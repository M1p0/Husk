#!/usr/bin/env python3
"""Exercise real Runtime images in isolated subprocesses, not a static harness."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory


def checked(args, **kwargs):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise AssertionError(f"{args}\n{result.stdout}\n{result.stderr}")
    return result


def exclusive_available(path):
    with path.open("rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False


@contextmanager
def writable(path):
    mode = path.stat().st_mode & 0o777
    path.chmod(mode | 0o200)
    try:
        yield
    finally:
        path.chmod(mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", nargs=2, type=Path,
                        help="copy two existing bundles into an isolated test store instead of rebuilding")
    options = parser.parse_args()
    processes = []
    with tempfile.TemporaryDirectory(prefix="husk-runtime-m1-", dir=task_directory("runtime-tests") / "tmp") as temporary:
        work = Path(temporary).resolve()
        store = work / "store"
        store.mkdir()
        try:
            versions = []
            if options.versions:
                (store / ".leases").mkdir()
                for source in options.versions:
                    source = source.resolve(strict=True)
                    destination = store / source.name
                    shutil.copytree(source, destination)
                    (store / ".leases" / (source.name + ".lock")).touch()
                    versions.append(destination)
            else:
                for name in ["m1-test-a", "m1-test-b"]:
                    result = checked(["sh", ROOT / "scripts/build_macos_runtime.sh",
                                      "--store", store, "--build-id", name], timeout=180)
                    versions.append(store / name)
                    print(result.stdout.strip().splitlines()[-1])
            a, b = versions
            probe = work / "runtime-probe"
            checked(["clang", "-fobjc-arc", "-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations",
                     ROOT / "tools/runtime-probe.m", "-framework", "Foundation", "-o", probe])
            api_test = work / "context-api-test"
            checked(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                     "-I" + str(ROOT / "src/runtime-api"),
                     ROOT / "tests/runtime/context_api_test.c", "-o", api_test])
            api_arguments = [api_test, a / "lib/libHuskRuntime.dylib"]
            if json.loads((a / "manifest.json").read_text())["profile"] == "macos-app":
                api_arguments.append("--app-profile")
            result = checked(api_arguments, timeout=15)
            print(result.stdout.strip())
            app_test = work / "app-api-test"
            checked(["clang", "-fobjc-arc", "-Wall", "-Wextra", "-Werror",
                     "-I" + str(ROOT / "src/runtime-api"), ROOT / "tests/runtime/app_api_test.m",
                     "-framework", "Foundation", "-o", app_test])
            result = checked([app_test, a / "lib/libHuskRuntime.dylib"], timeout=15)
            print(result.stdout.strip())
            dependencies = checked(["otool", "-L", probe]).stdout
            assert "libHuskRuntime" not in dependencies, dependencies
            undefined = checked(["nm", "-u", probe]).stdout
            assert not any(symbol in undefined for symbol in ["_tl_dvm_", "_tl_jni_", "_tl_bionic_"]), undefined
            print("PASS: launcher links only host libraries and obtains Runtime through the public getter")

            def arguments(version, name, **overrides):
                base = work / name
                values = {"runtime": str(version), "app-id": "dev.husk." + name, "generation": name, "jit-mib": "256"}
                for role in ["package", "data", "cache", "config"]:
                    (base / role).mkdir(parents=True, exist_ok=True)
                    values[role] = str(base / role)
                values.update(overrides)
                return [str(probe)] + [item for key, value in values.items() for item in ["--" + key, str(value)]]

            def run(version=a, name="single", expected=0, **overrides):
                result = subprocess.run(arguments(version, name, **overrides), capture_output=True, text=True, timeout=8)
                assert result.returncode == expected, (result.returncode, result.stdout, result.stderr)
                payload = json.loads(result.stdout)
                assert payload["ok"] == (expected == 0), payload
                return payload

            a.chmod(0o755)
            try:
                incomplete = run(name="incomplete-publication", expected=2)
                assert "read-only publication" in incomplete["error"], incomplete
            finally:
                a.chmod(0o555)
            print("PASS: interrupted publication with a writable root cannot load")

            def holding(version, name, **overrides):
                stderr = (work / (name + ".stderr")).open("w")
                process = subprocess.Popen(arguments(version, name, **{"hold-seconds": "45", **overrides}),
                                           stdout=subprocess.PIPE, stderr=stderr, text=True)
                processes.append(process)
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    assert selector.select(timeout=12), f"{name} did not become ready"
                    line = process.stdout.readline()
                stderr.close()
                assert line, (name, process.poll())
                payload = json.loads(line)
                assert payload["ok"], payload
                return process, payload

            def stop(process, kill=False):
                if process.poll() is None:
                    (process.kill if kill else process.terminate)()
                process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()

            first, first_info = holding(a, "app-a")
            second, second_info = holding(b, "app-b")
            assert first.pid != second.pid
            assert first_info["build_id"] == a.name and second_info["build_id"] == b.name
            assert first_info["data_root"] != second_info["data_root"]
            for info in [first_info, second_info]:
                assert info["context_copied"] and info["java_worker_stack_bytes"] == 32 << 20
                assert info["manual_jit_bytes"] == 256 << 20
                assert info["hard_memory_limit"] == "unsupported"
                assert info["apk_execution"] in {"not-implemented", "available-not-exercised"}
            lock_a = store / ".leases" / (a.name + ".lock")
            lock_b = store / ".leases" / (b.name + ".lock")
            assert not exclusive_available(lock_a) and not exclusive_available(lock_b)
            stop(first, kill=True)
            assert exclusive_available(lock_a)
            assert second.poll() is None and not exclusive_available(lock_b)
            stop(second)
            assert exclusive_available(lock_b)
            print("PASS: two new processes select different versions; SIGKILL of A preserves B and its version hold")

            first, _ = holding(a, "same-a")
            second, _ = holding(a, "same-b")
            stop(first, kill=True)
            assert not exclusive_available(lock_a), "remaining reader must still protect the version"
            stop(second)
            assert exclusive_available(lock_a)
            assert run(name="normal-exit")["ok"] and exclusive_available(lock_a)
            with lock_a.open("rb") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                run(name="locked", expected=2, **{"lease-timeout-ms": "30"})
            print("PASS: all readers must exit before exclusive use; normal exit releases; lock waits are bounded")

            run(name="wrong-abi", expected=3, **{"requested-abi": "2"})
            run(name="short-table", expected=3, **{"table-size": "0"})
            run(name="overlap", expected=4, data=str(a))
            assert run(name="zero-jit", **{"jit-mib": "0"})["manual_jit_bytes"] == 0
            print("PASS: incompatible ABI, undersized function table and writable/Runtime overlap are rejected")

            cert = a / "resources/cacert.pem"
            original_cert = cert.read_bytes()
            with writable(cert):
                try:
                    cert.write_bytes(original_cert + b"\ncorrupt\n")
                    run(name="tampered", expected=2)
                finally:
                    cert.write_bytes(original_cert)
            with writable(cert.parent):
                try:
                    cert.unlink()
                    run(name="missing-resource", expected=2)
                    external = work / "external-cert"
                    external.write_bytes(original_cert)
                    cert.symlink_to(external)
                    run(name="linked-resource", expected=2)
                finally:
                    if cert.is_symlink() or cert.exists():
                        cert.unlink()
                    cert.write_bytes(original_cert)
                    cert.chmod(0o444)
            manifest_file = a / "manifest.json"
            original_manifest = manifest_file.read_bytes()
            with writable(manifest_file):
                try:
                    manifest_file.write_bytes(b"")
                    run(name="empty-manifest", expected=2)
                    with manifest_file.open("wb") as stream:
                        stream.truncate(9 * 1024 * 1024)
                    run(name="oversized-manifest", expected=2)
                    manifest = json.loads(original_manifest)
                    manifest["artifacts"]["../outside"] = "0" * 64
                    manifest_file.write_text(json.dumps(manifest))
                    run(name="path-escape", expected=2)
                finally:
                    manifest_file.write_bytes(original_manifest)
            print("PASS: changed/missing/symlink artifacts, empty manifests and traversal paths fail before Runtime load")

            # Deliberate fixture violations: never touch the published source store.
            saved_lock = lock_a.with_suffix(".saved")
            lock_a.rename(saved_lock)
            try:
                run(name="missing-lease", expected=2)
                os.mkfifo(lock_a, 0o600)
                started = time.monotonic()
                run(name="fifo-lease", expected=2, **{"lease-timeout-ms": "50"})
                assert time.monotonic() - started < 2, "FIFO open bypassed lease timeout"
                lock_a.unlink()
                lock_a.symlink_to(saved_lock)
                run(name="symlink-lease", expected=2)
            finally:
                lock_a.unlink(missing_ok=True)
                saved_lock.rename(lock_a)
            print("PASS: missing, FIFO and symlink lease paths are rejected without blocking")

            # A replacement while acquisition is blocked must not split the readers.
            # This is not a claim that flock prevents a privileged unlink.
            with lock_a.open("rb") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                waiter = subprocess.Popen(arguments(a, "replaced-lease", **{"lease-timeout-ms": "3000"}),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(waiter)
                with selectors.DefaultSelector() as selector:
                    selector.register(waiter.stderr, selectors.EVENT_READ)
                    assert selector.select(timeout=5)
                    assert waiter.stderr.readline().strip() == "waiting for version lease"
                lock_a.rename(saved_lock)
                try:
                    lock_a.touch()
                    fcntl.flock(stream, fcntl.LOCK_UN)
                    stdout, _ = waiter.communicate(timeout=5)
                    assert waiter.returncode == 2 and "replaced" in json.loads(stdout)["error"]
                finally:
                    lock_a.unlink(missing_ok=True)
                    saved_lock.rename(lock_a)
            print("PASS: lease identity replacement during acquisition is detected")

            info = run(name="final-valid")
            assert info["build_id"] == a.name and exclusive_available(lock_a)
            print("PASS: bootstrap/ABI checks complete; APK UI, sandbox and hard-memory guarantees require separate acceptance")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            # Version trees are cooperatively readonly; cleanup only this test's own tree.
            for directory, _, filenames in os.walk(work):
                Path(directory).chmod(0o700)
                for name in filenames:
                    path = Path(directory) / name
                    if not path.is_symlink():
                        path.chmod(0o600)


if __name__ == "__main__":
    main()
