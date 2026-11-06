#!/usr/bin/env python3
"""Run bounded, local APK diagnostics against a published macOS Runtime dylib.

This developer harness calls private tl_* test interfaces. It does not add APK
execution to the stable M1 ABI, provide M2 app shells or prove sandbox/memory
isolation. Exit status alone is not evidence of a correctly rendered app.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import zipfile

sys.dont_write_bytecode = True
from package_macos import FRAMEWORKS, source_snapshot


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def checked(arguments, *, timeout=60, log: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run([str(value) for value in arguments], cwd=ROOT, capture_output=True,
                            text=True, errors="replace", timeout=timeout)
    if log is not None:
        log.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {arguments[0]}; "
                           f"{log if log else result.stderr[-2000:]}")
    return result


def same_lease(descriptor: int, path: Path) -> bool:
    held, named = os.fstat(descriptor), path.lstat()
    return (stat.S_ISREG(held.st_mode) and stat.S_ISREG(named.st_mode)
            and held.st_nlink == named.st_nlink == 1
            and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino))


@contextmanager
def hold_version(runtime: Path):
    directory = runtime.parent / ".leases"
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise ValueError("Runtime lease directory must be a real directory")
    lease = directory / (runtime.name + ".lock")
    descriptor = os.open(lease, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not same_lease(descriptor, lease):
            raise ValueError("missing or unsafe Runtime lease file")
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Runtime lease acquisition timed out")
                time.sleep(0.01)
        if not same_lease(descriptor, lease):
            raise ValueError("Runtime lease object changed during acquisition")
        yield descriptor, lease
    finally:
        os.close(descriptor)


def check_macos_graphics(path: Path, evidence: Path) -> None:
    architectures = checked(["lipo", "-archs", path]).stdout
    commands = checked(["otool", "-arch", "arm64", "-l", path]).stdout
    evidence.write_text(architectures + "\n" + commands)
    if "arm64" not in architectures.split():
        raise ValueError(f"graphics library has no arm64 slice: {path}")
    macos = False
    for command in re.split(r"Load command \d+", commands):
        if "cmd LC_VERSION_MIN_MACOSX" in command:
            macos = True
        if "cmd LC_BUILD_VERSION" in command:
            match = re.search(r"\bplatform\s+(\S+)", command)
            if match and match.group(1).lower() in {"1", "macos"}:
                macos = True
    if not macos:
        raise ValueError(f"graphics library is not a macOS arm64 image: {path}")


def preflight(runtime: Path, out: Path) -> dict:
    probe = out / "runtime-probe"
    checked(["clang", "-fobjc-arc", "-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations",
             ROOT / "tools/runtime-probe.m", "-framework", "Foundation", "-o", probe],
            log=out / "build-probe.log")
    base = out / "preflight"
    for name in ["package", "data", "cache", "config"]:
        (base / name).mkdir(parents=True)
    # The probe configures metadata only, in a separate process. Its 256 MiB
    # snapshot neither allocates JIT nor changes the APK harness's existing JIT
    # defaults. The diagnostic below does not override a JIT target or stack.
    args = [probe, "--runtime", runtime, "--app-id", "dev.husk.apk-diagnostic.preflight",
            "--generation", out.name, "--jit-mib", "256"]
    for role in ["package", "data", "cache", "config"]:
        args.extend(["--" + role, base / role])
    result = checked(args, timeout=60, log=out / "preflight.log")
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if not payload.get("ok") or payload.get("build_id") != runtime.name:
        raise ValueError("Runtime preflight did not confirm the selected version")
    return payload


def build_harness(runtime: Path, out: Path) -> Path:
    library = runtime / "lib/libHuskRuntime.dylib"
    harness = out / "javaapp-dynamic-test"
    arguments = ["clang", "-g", "-O1", "-Wall", "-Wno-unused-function", "-Wno-deprecated-declarations",
                 "-I" + str(ROOT / "src/translation-layer"),
                 "-I" + str(ROOT / "src/translation-layer-next"),
                 ROOT / "tools/javaapp-test.c", library,
                 "-Wl,-rpath," + str(library.parent), "-o", harness]
    for framework in FRAMEWORKS:
        arguments.extend(["-framework", framework])
    (out / "harness-build-command.json").write_text(json.dumps([str(x) for x in arguments], indent=2) + "\n")
    checked(arguments, timeout=120, log=out / "build-harness.log")
    dependencies = checked(["otool", "-L", harness]).stdout
    load_commands = checked(["otool", "-l", harness]).stdout
    undefined = checked(["nm", "-u", harness]).stdout
    defined = checked(["nm", "-gU", harness]).stdout
    (out / "harness-otool-L.txt").write_text(dependencies)
    (out / "harness-otool-l.txt").write_text(load_commands)
    (out / "harness-nm-undefined.txt").write_text(undefined)
    (out / "harness-nm-defined.txt").write_text(defined)
    if "libHuskRuntime.dylib" not in dependencies or str(library.parent) not in load_commands:
        raise ValueError("harness did not link the selected Runtime with its fixed version rpath")
    if "_tl_dvm_start" not in undefined or "_tl_javaapp_start" not in undefined:
        raise ValueError("harness is not importing its core execution functions from the dylib")
    if re.search(r"\b_tl_(?:dvm|jni|bionic|javaapp|xmem)_", defined):
        raise ValueError("harness unexpectedly defines native-runtime core symbols")
    return harness


def png_dimensions(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(24)
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return {"width": width, "height": height}
    return {}


def bitmap_evidence(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) < 54 or data[:2] != b"BM":
        return {"valid_header": False}
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    bits = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    evidence = {"width": width, "height": abs(height), "bits_per_pixel": bits}
    if width <= 0 or not height or bits not in {24, 32} or compression not in {0, 3}:
        return evidence
    stride = (width * (bits // 8) + 3) & ~3
    if offset + stride * abs(height) > len(data):
        evidence["truncated"] = True
        return evidence
    evidence["pixel_data_present"] = True
    first = data[offset:offset + 3]
    uniform = True
    for y in range(abs(height)):
        row = data[offset + y * stride:offset + y * stride + width * (bits // 8)]
        if any(row[channel::bits // 8].count(bytes((first[channel],))) != width for channel in range(3)):
            uniform = False
            break
    evidence["uniform"] = uniform
    if uniform:
        evidence["uniform_rgb"] = list(reversed(first))
    pixels = []
    for y in range(0, abs(height), max(1, abs(height) // 36)):
        for x in range(0, width, max(1, width // 64)):
            start = offset + y * stride + x * (bits // 8)
            pixels.append(tuple(data[start:start + 3]))
    if pixels:
        evidence["sampled_unique_rgb"] = len(set(pixels))
        evidence["sampled_mean_channel"] = round(sum(sum(pixel) for pixel in pixels) / (3 * len(pixels)), 3)
    return evidence


def stop_owned(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        process.wait()
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def child_environment(runtime: Path, case: Path, egl: Path, gles: Path) -> dict:
    environment = os.environ.copy()
    for name in ["TL_AOSP", "TL_AOSP_FAT", "TL_SPLITS", "TL_PACKAGE",
                 "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"]:
        environment.pop(name, None)
    java = runtime / "resources/java-runtime"
    system = runtime / "resources/android-system"
    environment.update({
        "TL_ART": str(java / "com.android.art"),
        "TL_FRAMEWORK": str(java / "husk-framework.dex"),
        "TL_FRAMEWORK_CORE": str(java / "husk-framework-core.dex"),
        "TL_HTTP_LEGACY": str(java / "org.apache.http.legacy.jar"),
        "TL_FRAMEWORK_RES": str(java / "framework-res.apk"),
        "TL_SYSFONTS": str(system),
        "ANDROID_ART_ROOT": str(java / "com.android.art"),
        "ANDROID_I18N_ROOT": str(java / "com.android.i18n"),
        "ANDROID_TZDATA_ROOT": str(java / "com.android.tzdata"),
        "ANDROID_ROOT": str(system),
        "ANDROID_DATA": str(case / "data"),
        "TL_DATA": str(case / "data"), "TL_TMP": str(case / "tmp"),
        "TL_FRAMEDIR": str(case / "frames"), "TL_CTL": str(case / "control.txt"),
        "TL_ANGLE_EGL": str(egl), "TL_ANGLE_GLES": str(gles),
        "TL_CYCLES": "1", "DYLD_PRINT_LIBRARIES": "1",
    })
    return environment


def run_apk(harness: Path, runtime: Path, apk: Path, case: Path, egl: Path, gles: Path,
            seconds: int, timeout: int, lease_fd: int) -> dict:
    for name in ["package", "data", "cache", "config", "tmp", "frames"]:
        (case / name).mkdir(parents=True)
    packaged_apk = case / "package/base.apk"
    shutil.copy2(apk, packaged_apk)
    with zipfile.ZipFile(packaged_apk) as archive:
        names = archive.namelist()
        flutter = "lib/arm64-v8a/libflutter.so" in names
    screenshot = case / "screenshot.png"
    (case / "control.txt").write_text(
        f"wait {max(1000, (seconds - 2) * 1000)}\nshot {screenshot}\nwait 1000\nquit\n")
    environment = child_environment(runtime, case, egl, gles)
    log = case / "run.log"
    timed_out = False
    started = time.monotonic()
    with log.open("wb") as output:
        process = subprocess.Popen([str(harness), str(packaged_apk), str(seconds + 5)],
                                   cwd=case, env=environment, stdout=output, stderr=subprocess.STDOUT,
                                   pass_fds=(lease_fd,))
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            stop_owned(process)
    text = log.read_text(errors="replace")
    frame_counts = [int(value) for pattern in [r"javaapp:\s+(\d+) frames", r"ctl: shot[^\n]*\(frame (\d+)\)"]
                    for value in re.findall(pattern, text)]
    crash = bool(re.search(r"=== CRASH|\*\*\* FATAL|EXC_BAD_ACCESS|SIG(?:SEGV|BUS|ILL|ABRT|TRAP)\b", text))
    if process.returncode is not None and process.returncode < 0 and not timed_out:
        crash = True
    images = []
    for image in sorted(case.glob("*.png")):
        images.append({"path": str(image), "bytes": image.stat().st_size, **png_dimensions(image)})
    bitmap = case / "frames/latest.bmp"
    bitmap_info = {"path": str(bitmap), "bytes": bitmap.stat().st_size, **bitmap_evidence(bitmap)} if bitmap.exists() else None
    frame_count = max(frame_counts) if frame_counts else None
    evidence = bool(any(image.get("width", 0) > 0 and image.get("height", 0) > 0 for image in images)
                    or (bitmap_info and bitmap_info.get("pixel_data_present")) or (frame_count or 0) > 0)
    uniform_main = bitmap_info.get("uniform") if bitmap_info else None
    if uniform_main is True:
        render_status = "uniform-main-bitmap"
    elif uniform_main is False:
        render_status = "nonuniform-main-bitmap-review-required"
    elif images:
        render_status = "captured-image-review-required"
    elif (frame_count or 0) > 0:
        render_status = "frames-reported-without-main-bitmap"
    else:
        render_status = "no-frame-evidence"
    return {
        "apk": str(apk), "apk_sha256": digest(packaged_apk), "flutter_library_present": flutter,
        "harness": "javaapp-test-private-api", "pid": process.pid, "exit_code": process.returncode,
        "timeout": timed_out, "crash": crash, "frames": frame_count,
        "elapsed_seconds": round(time.monotonic() - started, 3), "log": str(log),
        "data": str(case / "data"), "tmp": str(case / "tmp"), "control": str(case / "control.txt"),
        "images": images, "bitmap": bitmap_info, "frame_evidence_observed": evidence,
        "uniform_main_bitmap": uniform_main, "nonuniform_main_bitmap": uniform_main is False,
        "render_evidence_status": render_status, "render_evidence_observed": uniform_main is False,
        "selected_runtime_observed_in_dyld_log": str(runtime / "lib/libHuskRuntime.dylib") in text,
        "visual_review": "required; images and frame counts do not prove app correctness",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path, help="published version directory")
    parser.add_argument("--egl", required=True, type=Path, help="real macOS arm64 libEGL.dylib")
    parser.add_argument("--gles", required=True, type=Path, help="real macOS arm64 libGLESv2.dylib")
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--timeout", type=int, help="per-APK wall timeout (default: max(60, seconds+30))")
    parser.add_argument("--out", type=Path, help="new output directory; whitespace is unsupported by the harness control format")
    parser.add_argument("apks", nargs="+", type=Path)
    options = parser.parse_args()
    task = task_directory(label="macos-apk-diagnostics")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("requires Apple Silicon macOS")
    if not 3 <= options.seconds <= 300:
        parser.error("--seconds must be between 3 and 300")
    timeout = options.timeout if options.timeout is not None else max(60, options.seconds + 30)
    if timeout <= options.seconds or timeout > 900:
        parser.error("--timeout must exceed --seconds and be at most 900")
    runtime, egl, gles = [path.resolve(strict=True) for path in [options.runtime, options.egl, options.gles]]
    apks = [path.resolve(strict=True) for path in options.apks]
    if not runtime.is_dir() or not egl.is_file() or not gles.is_file() or not all(path.is_file() for path in apks):
        parser.error("Runtime must be a directory and APK/graphics inputs must be files")
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    out = temporary_path(options.out or task / "evidence" / ("runtime-apk-diagnostics-" + name))
    if any(character.isspace() for character in str(out)):
        parser.error("--out cannot contain whitespace: the legacy screenshot control format has no quoting")
    if out == runtime or runtime in out.parents or out in runtime.parents:
        parser.error("output and Runtime version directories must not overlap")
    out.mkdir(parents=True, exist_ok=False)
    report = {
        "scope": "local compatibility diagnostic using private tl_* interfaces; not stable ABI APK launch or M2",
        "task_directory": str(task),
        "temporary_environment": {key: os.environ.get(key) for key in ["TMPDIR", "TMP", "TEMP"]},
        "runtime": str(runtime), "output": str(out), "observed_at": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "architecture": platform.machine()},
        "egl": str(egl), "gles": str(gles), "cases": [],
        "jit_policy": "APK harness settings unchanged; preflight metadata does not allocate JIT",
        "sandbox_isolation": "unverified", "hard_memory_limit": "unsupported",
    }
    try:
        with hold_version(runtime) as (lease_fd, lease_path):
            report["version_lease"] = str(lease_path)
            report["preflight"] = preflight(runtime, out)
            manifest = json.loads((runtime / "manifest.json").read_text())
            report["runtime_source_snapshot"] = manifest["source_tree_sha256"]
            if manifest["source_tree_sha256"] != source_snapshot()[2]:
                raise ValueError("private diagnostic ABI requires a Runtime built from this exact native source/header snapshot")
            check_macos_graphics(egl, out / "egl-macho.txt")
            check_macos_graphics(gles, out / "gles-macho.txt")
            harness = build_harness(runtime, out)
            for index, apk in enumerate(apks):
                if not same_lease(lease_fd, lease_path):
                    raise ValueError("Runtime lease identity changed before APK launch")
                slug = re.sub(r"[^A-Za-z0-9._-]+", "-", apk.stem)[:70]
                case = out / f"{index + 1:02d}-{slug}"
                result = run_apk(harness, runtime, apk, case, egl, gles, options.seconds, timeout, lease_fd)
                report["cases"].append(result)
                (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if not same_lease(lease_fd, lease_path):
                raise ValueError("Runtime lease identity changed during diagnostics")
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"], file=sys.stderr)
    finally:
        (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"Results: {out / 'results.json'}", flush=True)
    if report.get("error"):
        return 1
    return 0 if all(case["exit_code"] == 0 and not case["timeout"] and not case["crash"]
                    and case["render_evidence_observed"] for case in report["cases"]) else 1


if __name__ == "__main__":
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("diagnostic runner interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
