#!/usr/bin/env python3
"""Exercise real CPU image/layer pixels on a published macOS Runtime, using private developer APIs.

This runs bounded Java fixtures through tools/java-test.c linked to the selected
dylib. It does not launch an APK, change production JIT/stack settings or claim
that software HardwareBuffer emulation supplies external GPU handles.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/runtime"))
from test_apks_macos import FRAMEWORKS, hold_version, preflight


def command(arguments, log: Path, *, environment=None, pass_fds=(), timeout=120):
    result = subprocess.run([str(value) for value in arguments], cwd=ROOT, env=environment,
                            pass_fds=pass_fds, capture_output=True, text=True,
                            errors="replace", timeout=timeout)
    log.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"command exited {result.returncode}: see {log}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--framework", type=Path, help="development dex override; default: the selected version's dex")
    parser.add_argument("--out", type=Path, help="new output directory")
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    framework = (args.framework or runtime / "resources/java-runtime/husk-framework.dex").resolve(strict=True)
    out = (args.out or ROOT / "build/surface-image-tests" /
           (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}")).resolve()
    if out == runtime or runtime in out.parents or out in runtime.parents:
        parser.error("output and immutable Runtime directories must not overlap")
    out.mkdir(parents=True, exist_ok=False)
    jdk = os.environ.get("HUSK_JDK") or os.environ.get("JAVA_HOME")
    javac = Path(jdk) / "bin/javac" if jdk else shutil.which("javac")
    java = Path(jdk) / "bin/java" if jdk else shutil.which("java")
    d8 = Path(os.environ.get("HUSK_D8", str(ROOT / "build/tools/r8.jar"))).resolve(strict=True)
    if not javac or not java:
        parser.error("set HUSK_JDK to an installed JDK")
    report = {"runtime": str(runtime), "framework": str(framework), "private_developer_api": True, "tests": []}
    try:
        with hold_version(runtime) as (lease, lease_path):
            report["version_lease"] = str(lease_path)
            report["preflight"] = preflight(runtime, out)
            classes, dex = out / "classes", out / "dex"
            classes.mkdir(); dex.mkdir()
            fixtures = ["ImageReaderBufferTest", "NativeSurfaceLayerTest"]
            sourcepath = os.pathsep.join(str(ROOT / "src/java-framework" / directory)
                                         for directory in ["src", "gen-src", "stubs"])
            command([javac, "--release", "8", "-nowarn", "-sourcepath", sourcepath, "-d", classes,
                     *[ROOT / "tests/translation-layer/fixtures" / (name + ".java") for name in fixtures]],
                    out / "javac.log")
            jar = out / "fixtures.jar"
            with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in fixtures:
                    for path in sorted(classes.glob(name + "*.class")):
                        archive.write(path, path.name)
            command([java, "-cp", d8, "com.android.tools.r8.D8", "--min-api", "26", "--output", dex, jar], out / "d8.log")
            library = runtime / "lib/libHuskRuntime.dylib"
            harness = out / "java-image-test"
            arguments = ["clang", "-g", "-O1", "-Wno-unused-function", "-Wno-deprecated-declarations",
                         "-I" + str(ROOT / "src/translation-layer"), "-I" + str(ROOT / "src/translation-layer-next"),
                         ROOT / "tools/java-test.c", library, "-Wl,-rpath," + str(library.parent), "-o", harness]
            for item in FRAMEWORKS:
                arguments += ["-framework", item]
            command(arguments, out / "clang.log")
            command(["otool", "-L", harness], out / "harness-otool.txt")
            if "libHuskRuntime.dylib" not in (out / "harness-otool.txt").read_text():
                raise RuntimeError("test harness did not dynamically link the selected Runtime")
            environment = os.environ.copy()
            for key in ["DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"]:
                environment.pop(key, None)
            java_root = runtime / "resources/java-runtime"
            environment.update(TL_ART=str(java_root / "com.android.art"), TL_FRAMEWORK=str(framework),
                               ANDROID_ART_ROOT=str(java_root / "com.android.art"),
                               ANDROID_I18N_ROOT=str(java_root / "com.android.i18n"),
                               ANDROID_TZDATA_ROOT=str(java_root / "com.android.tzdata"))
            for name in fixtures:
                result = command([harness, dex / "classes.dex", name, "run", "()Ljava/lang/String;"],
                                 out / (name + ".log"), environment=environment, pass_fds=(lease,), timeout=40)
                if 'RESULT "PASS:' not in result.stdout:
                    raise RuntimeError(f"fixture did not report passing pixels: {name}")
                report["tests"].append({"name": name, "passed": True})
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        (out / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
