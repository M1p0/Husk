#!/usr/bin/env python3
"""Prepare Husk's existing Noto CJK font for the self-contained Mac Runtime."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, staging_directory, temporary_path
FILES = {
    "NotoSansCJK-Regular.ttc": (
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTC/NotoSansCJK-Regular.ttc",
        "b76b0433203017ca80401b2ee0dd69350349871c4b19d504c34dbdd80541690a"),
    "NotoSansCJK-OFL.txt": (
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/LICENSE",
        "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"),
}


def main():
    task_directory("macos-fonts")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fonts", type=Path, default=ROOT / "build/android-system/fonts")
    args = parser.parse_args()
    args.fonts.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in FILES.items():
        target = args.fonts / name
        if target.exists():
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError(f"existing font resource differs; preserve it and choose another --fonts directory: {target}")
            print(f"Verified: {target}")
            continue
        fd, temporary = tempfile.mkstemp(prefix="noto-", dir=staging_directory(args.fonts))
        try:
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as destination, urllib.request.urlopen(url, timeout=30) as source:
                while block := source.read(64 * 1024):
                    total += len(block)
                    if total > 32 * 1024 * 1024:
                        raise ValueError("font download exceeded expected size bound")
                    digest.update(block)
                    destination.write(block)
                destination.flush()
                os.fsync(destination.fileno())
            if digest.hexdigest() != expected:
                raise ValueError(f"upstream content changed; expected SHA256 {expected}: {url}")
            # Do not replace a concurrently supplied local font.
            os.link(temporary, target)
            print(f"Prepared: {target}")
        finally:
            temporary_path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
