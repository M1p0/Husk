#!/usr/bin/env python3
"""Verify guest /proc mappings against an independently linked ELF's PT_LOADs."""
import argparse
import ctypes
import os
from pathlib import Path
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--linker", required=True, type=Path, help="ELF ld.lld executable")
    args = parser.parse_args()
    task = task_directory("proc-maps-test")
    work = task / "tmp/proc-maps fixture"
    work.mkdir(parents=True, exist_ok=True)
    source = work / "fixture.c"
    source.write_text('volatile int fixture_value = 42;\n'
                      'const char fixture_name[] = "proc-maps-fixture";\n'
                      'int fixture_entry(void) { return fixture_value; }\n')
    subprocess.run(["clang", "--target=aarch64-linux-android29", "-O1", "-fPIC",
                    "-c", str(source), "-o", str(work / "fixture.o")], check=True)
    fixture = work / "libproc-maps.so"
    subprocess.run([str(args.linker.resolve(strict=True)), "-shared", "--eh-frame-hdr",
                    "-z", "max-page-size=16384", "-soname", fixture.name,
                    str(work / "fixture.o"), "-o", str(fixture)], check=True)
    elf = fixture.read_bytes()
    offset = struct.unpack_from("<Q", elf, 32)[0]
    size, count = struct.unpack_from("<HH", elf, 54)
    segments = [struct.unpack_from("<IIQQQQQQ", elf, offset + i * size)
                for i in range(count)]
    segments = [p for p in segments if p[0] == 1 and p[6]]
    assert any((p[1] & 1) and p[3] != p[2] for p in segments), "fixture must separate file and executable addresses"

    library = args.runtime.resolve(strict=True)
    if library.is_dir():
        library /= "lib/libHuskRuntime.dylib"
    runtime = ctypes.CDLL(str(library))
    runtime.husk_jit_budget_set_target_bytes.argtypes = [ctypes.c_size_t]
    runtime.husk_jit_budget_set_target_bytes(512 << 20)
    runtime.tl_set_data_dir.argtypes = [ctypes.c_char_p]
    runtime.tl_set_data_dir(os.fsencode(work))
    runtime.tl_ld_load.argtypes = [ctypes.c_char_p]
    runtime.tl_ld_load.restype = ctypes.c_void_p
    runtime.tl_ld_lib_base.argtypes = [ctypes.c_void_p]
    runtime.tl_ld_lib_base.restype = ctypes.c_void_p
    runtime.tl_ld_maps.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    runtime.tl_ld_maps.restype = ctypes.c_size_t
    image = runtime.tl_ld_load(os.fsencode(fixture))
    assert image, "fixture did not load"
    base = runtime.tl_ld_lib_base(image)
    output = ctypes.create_string_buffer(65536)
    length = runtime.tl_ld_maps(output, len(output), os.fsencode(work))
    assert length < len(output)
    mappings = []
    for line in output.value.decode().splitlines():
        address, permissions, file_offset, device, inode, path = line.split(maxsplit=5)
        if path != str(fixture):
            continue
        start, end = (int(value, 16) for value in address.split("-"))
        mappings.append((start, end, permissions, int(file_offset, 16)))
    assert len(mappings) == len(segments), (mappings, segments)
    for segment in segments:
        _, flags, file_offset, address, _, file_size, memory_size, _ = segment
        matches = [m for m in mappings if m[0] <= base + address < m[1]]
        assert len(matches) == 1, (segment, matches)
        start, end, permissions, offset = matches[0]
        expected = ''.join(letter if flags & flag else '-'
                           for flag, letter in [(4, 'r'), (2, 'w'), (1, 'x')]) + 'p'
        assert permissions == expected, (segment, permissions, expected)
        assert end >= base + address + memory_size
        assert base + address - start == file_offset - offset, (segment, matches)
        if flags & 1:
            # An unwinder combines the mapping's file offset with the ELF load
            # bias; the result must identify the actual executable ELF address.
            pc = base + address + file_size // 2
            relative_pc = pc - start + offset + address - file_offset
            assert relative_pc == pc - base, (hex(relative_pc), hex(pc - base))
    ordered = sorted(mappings)
    assert all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:])), mappings
    print("PASS: read-only, executable and writable mappings match ELF segment permissions")
    print("PASS: mapping offsets preserve ELF-relative PCs and do not overlap")


if __name__ == "__main__":
    main()
