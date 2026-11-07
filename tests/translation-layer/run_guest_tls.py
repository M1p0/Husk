#!/usr/bin/env python3
"""Build tiny arm64 ELF fixtures with Apple's assembler, then execute native loader regressions."""
import argparse
import os
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "tools"))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, help="Evidence directory inside the current task directory")
parser.add_argument("--fixtures-only", action="store_true", help="Generate ELF fixtures without building or running the harness")
parser.add_argument("--code-views", action="store_true", help="Exercise original-code reads with code/data on separate hardware pages")
args = parser.parse_args()
TASK = task_directory("guest-tls-test")
OUT = temporary_path(args.out if args.out is not None else TASK / "evidence" / "guest-tls-test")
TASK_TEMP = TASK / "tmp"
temporary_path(TASK_TEMP)
TASK_TEMP.mkdir(parents=True, exist_ok=True)
CHILD_ENV = dict(os.environ, TMPDIR=str(TASK_TEMP), TMP=str(TASK_TEMP), TEMP=str(TASK_TEMP), TL_TMP=str(TASK_TEMP), HUSK_TASK_TMPDIR=str(TASK))
if args.code_views:
    CHILD_ENV.update(TL_ORIGINAL_CODE_VIEW="1", TL_CODE_VIEWS_TEST="1")
text_base, data_base = (0x4000, 0x8000) if args.code_views else (0x1000, 0x3000)
OUT.mkdir(parents=True, exist_ok=True)
assembly = [".text", ".p2align 2"]
for register in [0, 16, 17, 18, 28]:
    assembly += [f".globl _tp{register}", f"_tp{register}:", "stp x28, x30, [sp, #-16]!",
                 f"mrs x{register}, tpidr_el0", f"mov x0, x{register}", "ldp x28, x30, [sp], #16", "ret"]
assembly += [".globl _tpflags", "_tpflags:", "cmp x0, x0", "mrs x0, tpidr_el0", "csel x0, x0, xzr, eq", "ret",
             ".globl _write_g", "_write_g:", "mrs x8, tpidr_el0", "str x0, [x8, #16]", "ret",
             ".globl _read_g", "_read_g:", "mrs x0, tpidr_el0", "ldr x0, [x0, #16]", "ret",
             ".globl _read_tlsdesc", "_read_tlsdesc:", "stp x29, x30, [sp, #-16]!", "adr x0, _descriptor_placeholder",
             "ldr x1, [x0]", "blr x1", "mrs x1, tpidr_el0", "add x0, x1, x0", "ldr x0, [x0]", "ldp x29, x30, [sp], #16", "ret",
             ".globl _write_tlsdesc", "_write_tlsdesc:", "stp x19, x30, [sp, #-16]!", "mov x19, x0", "adr x0, _descriptor_placeholder",
             "ldr x1, [x0]", "blr x1", "mrs x1, tpidr_el0", "add x0, x1, x0", "str x19, [x0]", "ldp x19, x30, [sp], #16", "ret",
             ".globl _check_regs", "_check_regs:", "stp x19, x28, [sp, #-16]!", "stp x29, x30, [sp, #-16]!",
             "str q8, [sp, #-16]!", "mov x19, x0"]
assembly += [f"mov x{n}, #{0x100 + n}" for n in range(18)]
assembly += ["mov x28, #0x128", "movi v0.16b, #0x5a", "movi v8.16b, #0xa5", "movi v31.16b, #0x3c",
             "cmp x0, x0", "mrs x17, tpidr_el0"]
assembly += [f"str x{n}, [x19, #{n * 8}]" for n in range(18)]
assembly += ["str x28, [x19, #224]", "mrs x0, nzcv", "str x0, [x19, #240]", "str q0, [x19, #256]",
             "str q8, [x19, #272]", "str q31, [x19, #288]", "ldr q8, [sp], #16", "ldp x29, x30, [sp], #16",
             "ldp x19, x28, [sp], #16", "ret",
             ".globl _small_stack_syscall", "_small_stack_syscall:", "stp x19, x30, [sp, #-16]!", "mov x19, sp",
             "mov sp, x0", "mov x0, #0", "mov x8, #261", "svc #0", "mov sp, x19", "ldp x19, x30, [sp], #16", "ret", "_descriptor_placeholder:", ".space 16"]
assembly += [".globl _literal_mrs", "_literal_mrs:", "ldr w0, 1f", "ret", "1:", ".long 0xd53bd040",
             ".globl _literal_svc", "_literal_svc:", "ldr w0, 2f", "ret", "2:", ".long 0xd4000001"]
assembly += [".globl _original_code_address", "_original_code_address:", "adr x0, _descriptor_placeholder",
             f"sub x0, x0, #{(data_base - text_base) // 4096}, lsl #12", "add x0, x0, #4", "ret",
             ".globl _data_address", "_data_address:", "adr x0, _descriptor_placeholder", "ret",
             ".globl _read_value", "_read_value:", "adr x0, _descriptor_placeholder", "ldr x0, [x0, #32]", "ret",
             ".globl _write_value", "_write_value:", "adr x1, _descriptor_placeholder", "str x0, [x1, #32]", "ret"]
assembly += [".globl _read_adr_code", "_read_adr_code:", "adr x1, _tp0", "ldr w0, [x1, #4]", "ret",
             ".globl _read_adrp_code", "_read_adrp_code:", "adrp x1, _tp0@PAGE", "add x1, x1, #4", "ldr w0, [x1]", "ret",
             ".globl _direct_code_address", "_direct_code_address:", "adr x0, _tp0", "ret",
             ".globl _page_code_address", "_page_code_address:", "adrp x0, _tp0@PAGE", "add x0, x0, #0", "ret"]
assembly += [".globl _read_literal_code", "_read_literal_code:",
             "ldr w0, _tp0+4" if args.code_views else "ldr w0, _literal_constant", "ret",
             "_literal_constant:", ".long 0xd53bd040"]
assembly += [".globl _read_indexed_code", "_read_indexed_code:", "mov w2, #1", "adr x10, _tp0", "add x11, x10, w2, uxtw #2",
             "nop", "nop", "nop", "nop", "nop", "nop", "nop", "nop", "ldr w0, [x11]", "ret"]
assembly += [".globl _literal_data", "_literal_data:", "ldr w0, _descriptor_placeholder", "ret"]
(OUT / "fixture.s").write_text("\n".join(assembly) + "\n")
subprocess.run(["clang", "-arch", "arm64", "-c", OUT / "fixture.s", "-o", OUT / "fixture.o"], check=True, env=CHILD_ENV)
obj = (OUT / "fixture.o").read_bytes()
ncommands = struct.unpack_from("<I", obj, 16)[0]
offset, symbols, text = 32, None, None
for _ in range(ncommands):
    command, size = struct.unpack_from("<II", obj, offset)
    if command == 0x19:
        count = struct.unpack_from("<I", obj, offset + 64)[0]
        for n in range(count):
            section = offset + 72 + n * 80
            if obj[section:section + 16].rstrip(b"\0") == b"__text":
                address, length, fileoff = struct.unpack_from("<QQI", obj, section + 32)
                text = obj[fileoff:fileoff + length]
    if command == 2:
        symbols = struct.unpack_from("<IIII", obj, offset + 8)
    offset += size
assert text is not None and symbols is not None
symoff, nsyms, stroff, strsize = symbols
exports = []
for n in range(nsyms):
    string, kind, section, descriptor, value = struct.unpack_from("<IBBHQ", obj, symoff + n * 16)
    name = obj[stroff + string:obj.index(b"\0", stroff + string)].decode()
    if kind & 1 and name.startswith("_"):
        exports.append((name[1:], value))
# ADR sites name the writable descriptor, not its assembler placeholder.
text = bytearray(text)
for name, delta in exports:
    if name in {"read_tlsdesc", "write_tlsdesc", "original_code_address", "data_address", "read_value", "write_value"}:
        site = delta + (4 if name == "read_tlsdesc" else 8 if name == "write_tlsdesc" else 0)
        distance = data_base - (text_base + site)
        rd = 1 if name == "write_value" else 0
        struct.pack_into("<I", text, site, 0x10000000 | ((distance & 3) << 29) | (((distance >> 2) & 0x7ffff) << 5) | rd)
for name, delta in exports:
    if name == "read_literal_code" and args.code_views:
        distance = 4 - delta
        struct.pack_into("<I", text, delta, 0x18000000 | (((distance // 4) & 0x7ffff) << 5))
    if name == "read_indexed_code":
        site = delta + 4; distance = -site
        struct.pack_into("<I", text, site, 0x10000000 | ((distance & 3) << 29) | (((distance >> 2) & 0x7ffff) << 5) | 10)
    if name in {"read_adr_code", "direct_code_address"}:
        distance = -delta; rd = 1 if name == "read_adr_code" else 0
        struct.pack_into("<I", text, delta, 0x10000000 | ((distance & 3) << 29) | (((distance >> 2) & 0x7ffff) << 5) | rd)
    if name in {"read_adrp_code", "page_code_address"}:
        pages = (text_base >> 12) - ((text_base + delta) >> 12); rd = 1 if name == "read_adrp_code" else 0
        struct.pack_into("<I", text, delta, 0x90000000 | ((pages & 3) << 29) | (((pages >> 2) & 0x7ffff) << 5) | rd)
for name, delta in exports:
    if name == "literal_data":
        distance = data_base + 32 - (text_base + delta)
        struct.pack_into("<I", text, delta, 0x18000000 | (((distance // 4) & 0x7ffff) << 5))
for filename in ["libtls-one.so", "libtls-two.so"]:
    image = bytearray(data_base + (0x4000 if args.code_views else 0x1000))
    image[:64] = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9, 3, 183, 1, 0, 64, 0x2800, 0, 64, 56, 4, 64, 7, 5)
    image[64:120] = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, data_base, data_base, 0x1000)
    image[120:176] = struct.pack("<IIQQQQQQ", 2, 4, 0x200, 0x200, 0x200, 16 * 9, 16 * 9, 8)
    image[176:232] = struct.pack("<IIQQQQQQ", 1, 6, data_base, data_base, data_base, 0x1000, 0x1000, 0x1000)
    image[232:288] = struct.pack("<IIQQQQQQ", 7, 4, 0x2200, 0x2200, 0x2200, 8, 8, 8)
    strings = b"\0"; symtab = bytes(24)
    for name, value in exports:
        symtab += struct.pack("<IBBHQQ", len(strings), 0x12, 0, 1, text_base + value, 0)
        strings += name.encode() + b"\0"
    image[0x400:0x400 + len(strings)] = strings
    image[0x600:0x600 + len(symtab)] = symtab
    hashes = [1, len(exports) + 1, 1, 0] + list(range(2, len(exports) + 1)) + [0]
    image[0x900:0x900 + 4 * len(hashes)] = struct.pack("<" + "I" * len(hashes), *hashes)
    struct.pack_into("<QQq", image, 0xa00, data_base, 1031, 0)
    struct.pack_into("<Q", image, 0x2200, 0x12345678)
    dynamic = [(7, 0xa00), (8, 24), (9, 24), (4, 0x900), (5, 0x400), (6, 0x600), (10, len(strings)), (11, 24), (0, 0)]
    for n, pair in enumerate(dynamic): struct.pack_into("<qQ", image, 0x200 + n * 16, *pair)
    struct.pack_into("<I", image, data_base + 32, 0x1234cafe)
    image[text_base:text_base + len(text)] = text
    names = b"\0.text\0.dynstr\0.dynsym\0.hash\0.shstrtab\0.tdata\0"
    image[0x2600:0x2600 + len(names)] = names
    sections = [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0), (1, 1, 6, text_base, text_base, len(text), 0, 0, 4, 0),
                (7, 3, 2, 0x400, 0x400, len(strings), 0, 0, 1, 0), (15, 11, 2, 0x600, 0x600, len(symtab), 2, 1, 8, 24),
                (23, 5, 2, 0x900, 0x900, 4 * len(hashes), 3, 0, 4, 4), (29, 3, 0, 0, 0x2600, len(names), 0, 0, 1, 0), (39, 1, 0x402, 0x2200, 0x2200, 8, 0, 0, 8, 0)]
    for n, row in enumerate(sections): struct.pack_into("<IIQQQQIIQQ", image, 0x2800 + n * 64, *row)
    (OUT / filename).write_bytes(image)
if not args.fixtures_only:
    with (OUT / "build.log").open("w") as log:
        subprocess.run(["bash", ROOT / "tools/regress/build.sh", "tests/translation-layer/guest_tls.c", OUT / "guest-tls-test"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, env=CHILD_ENV)
    with (OUT / "results.log").open("w") as log:
        result = subprocess.run([OUT / "guest-tls-test", OUT / "libtls-one.so", OUT / "libtls-two.so"],
                                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=CHILD_ENV, timeout=30)
        log.write(result.stdout)
        print(result.stdout, end="")
        result.check_returncode()
    print(OUT / "results.log")
