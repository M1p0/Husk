#!/usr/bin/env python3
"""Build a self-contained native APK manager using the existing local Python.

The manager bundles the installation backend and prebuilt APK host. Installing
an APK needs no compiler or source checkout; the configured shared Runtime store
and the recorded Python interpreter must remain available.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, staging_directory, temporary_path
sys.path.insert(0, str(ROOT / "tools/macos"))
from install_apk import make_icon, publish_exclusive, sync_directory


def main():
    task_directory("apk-installer-build")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path.home() / "Applications/Husk APK Installer.app")
    parser.add_argument("--applications", type=Path, default=Path.home() / "Applications/Husk Apps")
    parser.add_argument("--runtime-store", type=Path, default=ROOT / "build/macos-runtime")
    parser.add_argument("--register-apk", type=Path, help="register this installed manager as the default opener for APK files")
    options = parser.parse_args()
    destination = options.output.expanduser().absolute()
    if destination.suffix != ".app" or destination.exists():
        raise ValueError("choose a new .app destination; existing applications are never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="husk-manager-", dir=staging_directory(destination.parent)))
    try:
        app = stage / destination.name
        binary = app / "Contents/MacOS/HuskInstaller"
        resources = app / "Contents/Resources"
        binary.parent.mkdir(parents=True); resources.mkdir()
        subprocess.run(["xcrun", "--sdk", "macosx", "clang", "-arch", "arm64", "-mmacosx-version-min=12.0", "-fobjc-arc", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations", str(ROOT / "tools/macos/HuskInstaller.m"), "-framework", "AppKit", "-framework", "CoreServices", "-framework", "UniformTypeIdentifiers", "-o", str(binary)], check=True)
        subprocess.run(["bash", str(ROOT / "scripts/build_macos_app_host.sh"), str(resources / "HuskAppHost")], check=True)
        subprocess.run([str(binary), "--make-icon", str(resources / "DefaultAPK.png")], check=True)
        make_icon(resources, (resources / "DefaultAPK.png").read_bytes())
        backend = resources / "engine"
        for source in ["tools/task_paths.py", "tools/macos/install_apk.py", "LICENSE"]:
            target = backend / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / source, target)
        config = {"schema": 1, "python": str(Path(sys.executable).absolute()),
                  "runtime_store": str(options.runtime_store.expanduser().resolve(strict=True)),
                  "applications": str(options.applications.expanduser().absolute()),
                  "scan_roots": [str(ROOT / "build/macos-apps")]}
        (resources / "installer.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        uti = "org.husk.android-package"
        info = {"CFBundleIdentifier": "org.husk.apk-installer", "CFBundleExecutable": "HuskInstaller", "CFBundleName": "Husk APK Installer",
                "CFBundleDisplayName": "Husk APK 安装器", "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0",
                "CFBundleIconFile": "AppIcon.icns", "NSPrincipalClass": "NSApplication", "NSHighResolutionCapable": True,
                "LSMinimumSystemVersion": "12.0", "LSApplicationCategoryType": "public.app-category.utilities",
                "CFBundleDocumentTypes": [{"CFBundleTypeName": "Android APK", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Default",
                    "CFBundleTypeExtensions": ["apk"], "LSItemContentTypes": [uti, "com.android.package-archive"], "CFBundleTypeIconFile": "AppIcon.icns"}],
                "UTExportedTypeDeclarations": [{"UTTypeIdentifier": uti, "UTTypeDescription": "Android APK", "UTTypeConformsTo": ["public.zip-archive"],
                    "UTTypeTagSpecification": {"public.filename-extension": ["apk"], "public.mime-type": ["application/vnd.android.package-archive"]}}]}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        publish_exclusive(app, destination); sync_directory(destination.parent)
    finally:
        shutil.rmtree(temporary_path(stage))
    print(json.dumps({"application": str(destination), **config}, ensure_ascii=False, indent=2))
    if options.register_apk:
        subprocess.run([str(destination / "Contents/MacOS/HuskInstaller"), "--register-default", str(options.register_apk.resolve(strict=True))], check=True)


if __name__ == "__main__":
    main()
