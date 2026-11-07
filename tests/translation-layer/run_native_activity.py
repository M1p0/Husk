#!/usr/bin/env python3
"""Verify NativeActivity against a real Android ELF and the selected framework dex."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--runtime", type=Path, required=True)
parser.add_argument("--linker", type=Path, required=True, help="ELF lld (rust-lld is supported)")
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
task = task_directory("native-activity-test")
out = temporary_path(args.out)
out.mkdir(parents=True, exist_ok=False)
runtime = args.runtime.resolve(strict=True)
jdk = Path(os.environ.get("HUSK_JDK", "/opt/homebrew/opt/openjdk@21"))
env = dict(os.environ)
for key in ("DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "TL_ORIGINAL_CODE_VIEW", "TL_CODE_VIEWS_TEST"):
    env.pop(key, None)

def run(arguments, name, timeout=120, environment=None):
    result = subprocess.run([str(a) for a in arguments], env=environment or env, cwd=ROOT,
                            capture_output=True, text=True, errors="replace", timeout=timeout)
    (out / name).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"exit {result.returncode}: {out / name}")
    return result.stdout

report = {"runtime": str(runtime), "game_startup": "not_tested_by_this_fixture"}
try:
    fixtures = ROOT / "tests/translation-layer/fixtures"
    obj, library = out / "native.o", out / "libnative-activity-lifecycle.so"
    run(["xcrun", "clang", "--target=aarch64-linux-android29", "-O1", "-fPIC", "-c", fixtures / "native_activity_lifecycle.c", "-o", obj], "clang-guest.log")
    linker = args.linker.resolve(strict=True)
    flavor = ["-flavor", "gnu"] if linker.name == "rust-lld" else []
    linker_env = dict(env)
    if linker.name == "rust-lld":
        linker_env["DYLD_LIBRARY_PATH"] = str(linker.parents[3])
    run([linker, *flavor, "-shared", "--hash-style=both", "-z", "max-page-size=16384", "-soname", library.name, obj, "-o", library], "link-guest.log", environment=linker_env)
    classes, dex = out / "classes", out / "dex"
    classes.mkdir(); dex.mkdir()
    sourcepath = os.pathsep.join(str(ROOT / "src/java-framework" / p) for p in ("src", "gen-src", "stubs"))
    run([jdk / "bin/javac", "-J-Djava.io.tmpdir=" + str(task / "tmp"), "--release", "8", "-nowarn", "-sourcepath", sourcepath,
         "-d", classes, fixtures / "NativeActivityLifecycleTest.java"], "javac.log")
    jar = out / "fixture.jar"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(classes.glob("NativeActivityLifecycleTest*.class")):
            archive.write(path, path.name)
    run([jdk / "bin/java", "-Djava.io.tmpdir=" + str(task / "tmp"), "-cp", ROOT / "build/tools/r8.jar", "com.android.tools.r8.D8", "--min-api", "26", "--output", dex, jar], "d8.log")
    dylib = runtime / "lib/libHuskRuntime.dylib"
    harness = out / "java-test"
    run(["xcrun", "clang", "-O1", "-g", "-Wno-deprecated-declarations", "-I" + str(ROOT / "src/translation-layer-next"),
         ROOT / "tools/java-test.c", dylib, "-Wl,-rpath," + str(dylib.parent), "-o", harness], "clang-host.log")
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", harness], "codesign.log")
    java = runtime / "resources/java-runtime"
    env.update(TL_ART=str(java / "com.android.art"), TL_FRAMEWORK=str(java / "husk-framework.dex"), HUSK_NATIVE_TEST_DIR=str(out),
               ANDROID_ART_ROOT=str(java / "com.android.art"), ANDROID_I18N_ROOT=str(java / "com.android.i18n"), ANDROID_TZDATA_ROOT=str(java / "com.android.tzdata"))
    result = run([harness, dex / "classes.dex", "NativeActivityLifecycleTest", "run", "()Ljava/lang/String;"], "results.log", timeout=40)
    if 'RESULT "PASS:' not in result:
        raise RuntimeError("fixture returned without an explicit passing result")
    report["native_activity"] = "passed"
    print(next(line for line in result.splitlines() if line.startswith("RESULT")))
except Exception as error:
    report["error"] = f"{type(error).__name__}: {error}"
finally:
    (out / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
print(out)
raise SystemExit(1 if "error" in report else 0)
