#!/usr/bin/env python3
"""Install an unchanged APK into an independent local macOS application shell.

The app dynamically selects a shared macos-app Runtime at each cold start.
This developer installer does not grant an OS sandbox or hard memory isolation.
Existing installations are deliberately never overwritten; signed APK updates
and live-installation migration are a later transaction.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "build/macos-runtime"
DEFAULT_APPLICATIONS = ROOT / "build/macos-apps"


def run(arguments: list[str]) -> str:
    return subprocess.check_output(arguments, text=True, stderr=subprocess.STDOUT).strip()


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def chunks(data: bytes, start: int, end: int):
    while start < end:
        kind, header, size = struct.unpack_from("<HHI", data, start)
        if header < 8 or size < header or start + size > end:
            raise ValueError("invalid Android resource chunk")
        yield kind, start, header, size
        start += size


def string_pool(data: bytes, offset: int) -> list[str]:
    kind, header, size, count, _, flags, strings_start, _ = struct.unpack_from("<HHIIIIII", data, offset)
    if kind != 1 or header < 28 or count > 500_000 or header + count * 4 > size:
        raise ValueError("invalid Android string pool")
    result = []
    end = offset + size
    for i in range(count):
        cursor = offset + strings_start + u32(data, offset + header + i * 4)
        if cursor < offset + strings_start or cursor >= end:
            raise ValueError("string offset outside resource pool")
        def length8():
            nonlocal cursor
            first = data[cursor]
            cursor += 1
            if first & 0x80:
                value = ((first & 0x7f) << 8) | data[cursor]
                cursor += 1
                return value
            return first
        if flags & 0x100:
            length8()
            length = length8()
            if cursor + length >= end:
                raise ValueError("truncated UTF-8 resource")
            value = data[cursor:cursor + length].decode("utf-8")
        else:
            length = u16(data, cursor)
            cursor += 2
            if length & 0x8000:
                length = ((length & 0x7fff) << 16) | u16(data, cursor)
                cursor += 2
            if cursor + length * 2 + 2 > end:
                raise ValueError("truncated UTF-16 resource")
            value = data[cursor:cursor + length * 2].decode("utf-16-le")
        result.append(value)
    return result


class Reference(int):
    pass


def manifest_nodes(data: bytes):
    if data.lstrip().startswith(b"<"):
        root = ET.fromstring(data)
        def visit(node):
            yield "start", node.tag.rsplit("}", 1)[-1], {key.rsplit("}", 1)[-1]: value for key, value in node.attrib.items()}
            for child in node:
                yield from visit(child)
            yield "end", node.tag.rsplit("}", 1)[-1], {}
        yield from visit(root)
        return
    if len(data) < 8 or u16(data, 0) != 3 or u32(data, 4) != len(data):
        raise ValueError("invalid binary Android manifest")
    strings = []
    for kind, offset, header, size in chunks(data, u16(data, 2), len(data)):
        if kind == 1:
            strings = string_pool(data, offset)
        elif kind == 0x102:
            if header < 16 or size < 36:
                raise ValueError("invalid Android start element")
            extension = offset + 16
            name = strings[u32(data, extension + 4)]
            attribute_start = u16(data, extension + 8)
            attribute_size = u16(data, extension + 10)
            count = u16(data, extension + 12)
            if attribute_size < 20 or extension + attribute_start + count * attribute_size > offset + size:
                raise ValueError("invalid Android attributes")
            values = {}
            for i in range(count):
                position = extension + attribute_start + i * attribute_size
                key = strings[u32(data, position + 4)]
                raw, value_type, value = u32(data, position + 8), data[position + 15], u32(data, position + 16)
                if value_type == 1:
                    values[key] = Reference(value)
                elif raw != 0xffffffff:
                    values[key] = strings[raw]
                elif value_type == 3:
                    values[key] = strings[value]
                elif value_type == 0x12:
                    values[key] = bool(value)
                else:
                    values[key] = value
            yield "start", name, values
        elif kind == 0x103:
            if size < 24:
                raise ValueError("invalid Android end element")
            yield "end", strings[u32(data, offset + 20)], {}


class ResourceTable:
    def __init__(self, data: bytes):
        if len(data) < 12 or u16(data, 0) != 2 or u32(data, 4) != len(data):
            raise ValueError("invalid Android resource table")
        self.data, self.strings, self.packages = data, [], []
        for kind, offset, header, size in chunks(data, u16(data, 2), len(data)):
            if kind == 1:
                self.strings = string_pool(data, offset)
            elif kind == 0x200:
                self.packages.append((offset, header, size))

    def values(self, resource_id: int):
        package, wanted_type, entry = resource_id >> 24, (resource_id >> 16) & 255, resource_id & 65535
        data = self.data
        for base, package_header, package_size in self.packages:
            if u32(data, base + 8) != package:
                continue
            type_offset = u32(data, base + 284) if package_header >= 288 else 0
            for kind, start, header, size in chunks(data, base + package_header, base + package_size):
                if kind != 0x201 or data[start + 8] + type_offset != wanted_type:
                    continue
                flags = data[start + 9]
                count, entries_start = u32(data, start + 12), u32(data, start + 16)
                config_size = u32(data, start + 20)
                if header < 20 + config_size or config_size < 16:
                    raise ValueError("invalid resource configuration")
                density = u16(data, start + 34)
                default_locale = data[start + 28:start + 32] == b"\0\0\0\0"
                entry_offset = None
                if flags & 1:
                    for i in range(count):
                        index, value = struct.unpack_from("<HH", data, start + header + 4 * i)
                        if index == entry:
                            entry_offset = value * 4
                            break
                elif entry < count:
                    if flags & 2:
                        value = u16(data, start + header + 2 * entry)
                        if value != 0xffff:
                            entry_offset = value * 4
                    else:
                        value = u32(data, start + header + 4 * entry)
                        if value != 0xffffffff:
                            entry_offset = value
                if entry_offset is None:
                    continue
                at = start + entries_start + entry_offset
                if at < start or at + 8 > start + size:
                    raise ValueError("resource entry outside chunk")
                entry_size, entry_flags = u16(data, at), u16(data, at + 2)
                if entry_flags & 1:
                    continue
                value_at = at + entry_size
                if value_at + 8 > start + size:
                    raise ValueError("truncated resource value")
                value_type, value = data[value_at + 3], u32(data, value_at + 4)
                if value_type == 3:
                    yield default_locale, density, self.strings[value]
                elif value_type == 1:
                    yield default_locale, density, Reference(value)

    def resolve(self, value, visited=frozenset()):
        if not isinstance(value, Reference):
            yield True, 0, value
        elif value not in visited and len(visited) < 16:
            for default, density, child in self.values(value):
                for child_default, child_density, text in self.resolve(child, visited | {value}):
                    yield default and child_default, density or child_density, text


def zip_read(archive: ZipFile, name: str, limit: int) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError(f"metadata asset too large: {name}")
    return archive.read(info)


def apk_metadata(apk: Path):
    with ZipFile(apk) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("APK contains duplicate ZIP entries")
        manifest, application, launchers = {}, {}, []
        activity = intent = None
        for event, name, values in manifest_nodes(zip_read(archive, "AndroidManifest.xml", 8 << 20)):
            if event == "start":
                if name == "manifest": manifest = values
                elif name == "application": application = values
                elif name in ("activity", "activity-alias"): activity = values
                elif name == "intent-filter" and activity is not None: intent = {"actions": [], "categories": []}
                elif name == "action" and intent is not None: intent["actions"].append(values.get("name"))
                elif name == "category" and intent is not None: intent["categories"].append(values.get("name"))
            elif name == "intent-filter" and intent is not None:
                if "android.intent.action.MAIN" in intent["actions"] and "android.intent.category.LAUNCHER" in intent["categories"]:
                    launchers.append(dict(activity))
                intent = None
            elif name in ("activity", "activity-alias"):
                activity = None
        package = manifest.get("package")
        if not isinstance(package, str) or len(package) > 255 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", package):
            raise ValueError("APK has no valid Android package identity")
        if not launchers:
            raise ValueError("APK has no launcher activity")
        table = ResourceTable(zip_read(archive, "resources.arsc", 64 << 20)) if "resources.arsc" in names else None
        label = launchers[0].get("label", application.get("label", package))
        label_values = list(table.resolve(label)) if table else [(True, 0, label)]
        label_values = [item for item in label_values if isinstance(item[2], str) and item[2].strip()]
        display_name = sorted(label_values, key=lambda item: not item[0])[0][2] if label_values else package
        icons = []
        icon_value = launchers[0].get("icon", application.get("icon"))
        if icon_value is not None:
            icon_values = list(table.resolve(icon_value)) if table else [(True, 0, icon_value)]
            for default, density, path in icon_values:
                if isinstance(path, str) and path in names and path.lower().endswith(".png"):
                    raw = zip_read(archive, path, 16 << 20)
                    if len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
                        width, height = struct.unpack_from(">II", raw, 16)
                        icons.append((default, min(width, height), density, path, raw))
        if not icons:
            for path in names:
                if path.lower().endswith(".png") and re.search(r"(?:^|/)ic_launcher(?:_round)?\.png$", path):
                    raw = zip_read(archive, path, 16 << 20)
                    if len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
                        width, height = struct.unpack_from(">II", raw, 16)
                        icons.append((True, min(width, height), 0, path, raw))
        icon = max(icons, key=lambda item: item[:3]) if icons else None
        return {"package": package, "label": display_name,
                "version_name": str(manifest.get("versionName", "1.0")),
                "version_code": str(manifest.get("versionCode", "1")),
                "launcher": launchers[0].get("targetActivity", launchers[0].get("name"))}, icon


def sync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def atomic_json(path: Path, value: dict):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def safe_relative(value: str) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith("/") and "\\" not in value and all(part not in ("", ".", "..") for part in value.split("/"))


def safe_file(root: Path, relative: str) -> Path:
    if not safe_relative(relative): raise ValueError("unsafe artifact path")
    path = root
    for index, part in enumerate(relative.split("/")):
        path = path / part
        info = path.lstat()
        final = index == len(relative.split("/")) - 1
        if (final and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)) or (not final and not stat.S_ISDIR(info.st_mode)):
            raise ValueError(f"unsafe artifact: {path}")
    return path


@contextmanager
def version_lease(runtime: Path):
    leases = runtime.parent / ".leases"
    if not stat.S_ISDIR(leases.lstat().st_mode): raise ValueError("unsafe lease directory")
    path = leases / (runtime.name + ".lock")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        def check():
            held, named = os.fstat(fd), path.lstat()
            if not stat.S_ISREG(held.st_mode) or not stat.S_ISREG(named.st_mode) or held.st_nlink != 1 or named.st_nlink != 1 or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                raise ValueError("lease object missing or replaced")
        check(); fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB); check()
        yield
        check()
    finally:
        os.close(fd)


def validate_runtime(runtime: Path) -> dict:
    manifest_path = safe_file(runtime, "manifest.json")
    if manifest_path.stat().st_size > 8 << 20: raise ValueError("manifest too large")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != 1 or manifest.get("abi_version") != 1 or manifest.get("build_id") != runtime.name or manifest.get("profile") != "macos-app" or manifest.get("host_target") != "macos-arm64":
        raise ValueError("select requires a matching macos-app Runtime manifest")
    artifacts = manifest.get("artifacts")
    required = {"lib/libHuskRuntime.dylib", "resources/angle/libEGL.dylib", "resources/angle/libGLESv2.dylib", "resources/java-runtime/husk-framework.dex", "resources/cacert.pem"}
    if not isinstance(artifacts, dict) or not required.issubset(artifacts): raise ValueError("incomplete app Runtime manifest")
    for relative, expected in artifacts.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256(safe_file(runtime, relative)) != expected:
            raise ValueError(f"Runtime artifact hash mismatch: {relative}")
    actual = set()
    for path in runtime.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode): continue
        relative = path.relative_to(runtime).as_posix()
        safe_file(runtime, relative)
        if relative != "manifest.json": actual.add(relative)
    if actual != set(artifacts): raise ValueError("Runtime inventory differs from its manifest")
    for relative in ["lib/libHuskRuntime.dylib", "resources/angle/libEGL.dylib", "resources/angle/libGLESv2.dylib"]:
        path = runtime / relative
        if "arm64" not in run(["xcrun", "lipo", "-archs", str(path)]).split(): raise ValueError(f"missing arm64 host library: {relative}")
        if not re.search(r"\bplatform\s+MACOS\b", run(["xcrun", "vtool", "-arch", "arm64", "-show-build", str(path)])):
            raise ValueError(f"library is not a macOS build: {relative}")
        run(["codesign", "--verify", "--strict", str(path)])
    return manifest


def make_icon(resources: Path, raw: bytes):
    source = resources / "AppIconSource.png"
    source.write_bytes(raw)
    with tempfile.TemporaryDirectory(prefix="husk-icon-") as temporary:
        iconset = Path(temporary) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                target = iconset / f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                run(["sips", "-z", str(size * scale), str(size * scale), str(source), "--out", str(target)])
        run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")])


def publish_exclusive(source: Path, destination: Path):
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    rename = library.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(source), os.fsencode(destination), 4):
        issue = ctypes.get_errno()
        raise OSError(issue, os.strerror(issue), str(destination))


def install_snapshot(options, apk: Path):
    if not (16 <= options.width <= 4096 and 16 <= options.height <= 4096 and math.isfinite(options.density) and .5 <= options.density <= 4):
        raise ValueError("width/height must be 16..4096 pixels and density .5..4")
    store = options.runtime_store.resolve(strict=True)
    selection = json.loads((store / "default.json").read_text())
    build_id = selection.get("build_id")
    if not isinstance(build_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", build_id): raise ValueError("select a valid default Runtime first")
    runtime = store / build_id
    metadata, icon = apk_metadata(apk)
    display_name = (options.name or metadata["label"]).strip()
    name = re.sub(r"[/\\:\x00-\x1f]", "-", display_name).strip(" .")[:160]
    if not name: raise ValueError("application name is empty")
    package_digest = hashlib.sha256(metadata["package"].encode()).hexdigest()[:12]
    normalized = re.sub("[^a-z0-9.-]", "-", metadata["package"].lower())[:120]
    bundle_id = f"org.husk.apk.{normalized}.{package_digest}"
    applications = options.applications.expanduser().resolve()
    applications.mkdir(parents=True, exist_ok=True)
    destination = applications / (name + ".app")
    if os.path.lexists(destination): raise FileExistsError(f"application exists; no files were replaced: {destination}")
    for existing in applications.glob("*.app"):
        info = existing / "Contents/Info.plist"
        if info.is_file() and plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") == bundle_id:
            raise FileExistsError(f"this package is already installed: {existing}")
    if not icon and not options.icon:
        raise ValueError("APK has no supported raster launcher icon; provide --icon PNG rather than inventing an icon")
    raw_icon = options.icon.resolve(strict=True).read_bytes() if options.icon else icon[4]
    if not raw_icon.startswith(b"\x89PNG\r\n\x1a\n"): raise ValueError("launcher icon must be PNG")
    with version_lease(runtime):
        validate_runtime(runtime)
        host = ROOT / "build/macos-host/HuskAppHost"
        run(["bash", str(ROOT / "scripts/build_macos_app_host.sh"), str(host)])
        staging_parent = Path(tempfile.mkdtemp(prefix=".husk-install-", dir=applications))
        staging = staging_parent / destination.name
        try:
            resources = staging / "Contents/Resources"
            package_root = resources / "package"
            package_root.mkdir(parents=True)
            executable = staging / "Contents/MacOS/HuskAppHost"
            executable.parent.mkdir()
            shutil.copy2(host, executable)
            package_hash = sha256(apk)
            apk_file = package_hash + ".apk"
            shutil.copyfile(apk, package_root / apk_file)
            if sha256(package_root / apk_file) != package_hash: raise ValueError("APK changed during installation")
            make_icon(resources, raw_icon)
            config = {"schema": 1, "bundle_id": bundle_id, "apk_file": apk_file,
                      "apk_sha256": package_hash, "runtime_store": str(store), "display_name": display_name,
                      "width": options.width, "height": options.height, "density": options.density,
                      "android_package": metadata["package"], "android_launcher": metadata["launcher"],
                      "android_version_name": metadata["version_name"], "android_version_code": metadata["version_code"],
                      "icon_resource": icon[3] if icon and not options.icon else "user-supplied PNG"}
            (resources / "husk-app.json").write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            version = re.match(r"\d+(?:\.\d+){0,2}", metadata["version_name"])
            info = {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "HuskAppHost",
                    "CFBundleName": display_name, "CFBundleDisplayName": display_name,
                    "CFBundlePackageType": "APPL", "CFBundleInfoDictionaryVersion": "6.0",
                    "CFBundleShortVersionString": version.group() if version else "1.0",
                    "CFBundleVersion": metadata["version_code"] if metadata["version_code"].isdigit() else "1",
                    "CFBundleIconFile": "AppIcon.icns", "NSPrincipalClass": "NSApplication",
                    "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0",
                    "LSApplicationCategoryType": "public.app-category.utilities"}
            (staging / "Contents/Info.plist").write_bytes(plistlib.dumps(info, sort_keys=True))
            shutil.copy2(ROOT / "LICENSE", resources / "LICENSE-Husk")
            run(["codesign", "--force", "--sign", "-", "--timestamp=none", "--identifier", bundle_id, str(staging)])
            run(["codesign", "--verify", "--deep", "--strict", str(staging)])
            for path in staging.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream: os.fsync(stream.fileno())
            sync_directory(staging)
            publish_exclusive(staging, destination)
            sync_directory(applications)
        finally:
            shutil.rmtree(staging_parent)
    print(json.dumps({"ok": True, "application": str(destination), **config}, ensure_ascii=False, indent=2))


def install(options):
    source = options.apk.resolve(strict=True)
    if not source.is_file(): raise ValueError("APK must be a regular file")
    applications = options.applications.expanduser().resolve()
    applications.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(applications / ".husk-install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unsafe application installation lock")
        # Serialize the identity check and publication, including installations
        # with different names but the same Android package identity.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix="husk-apk-snapshot-") as temporary:
            snapshot = Path(temporary) / "input.apk"
            shutil.copyfile(source, snapshot)
            # Metadata, icon and published APK all come from this same snapshot;
            # a download manager changing the original cannot mix generations.
            install_snapshot(options, snapshot)
    finally:
        os.close(descriptor)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("install", help="create a new independent .app without modifying the source APK")
    command.add_argument("apk", type=Path)
    command.add_argument("--runtime-store", type=Path, default=DEFAULT_STORE)
    command.add_argument("--applications", type=Path, default=DEFAULT_APPLICATIONS)
    command.add_argument("--name")
    command.add_argument("--icon", type=Path)
    command.add_argument("--width", type=int, default=720)
    command.add_argument("--height", type=int, default=1280)
    command.add_argument("--density", type=float, default=2)
    command = commands.add_parser("select", help="verify and atomically select a complete macos-app Runtime")
    command.add_argument("runtime", type=Path)
    command.add_argument("--store", type=Path)
    command = commands.add_parser("settings", help="save an explicit manual JIT value; applies on next process launch")
    command.add_argument("--store", type=Path, default=DEFAULT_STORE)
    command.add_argument("--jit-mib", type=int, required=True)
    command = commands.add_parser("list", help="list the generated applications")
    command.add_argument("--applications", type=Path, default=DEFAULT_APPLICATIONS)
    return parser.parse_args()


def main():
    options = arguments()
    if sys.platform != "darwin" or platform.machine() != "arm64": raise ValueError("requires native Apple Silicon macOS")
    if options.command == "install": install(options)
    elif options.command == "select":
        runtime = options.runtime.resolve(strict=True)
        store = options.store.resolve(strict=True) if options.store else runtime.parent
        if runtime.parent != store: raise ValueError("selected Runtime must be an immediate version directory of its store")
        with version_lease(runtime):
            validate_runtime(runtime)
            atomic_json(store / "default.json", {"schema": 1, "build_id": runtime.name})
        print(json.dumps({"ok": True, "store": str(store), "build_id": runtime.name}))
    elif options.command == "settings":
        if not 2 <= options.jit_mib <= 1024: raise ValueError("manual JIT must be 2..1024 MiB")
        options.store.mkdir(parents=True, exist_ok=True)
        store = options.store.resolve(strict=True)
        atomic_json(store / "settings.json", {"schema": 1, "jit_megabytes": options.jit_mib})
        print(json.dumps({"ok": True, "store": str(store), "jit_megabytes": options.jit_mib}))
    else:
        rows = []
        for app in sorted(options.applications.expanduser().glob("*.app")):
            config = app / "Contents/Resources/husk-app.json"
            if config.is_file(): rows.append({"application": str(app.resolve()), **json.loads(config.read_text())})
        print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, IndexError, struct.error, UnicodeError, BadZipFile, ET.ParseError, subprocess.CalledProcessError) as issue:
        print(f"Husk installer: {issue}", file=sys.stderr)
        if isinstance(issue, subprocess.CalledProcessError) and issue.output: print(issue.output, file=sys.stderr)
        raise SystemExit(1)
