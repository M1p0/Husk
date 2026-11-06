"""Keep Codex build intermediates and test evidence in the user's task directory.

Final source files, installed .apps and immutable Runtime versions remain in the
project. Temporary staging must share their filesystem for atomic publication.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import os
from pathlib import Path
import re
import secrets

TEMP_ROOT = Path("/Users/m1p0/Developer/temp")
TASK_PARENT = TEMP_ROOT / "codex/Husk"
_task: Path | None = None


def task_directory(label: str = "macos") -> Path:
    global _task
    if _task is not None:
        return _task
    supplied = os.environ.get("HUSK_TASK_TMPDIR")
    if supplied:
        candidate = Path(supplied).expanduser()
        if not candidate.is_absolute():
            raise ValueError("HUSK_TASK_TMPDIR must be an absolute task directory")
    else:
        safe_label = re.sub(r"[^A-Za-z0-9_-]", "-", label)[:24] or "macos"
        candidate = TASK_PARENT / (safe_label + "-" + secrets.token_hex(4))
    candidate = candidate.resolve()
    parent = TASK_PARENT.resolve()
    if candidate.parent != parent or candidate == parent:
        raise ValueError(f"task directory must be a child of {TASK_PARENT}")
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate.chmod(0o700)
    scratch = candidate / "tmp"
    scratch.mkdir(exist_ok=True, mode=0o700)
    pycache = scratch / "pycache"
    pycache.mkdir(exist_ok=True, mode=0o700)
    cocoa_home = scratch / "cocoa-home"
    cocoa_home.mkdir(exist_ok=True, mode=0o700)
    os.environ.update(HUSK_TASK_TMPDIR=str(candidate), TMPDIR=str(scratch),
                      TMP=str(scratch), TEMP=str(scratch),
                      PYTHONDONTWRITEBYTECODE="1", PYTHONPYCACHEPREFIX=str(pycache),
                      CFFIXED_USER_HOME=str(cocoa_home), XDG_CACHE_HOME=str(scratch / "cache"),
                      CLANG_MODULE_CACHE_PATH=str(scratch / "clang-module-cache"),
                      SWIFT_MODULECACHE_PATH=str(scratch / "swift-module-cache"))
    sys.pycache_prefix = str(pycache)
    _task = candidate
    return candidate


def temporary_path(path: Path | str) -> Path:
    target = Path(path).expanduser().resolve()
    task = task_directory()
    if not target.is_relative_to(task) or target == task:
        raise ValueError(f"temporary output must be inside the current task directory: {task}")
    return target


def staging_directory(destination: Path | str) -> Path:
    """Return a permitted staging parent, refusing a non-atomic cross-volume move."""
    parent = Path(destination).resolve()
    while not parent.exists():
        parent = parent.parent
    scratch = task_directory() / "tmp"
    if os.stat(parent).st_dev != os.stat(scratch).st_dev:
        raise ValueError("the required task temp directory and delivery destination are on different volumes; atomic publication cannot proceed")
    return scratch


if __name__ == "__main__":
    print(task_directory(sys.argv[1] if len(sys.argv) > 1 else "macos"))
