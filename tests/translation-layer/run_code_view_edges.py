#!/usr/bin/env python3
"""Check explicit selection, unsupported mappings and >128 MiB stub fallbacks."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import struct
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--mode', choices=('disabled', 'far', 'mixed'), required=True)
args = parser.parse_args()
task_directory('code-view-edges')
out = temporary_path(args.out)
out.mkdir(parents=True, exist_ok=False)
names = ['tp', 'ctr', 'pid', 'read_code', 'code_address']
words = [0xd53bd040, 0xd65f03c0, 0xd53b0020, 0xd65f03c0,
         0xd2800008 | (172 << 5), 0xd4000001, 0xd65f03c0]
def adr(distance):
    return 0x10000000 | ((distance & 3) << 29) | (((distance >> 2) & 0x7ffff) << 5)
words += [adr(-len(words) * 4), 0xb9400000, 0xd65f03c0]
words += [adr(-len(words) * 4), 0xd65f03c0]
offsets = [0, 8, 16, 28, 40]
text = struct.pack('<' + 'I' * len(words), *words)
image = bytearray(0x4000)
phnum = 3 if args.mode == 'mixed' else 2
image[:64] = struct.pack('<16sHHIQQQIHHHHHH', b'\x7fELF\x02\x01\x01' + b'\0' * 9,
    3, 183, 1, 0, 64, 0x3200, 0, 64, 56, phnum, 64, 6, 5)
span = 0x9000000 if args.mode == 'far' else 0x3000 if args.mode == 'mixed' else 0x4000
filesize = 0x3000 if args.mode == 'mixed' else len(image)
image[64:120] = struct.pack('<IIQQQQQQ', 1, 5, 0, 0, 0, filesize, span, 0x4000)
image[120:176] = struct.pack('<IIQQQQQQ', 2, 4, 0x200, 0x200, 0x200, 16 * 6, 16 * 6, 8)
if args.mode == 'mixed':
    image[176:232] = struct.pack('<IIQQQQQQ', 1, 6, 0x3000, 0x3000, 0x3000, 0x1000, 0x1000, 0x1000)
strings = b'\0'; symbols = bytes(24)
for name, offset in zip(names, offsets):
    symbols += struct.pack('<IBBHQQ', len(strings), 0x12, 0, 1, 0x1000 + offset, 8)
    strings += name.encode() + b'\0'
image[0x400:0x400 + len(strings)] = strings
image[0x600:0x600 + len(symbols)] = symbols
hashes = [1, len(names) + 1, 1, 0] + list(range(2, len(names) + 1)) + [0]
image[0x900:0x900 + 4 * len(hashes)] = struct.pack('<' + 'I' * len(hashes), *hashes)
for n, pair in enumerate([(4, 0x900), (5, 0x400), (6, 0x600), (10, len(strings)), (11, 24), (0, 0)]):
    struct.pack_into('<qQ', image, 0x200 + n * 16, *pair)
image[0x1000:0x1000 + len(text)] = text
section_names = b'\0.text\0.dynstr\0.dynsym\0.hash\0.shstrtab\0'
image[0x3000:0x3000 + len(section_names)] = section_names
sections = [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    (1, 1, 6, 0x1000, 0x1000, len(text), 0, 0, 4, 0),
    (7, 3, 2, 0x400, 0x400, len(strings), 0, 0, 1, 0),
    (15, 11, 2, 0x600, 0x600, len(symbols), 2, 1, 8, 24),
    (23, 5, 2, 0x900, 0x900, 4 * len(hashes), 3, 0, 4, 4),
    (29, 3, 0, 0, 0x3000, len(section_names), 0, 0, 1, 0)]
for n, row in enumerate(sections): struct.pack_into('<IIQQQQIIQQ', image, 0x3200 + n * 64, *row)
fixture = out / 'libview-edge.so'
fixture.write_bytes(image)
# Legacy environment variables cannot activate this capability implicitly.
os.environ['TL_ORIGINAL_CODE_VIEW'] = '1'
runtime = ctypes.CDLL(str(args.runtime / 'lib/libHuskRuntime.dylib'))
runtime.tl_ld_enable_code_views.restype = ctypes.c_bool
if args.mode != 'disabled': assert runtime.tl_ld_enable_code_views()
runtime.tl_set_data_dir.argtypes = [ctypes.c_char_p]
runtime.tl_set_data_dir(bytes(out))
runtime.tl_ld_load.argtypes = [ctypes.c_char_p]; runtime.tl_ld_load.restype = ctypes.c_void_p
runtime.tl_ld_sym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]; runtime.tl_ld_sym.restype = ctypes.c_void_p
runtime.tl_ld_code_writable.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
runtime.tl_ld_code_writable.restype = ctypes.c_void_p
lib = runtime.tl_ld_load(bytes(fixture))
assert lib
tp = runtime.tl_ld_sym(lib, b'tp')
assert not runtime.tl_ld_enable_code_views()
if args.mode == 'far':
    assert runtime.tl_ld_code_writable(tp, 4)
    results = {}
    for name in names:
        address = runtime.tl_ld_sym(lib, name.encode())
        results[name] = ctypes.CFUNCTYPE(ctypes.c_uint64)(address)()
    assert results['tp'] and results['ctr'] == 0x8444c004
    assert results['pid'] == os.getpid()
    assert results['read_code'] == 0xd53bd040 and results['code_address'] == tp
else:
    assert not runtime.tl_ld_code_writable(tp, 4)
    results = {'unsupported_or_disabled_view_rejected': True}
(out / 'verification.json').write_text(json.dumps({'mode': args.mode, 'passed': True, **results}, indent=2) + '\n')
print('PASS:', args.mode, results, flush=True)
