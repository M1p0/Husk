#!/usr/bin/env python3
"""Build a real native-runtime dylib and publish a fixed macOS Runtime version.

This M1 package provides the headless/context-bootstrap profile. It contains the
current native-runtime code and its available Java/system resources, but does
not advertise APK launch, a graphics backend, OS sandboxing, or hard memory
limits. A later host supplies those capabilities after separate validation.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
TARGET = "macos-arm64"
LIBRARY = "lib/libHuskRuntime.dylib"
PROFILE = "headless-bootstrap"
FRAMEWORKS = (
    "AudioToolbox", "Foundation", "CoreFoundation", "CoreText", "CoreGraphics",
    "Security", "QuartzCore", "Metal", "IOSurface", "ImageIO", "WebKit",
    "AppKit", "VideoToolbox", "CoreMedia", "CoreVideo", "Accelerate",
)
BOOT_FILES = (
    "com.android.art/javalib/core-oj.jar",
    "com.android.art/javalib/core-libart.jar",
    "com.android.art/javalib/okhttp.jar",
    "com.android.art/javalib/bouncycastle.jar",
    "com.android.art/javalib/apache-xml.jar",
    "com.android.i18n/javalib/core-icu4j.jar",
    "com.android.conscrypt/javalib/conscrypt.jar",
    "org.apache.http.legacy.jar", "husk-framework.dex", "framework-res.apk",
)


def run(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files() -> list[Path]:
    native = ROOT / "src/translation-layer-next"
    sources = sorted([*native.glob("*.c"), *native.glob("*.m")])
    sources = [path for path in sources if path.name != "husk-tl-unity-app.c"]
    old = ROOT / "src/translation-layer"
    sources += [old / name for name in
                ("husk-tl-elf.c", "husk-tl-zip.c", "husk-tl-sound.c")]
    sources.append(ROOT / "src/runtime/husk-runtime-v1.c")
    for path in sources:
        if not path.is_file():
            raise ValueError(f"missing Runtime source: {path.relative_to(ROOT)}")
    return sources


def source_snapshot() -> tuple[list[Path], tuple[Path, ...], str]:
    """Capture the exact source list plus headers/includes and build scripts.

    Re-scan after compilation so added input files invalidate the build too.
    This guards normal concurrent edits; it is not a substitute for compiling
    an adversarially mutable source tree in an isolated checkout.
    """
    sources = source_files()
    inputs = set([*sources, Path(__file__).resolve(),
                  ROOT / "scripts/build_macos_runtime.sh"])
    for directory in (ROOT / "src/translation-layer", ROOT / "src/translation-layer-next",
                      ROOT / "src/runtime", ROOT / "src/runtime-api"):
        inputs.update(directory.rglob("*.h"))
        inputs.update(directory.rglob("*.inc"))
    paths = tuple(sorted(inputs))
    digest = hashlib.sha256()
    for path in paths:
        content = path.read_bytes()
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return sources, paths, digest.hexdigest()


def validate_resource_tree(source: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"resource directory missing: {source}")
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise ValueError(f"resource symlinks are not accepted: {entry}")
        if not entry.is_dir() and not entry.is_file():
            raise ValueError(f"non-regular resource is not accepted: {entry}")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_readonly(directory: Path) -> None:
    for entry in directory.rglob("*"):
        entry.chmod(0o555 if entry.is_dir() or entry.suffix == ".dylib" else 0o444)
    directory.chmod(0o555)


def discard_staging(directory: Path) -> None:
    if not directory.exists():
        return
    directory.chmod(0o700)
    for entry in directory.rglob("*"):
        entry.chmod(0o700 if entry.is_dir() else 0o600)
    shutil.rmtree(directory)


def publish_exclusive(source: Path, destination: Path) -> None:
    # RENAME_EXCL is defined in the Darwin SDK's sys/stdio.h. Unlike a plain
    # rename(), it cannot replace an existing version, including an empty one.
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    rename_exclusive = libc.renamex_np
    rename_exclusive.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename_exclusive.restype = ctypes.c_int
    if rename_exclusive(os.fsencode(source), os.fsencode(destination), 0x00000004):
        problem = ctypes.get_errno()
        raise OSError(problem, os.strerror(problem), str(destination))


def build_library(staging: Path, build_id: str, sources: list[Path]) -> None:
    destination = staging / LIBRARY
    destination.parent.mkdir(parents=True)
    flags = ["xcrun", "--sdk", "macosx", "clang", "-arch", "arm64",
             "-dynamiclib", "-g", "-O1", "-fobjc-arc", "-Wall",
             "-Wno-unused-function", "-Wno-deprecated-declarations",
             "-I" + str(ROOT / "src/translation-layer"),
             "-I" + str(ROOT / "src/translation-layer-next"),
             "-I" + str(ROOT / "src/runtime-api"),
             f'-DHUSK_RUNTIME_BUILD_ID="{build_id}"',
             "-Wl,-install_name,@rpath/libHuskRuntime.dylib",
             "-Wl,-current_version,1.0", "-Wl,-compatibility_version,1.0"]
    flags += [str(path) for path in sources]
    flags += ["-lz", "-lm", "-lresolv", "-lsqlite3"]
    for framework in FRAMEWORKS:
        flags += ["-framework", framework]
    flags += ["-o", str(destination)]
    # No dynamic_lookup or permitted undefined host symbols: missing bridges
    # must fail the link rather than depend on a monolithic test executable.
    run(flags)
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(destination)])
    run(["codesign", "--verify", "--strict", str(destination)])
    architectures = run(["xcrun", "lipo", "-archs", str(destination)], capture=True)
    if architectures != "arm64":
        raise ValueError(f"wrong Runtime architecture: {architectures}")
    version_commands = run(["xcrun", "vtool", "-show-build", str(destination)], capture=True)
    if not re.search(r"\bplatform\s+MACOS\b", version_commands):
        raise ValueError("Runtime must target macOS, not iOS or Simulator")
    dependencies = run(["xcrun", "otool", "-L", str(destination)], capture=True)
    for line in dependencies.splitlines()[1:]:
        dependency = line.strip().split(" (", 1)[0]
        if dependency == "@rpath/libHuskRuntime.dylib":
            continue
        if not dependency.startswith(("/System/Library/", "/usr/lib/")):
            raise ValueError(f"unpackaged host dependency: {dependency}")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=ROOT / "build/macos-runtime",
                        help="version repository; default: build/macos-runtime")
    parser.add_argument("--build-id", help="new immutable version ID; existing IDs are never overwritten")
    parser.add_argument("--java-runtime", type=Path, default=ROOT / "build/java-runtime",
                        help="complete Java guest resource tree; default: build/java-runtime")
    parser.add_argument("--android-system", type=Path, default=ROOT / "build/android-system",
                        help="Android certificates/fonts tree; default: build/android-system")
    return parser.parse_args()


def main() -> int:
    options = arguments()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("this build target requires native Apple Silicon macOS")
    revision = run(["git", "rev-parse", "HEAD"], capture=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    build_id = options.build_id or f"m1-{revision[:12]}-{stamp}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", build_id):
        raise ValueError("build ID must be 1-96 ASCII letters/digits/dot/underscore/hyphen, starting with a letter/digit")
    public_header = ROOT / "src/runtime-api/husk-runtime-v1.h"
    if not public_header.is_file():
        raise ValueError("missing public Runtime ABI header")
    sources, source_inputs, source_digest = source_snapshot()
    java = options.java_runtime.resolve(strict=True)
    system = options.android_system.resolve(strict=True)
    validate_resource_tree(java)
    validate_resource_tree(system)
    for relative in BOOT_FILES:
        if not (java / relative).is_file():
            raise ValueError(f"incomplete Java Runtime: missing {relative}")
    certificate = ROOT / "src/app/Husk/Resources/cacert.pem"
    if not certificate.is_file() or certificate.is_symlink():
        raise ValueError("missing regular cacert.pem resource")

    options.store.mkdir(parents=True, exist_ok=True)
    store = options.store.resolve(strict=True)
    if store.is_relative_to(java) or store.is_relative_to(system):
        raise ValueError("Runtime store must be outside resource input trees")
    version = store / build_id
    if os.path.lexists(version):
        raise FileExistsError(errno.EEXIST, "Runtime version already exists", str(version))
    leases = store / ".leases"
    if leases.is_symlink():
        raise ValueError("lease directory must not be a symlink")
    leases.mkdir(exist_ok=True)
    lease = leases / (build_id + ".lock")
    if os.path.lexists(lease):
        raise FileExistsError(errno.EEXIST, "version lease already exists; choose a new build ID", str(lease))
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{build_id}-", dir=store))
    try:
        print(f"Building {build_id}: {len(sources)} real Runtime source files", flush=True)
        build_library(staging, build_id, sources)
        resource_root = staging / "resources"
        resource_root.mkdir()
        shutil.copytree(java, resource_root / "java-runtime")
        shutil.copytree(system, resource_root / "android-system")
        shutil.copy2(certificate, resource_root / "cacert.pem")
        shutil.copy2(ROOT / "LICENSE", resource_root / "LICENSE-Husk")
        (staging / "include").mkdir()
        shutil.copy2(public_header, staging / "include/husk-runtime-v1.h")
        artifacts = {}
        for path in sorted(staging.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"published artifacts cannot be symlinks: {path}")
            if path.is_file():
                artifacts[path.relative_to(staging).as_posix()] = digest_file(path)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        final_sources, final_inputs, final_digest = source_snapshot()
        if (final_sources != sources or final_inputs != source_inputs
                or final_digest != source_digest):
            raise ValueError("Runtime source inputs changed during the build; no version was published")
        manifest = {
            "schema": 1, "abi_version": 1, "build_id": build_id,
            "host_target": TARGET, "profile": PROFILE,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_revision": revision,
            "source_tree_sha256": source_digest,
            "native_sources": [path.relative_to(ROOT).as_posix() for path in sources],
            "features": {"native_core": "included", "context_bootstrap": "supported",
                         "apk_execution": "unsupported", "graphics_backend": "unsupported",
                         "system_memory_limit": "unsupported", "sandbox_isolation": "unverified"},
            "artifacts": artifacts,
        }
        with (staging / "manifest.json").open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        make_readonly(staging)
        sync_directory(staging)
        descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o444)
        try:
            os.write(descriptor, (build_id + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        sync_directory(leases)
        publish_exclusive(staging, version)
        sync_directory(store)
        print(f"Published: {version}", flush=True)
        print(f"Profile: {PROFILE}; {len(artifacts)} hashed artifacts; lease: {lease}", flush=True)
        return 0
    finally:
        # Never remove a published version or a lease file: a failed publisher
        # may leave an unused lock, but replacing it could split future holders.
        discard_staging(staging)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Runtime package failed: {error}", file=sys.stderr)
        raise SystemExit(1)
