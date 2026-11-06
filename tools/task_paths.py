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
import stat

TEMP_ROOT = Path("/Users/m1p0/Developer/temp")
TASK_PARENT = TEMP_ROOT / "codex/Husk"
_task: Path | None = None


def _check_directory_at(parent: int, name: str, path: Path) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"task temporary directory must be a real directory, not a symlink or file: {path}")


def _private_directory_at(parent: int, name: str, path: Path) -> int:
    _check_directory_at(parent, name, path)
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    # The descriptor-relative open also refuses a symlink introduced between
    # the check and mkdir/open. Never create cache children through a link.
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        os.fchmod(descriptor, 0o700)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def task_directory(label: str = "macos") -> Path:
    global _task
    if _task is not None:
        candidate = _task
    else:
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
    scratch = candidate / "tmp"
    pycache = scratch / "pycache"
    cocoa_home = scratch / "cocoa-home"
    root_fd = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(root_fd, 0o700)
        scratch_fd = _private_directory_at(root_fd, "tmp", scratch)
        try:
            cache_names = ("pycache", "cocoa-home", "cache", "clang-module-cache", "swift-module-cache")
            for name in cache_names:
                _check_directory_at(scratch_fd, name, scratch / name)
            for name in cache_names:
                descriptor = _private_directory_at(scratch_fd, name, scratch / name)
                os.close(descriptor)
        finally:
            os.close(scratch_fd)
    finally:
        os.close(root_fd)
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
