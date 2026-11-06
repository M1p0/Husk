#!/usr/bin/env python3
"""Exercise proc-file metadata and reading through a real Runtime's guest ABI."""
import argparse
import ctypes
import os
from pathlib import Path
import stat
import struct
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path,
                        help="published Runtime directory or libHuskRuntime.dylib")
    args = parser.parse_args()
    task = task_directory("proc-cpuinfo-test")
    library = args.runtime.resolve(strict=True)
    if library.is_dir():
        library = library / "lib/libHuskRuntime.dylib"
    runtime = ctypes.CDLL(str(library))
    runtime.tl_bionic_find.argtypes = [ctypes.c_char_p]
    runtime.tl_bionic_find.restype = ctypes.c_void_p
    runtime.tl_set_data_dir.argtypes = [ctypes.c_char_p]
    runtime.tl_set_data_dir(os.fsencode(task / "tmp"))

    def symbol(name, result, *arguments):
        address = runtime.tl_bionic_find(name.encode())
        assert address, f"missing guest adapter: {name}"
        return ctypes.CFUNCTYPE(result, *arguments)(address)

    path_stat = {name: symbol(name, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
                 for name in ["stat", "lstat", "lstat64"]}
    at_stat = {name: symbol(name, ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                            ctypes.c_void_p, ctypes.c_int)
               for name in ["fstatat", "fstatat64"]}
    guest_open = symbol("open", ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint)
    guest_read = symbol("read", ctypes.c_long, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t)
    guest_close = symbol("close", ctypes.c_int, ctypes.c_int)

    def mode(function, path, *arguments):
        output = ctypes.create_string_buffer(128)
        result = function(path, output, *arguments)
        assert result == 0, f"metadata failed for {path!r}: {result}"
        return struct.unpack_from("<I", output.raw, 16)[0]

    for path in [b"/proc/cpuinfo", b"/proc/meminfo", b"/proc/self/status"]:
        for name, function in path_stat.items():
            assert stat.S_ISREG(mode(function, path)), (name, path)
        for name, function in at_stat.items():
            for flags in [0, 0x100]:  # Android AT_SYMLINK_NOFOLLOW
                assert stat.S_ISREG(mode(lambda p, out, f: function(-100, p, out, f),
                                         path, flags)), (name, path, flags)
    for name, function in path_stat.items():
        assert stat.S_ISDIR(mode(function, b"/proc")), name
    for name, function in at_stat.items():
        for flags in [0, 0x100]:
            assert stat.S_ISDIR(mode(lambda p, out, f: function(-100, p, out, f),
                                     b"/proc", flags)), name
    print("PASS: stat/lstat/lstat64 and both fstatat modes recognize regular proc files")
    print("PASS: /proc is a directory for FileStream's parent-directory check")

    descriptor = guest_open(b"/proc/cpuinfo", 0, 0)
    assert descriptor >= 0, "guest open(/proc/cpuinfo) failed"
    try:
        content = ctypes.create_string_buffer(32768)
        count = guest_read(descriptor, content, len(content))
        assert count > 0 and b"processor" in content.raw[:count]
        assert b"Features" in content.raw[:count]
    finally:
        assert guest_close(descriptor) == 0
    print("PASS: guest open/read receives CPU information")

    regular = task / "tmp/proc-metadata-regular.txt"
    link = task / "tmp/proc-metadata-link"
    regular.write_text("fixture\n")
    link.unlink(missing_ok=True)
    link.symlink_to(regular)
    try:
        assert stat.S_ISREG(mode(path_stat["stat"], os.fsencode(link)))
        assert stat.S_ISLNK(mode(path_stat["lstat"], os.fsencode(link)))
        assert stat.S_ISLNK(mode(path_stat["lstat"], b"/proc/self/exe"))
        for name, function in path_stat.items():
            output = ctypes.create_string_buffer(128)
            assert function(b"/proc/cpuinfo-does-not-exist", output) == -1, name
    finally:
        link.unlink()
        regular.unlink()
    print("PASS: host symlinks, proc/self/exe and missing-path behavior are preserved")


if __name__ == "__main__":
    main()
