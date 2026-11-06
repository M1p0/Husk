#!/usr/bin/env python3
"""Build the source-owned Java/WebKit acceptance APK without aapt or Android SDK.

Only this fixed fixture is supported. HUSK_JDK/JAVA_HOME and HUSK_D8 select the
same javac/R8 tools used by java-framework/build.sh. No installation or launch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path
PACKAGE = "org.husk.fixture"
ACTIVITY = PACKAGE + ".MainActivity"
ANDROID = "http://schemas.android.com/apk/res/android"
NONE = 0xFFFFFFFF


def chunk(kind, header_size, body, extension=b""):
    return struct.pack("<HHI", kind, header_size, 8 + len(extension) + len(body)) + extension + body


def binary_manifest():
    # Resource-map entries identify Android attributes; package is unnamespaced.
    android_attributes = {
        "name": 0x01010003, "label": 0x01010001,
        "versionCode": 0x0101021B, "versionName": 0x0101021C,
        "minSdkVersion": 0x0101020C, "targetSdkVersion": 0x01010270,
        "exported": 0x01010010,
    }
    strings = list(android_attributes)

    def index(value):
        if value not in strings:
            strings.append(value)
        return strings.index(value)

    def attribute(name, value, kind=3):
        namespace = NONE if name == "package" else index(ANDROID)
        raw = index(value) if kind == 3 else NONE
        data = raw if kind == 3 else value
        return struct.pack("<IIIHBBI", namespace, index(name), raw, 8, 0, kind, data)

    def start(name, attributes=()):
        attrs = b"".join(attribute(*a) for a in attributes)
        extension = struct.pack("<II6H", NONE, index(name), 20, 20, len(attributes), 0, 0, 0)
        return chunk(0x102, 16, extension + attrs, struct.pack("<II", 1, NONE))

    def end(name):
        return chunk(0x103, 16, struct.pack("<II", NONE, index(name)), struct.pack("<II", 1, NONE))

    def namespace(kind):
        return chunk(kind, 16, struct.pack("<II", index("android"), index(ANDROID)), struct.pack("<II", 1, NONE))

    body = namespace(0x100)
    body += start("manifest", [("package", PACKAGE), ("versionCode", 1, 0x10), ("versionName", "1.0")])
    body += start("uses-sdk", [("minSdkVersion", 26, 0x10), ("targetSdkVersion", 28, 0x10)]) + end("uses-sdk")
    body += start("application", [("label", "Husk Java Fixture")])
    body += start("activity", [("name", ACTIVITY), ("exported", 1, 0x12)])
    body += start("intent-filter")
    body += start("action", [("name", "android.intent.action.MAIN")]) + end("action")
    body += start("category", [("name", "android.intent.category.LAUNCHER")]) + end("category")
    body += end("intent-filter") + end("activity") + end("application") + end("manifest")
    body += namespace(0x101)

    def length8(value):
        if value > 0x7FFF:
            raise ValueError("fixture string exceeds AXML UTF-8 length limit")
        return bytes([(value >> 8) | 0x80, value & 0xFF]) if value > 127 else bytes([value])

    offsets, payload = [], bytearray()
    for value in strings:
        encoded = value.encode("utf-8")
        offsets.append(len(payload))
        payload += length8(len(value.encode("utf-16le")) // 2) + length8(len(encoded)) + encoded + b"\0"
    payload += b"\0" * (-len(payload) % 4)
    string_start = 28 + 4 * len(strings)
    pool = chunk(1, 28, struct.pack(f"<{len(offsets)}I", *offsets) + payload,
                 struct.pack("<5I", len(strings), 0, 0x100, string_start, 0))
    resources = chunk(0x180, 8, struct.pack(f"<{len(android_attributes)}I", *android_attributes.values()))
    return chunk(3, 8, pool + resources + body)


def icon_png():
    # A source-generated fixture marker, unrelated to imported APK artwork.
    def png_chunk(name, content):
        return struct.pack(">I", len(content)) + name + content + struct.pack(">I", zlib.crc32(name + content) & 0xFFFFFFFF)
    rows = bytearray()
    for y in range(64):
        rows.append(0)
        for x in range(64):
            rows += bytes((25, 189, 115, 255) if (x // 16 + y // 16) % 2 == 0 else (26, 75, 127, 255))
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">2I5B", 64, 64, 8, 6, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"IEND", b"")


def run(command, log):
    with log.open("ab") as output:
        result = subprocess.run([str(v) for v in command], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"tool failed ({result.returncode}); see {log}\n{log.read_text(errors='replace')[-10000:]}")


def compile_java(javac, sources, destination, work, name, classpath=None):
    destination.mkdir()
    arguments = ["--release", "8", "-nowarn", "-encoding", "UTF-8", "-d", str(destination)]
    if classpath:
        arguments += ["-cp", classpath]
    arguments += [str(p) for p in sources]
    argfile = work / (name + ".args")
    argfile.write_text("\n".join('"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"' for a in arguments) + "\n")
    run([javac, "-J-Djava.io.tmpdir=" + str(task_directory() / "tmp"), "@" + str(argfile)], work / (name + ".log"))


def jar_classes(directory, output):
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*.class")):
            archive.write(path, path.relative_to(directory).as_posix())


def tools():
    homes = [os.environ.get("HUSK_JDK"), os.environ.get("JAVA_HOME"),
             "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
             "/Volumes/GTAV/husk2/tools-dl/jdk/Contents/Home"]
    jdk = next((Path(p).expanduser().resolve() for p in homes if p and (Path(p).expanduser() / "bin/javac").is_file()), None)
    jars = [os.environ.get("HUSK_D8"), str(ROOT / "build/tools/r8.jar"), "/Volumes/GTAV/husk2/tools-dl/r8.jar"]
    d8 = next((Path(p).expanduser().resolve() for p in jars if p and Path(p).expanduser().is_file()), None)
    if not jdk or not d8:
        raise RuntimeError("set HUSK_JDK to a JDK and HUSK_D8 to R8 8.5+ r8.jar")
    return jdk, d8


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="new APK output; existing files are never overwritten")
    parser.add_argument("--work", type=Path, help="compiler work directory within the current task's temp directory")
    options = parser.parse_args()
    task = task_directory(label="macos-fixture")
    work_root = temporary_path(options.work or task / "tmp")
    name = "HuskJavaFixture-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{os.getpid()}.apk"
    output = temporary_path(options.out or task / "evidence" / name)
    if os.path.lexists(output):
        raise FileExistsError(f"choose a new APK output: {output}")
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="compile-", dir=work_root))
    jdk, d8 = tools()
    print(f"Compiling source fixture in {work}", flush=True)
    framework = ROOT / "src/java-framework"
    stubs, framework_classes, app_classes = work / "stubs", work / "framework", work / "app"
    compile_java(jdk / "bin/javac", sorted((framework / "stubs").rglob("*.java")), stubs, work, "stubs")
    compile_java(jdk / "bin/javac", sorted((framework / "src").rglob("*.java")) + sorted((framework / "gen-src").rglob("*.java")),
                 framework_classes, work, "framework", str(stubs))
    compile_java(jdk / "bin/javac", [Path(__file__).parent / "fixture/MainActivity.java"], app_classes, work, "app",
                 os.pathsep.join([str(framework_classes), str(stubs)]))
    app_jar, framework_jar = work / "app.jar", work / "framework.jar"
    jar_classes(app_classes, app_jar)
    jar_classes(framework_classes, framework_jar)
    dex = work / "dex"
    dex.mkdir()
    run([jdk / "bin/java", "-Djava.io.tmpdir=" + str(task / "tmp"), "-cp", d8, "com.android.tools.r8.D8", "--min-api", "26", "--lib", jdk,
         "--classpath", framework_jar, "--output", dex, app_jar], work / "d8.log")
    if not (dex / "classes.dex").read_bytes().startswith(b"dex\n"):
        raise RuntimeError("D8 did not produce a DEX file")
    staged = work / "fixture.apk"
    contract = {"package": PACKAGE, "activity": ACTIVITY, "log_tag": "HuskFixture",
                "default_surface": {"width": 720, "height": 1280, "density": 2},
                "java_rows_dp": {"title": 32, "counter": 32, "increment": 48, "input": 48, "finish": 48},
                "padding_dp": {"horizontal": 12, "vertical": 8},
                "web": "Remaining weighted height, green; native button turns panel cyan and increments WEB clicks",
                "background": "HUSK_FIXTURE tick=N every second; focus loss does not cancel ticker",
                "clean_exit": "finish button calls Activity.finish; onDestroy destroys WebView and logs onDestroy_complete"}
    entries = {"AndroidManifest.xml": binary_manifest(), "classes.dex": (dex / "classes.dex").read_bytes(),
               "res/drawable/ic_launcher.png": icon_png(), "assets/fixture-contract.json": (json.dumps(contract, indent=2) + "\n").encode()}
    with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            item = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, data)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination, staged.open("rb") as source:
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
    print(json.dumps({"apk": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                      "task_directory": str(task), "tmpdir": os.environ.get("TMPDIR"),
                      "work": str(work), "jdk": str(jdk), "r8": str(d8), **contract}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f"Fixture build failed: {error}")
