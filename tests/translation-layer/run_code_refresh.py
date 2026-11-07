#!/usr/bin/env python3
"""Check explicit retranslation after code changes, including warm stub caches."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--linker', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
task_directory('code-refresh-test')
out = temporary_path(args.out)
out.mkdir(parents=True, exist_ok=False)
source = out / 'fixture.S'
source.write_text('''.text
.p2align 4
.global refreshed
.type refreshed,@function
refreshed:
.cfi_startproc
nop
nop
nop
nop
ret
.cfi_endproc
.size refreshed, .-refreshed
''')
environment = dict(os.environ)
linker = args.linker.resolve(strict=True)
flavor = ['-flavor', 'gnu'] if linker.name == 'rust-lld' else []
if flavor:
    environment['DYLD_LIBRARY_PATH'] = str(linker.parents[3])
subprocess.run(['xcrun', 'clang', '--target=aarch64-linux-android29', '-fPIC', '-c', str(source),
                '-o', str(out / 'fixture.o')], env=environment, check=True)
subprocess.run([str(linker), *flavor, '-shared', '--hash-style=both', '-soname', 'librefresh.so',
                str(out / 'fixture.o'), '-o', str(out / 'librefresh.so')], env=environment, check=True)
runtime = ctypes.CDLL(str(args.runtime.resolve(strict=True) / 'lib/libHuskRuntime.dylib'))
runtime.tl_ld_enable_code_views.restype = ctypes.c_bool
assert runtime.tl_ld_enable_code_views()
runtime.tl_set_data_dir.argtypes = [ctypes.c_char_p]
runtime.tl_set_data_dir(bytes(out))
runtime.tl_ld_load.argtypes = [ctypes.c_char_p]
runtime.tl_ld_load.restype = ctypes.c_void_p
lib = runtime.tl_ld_load(bytes(out / 'librefresh.so'))
if not lib:
    raise RuntimeError('fixture did not load')
runtime.tl_ld_sym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
runtime.tl_ld_sym.restype = ctypes.c_void_p
function = runtime.tl_ld_sym(lib, b'refreshed')
runtime.tl_ld_lib_base.argtypes = [ctypes.c_void_p]
runtime.tl_ld_lib_base.restype = ctypes.c_void_p
base = runtime.tl_ld_lib_base(lib)
runtime.tl_ld_write_code.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
runtime.tl_ld_write_code.restype = ctypes.c_bool
runtime.tl_ld_retranslate_range.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t]
runtime.tl_ld_retranslate_range.restype = ctypes.c_bool
# A thread-pointer trap between the two x18 instructions makes the unadapted
# code fail deterministically: Apple's kernel does not preserve real x18.
code = (ctypes.c_uint32 * 4)(0xd2800552, 0xd53bd041, 0xaa1203e0, 0xd65f03c0)
assert runtime.tl_ld_write_code(function, code, 16)
test = ctypes.CFUNCTYPE(ctypes.c_uint64)(function)
before = test()
assert before == 0
assert not runtime.tl_ld_retranslate_range(lib, function - base + 1, 16)
assert not runtime.tl_ld_retranslate_range(lib, 0, 16)
assert not runtime.tl_ld_enable_code_views(), 'late mode changes must be rejected'
for begin, size in [(function - base, 0), (function - base, 3), (function - base, (16 << 20) + 4),
                    (2**64 - 4, 16)]:
    assert not runtime.tl_ld_retranslate_range(lib, begin, size)
results = []
for _ in range(8):
    assert runtime.tl_ld_retranslate_range(lib, function - base, 16)
    # Immediate calls also exercise newly emitted stubs in prefetched cache lines.
    results.append(test())
    assert results[-1] == 42
(out / 'verification.json').write_text(json.dumps({
    'before_retranslation': before, 'after_each_retranslation': results,
    'unaligned_and_noncode_ranges_rejected': True,
}, indent=2) + '\n')
print('PASS: changed code, virtual x18 across TLS traps, repeated translation and stub-cache invalidation', flush=True)
