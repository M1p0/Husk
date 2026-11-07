#!/usr/bin/env python3
"""Build the explicitly selected SDK module and its isolated native regression."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / 'tools'))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--test-apk', type=Path)
parser.add_argument('--module', choices=('thread-probe', 'startup'), default='thread-probe')
args = parser.parse_args()
if args.module == 'startup' and args.test_apk:
    parser.error('--test-apk runs the thread-probe native regression; validate startup with the actual isolated app')
task_directory('sdk-patch')
out = temporary_path(args.out); out.mkdir(parents=True, exist_ok=True)
if any(out.iterdir()):
    raise SystemExit('Output directory must be empty; use a fresh task subdirectory for each module build')
target = json.loads((HERE / 'target.json').read_text())
library = args.runtime.resolve() / 'lib/libHuskRuntime.dylib'
if hashlib.sha256(library.read_bytes()).hexdigest() != target['runtime_sha256']:
    raise SystemExit('Runtime fingerprint mismatch; refusing to build this version-pinned module')
pins = { 'TARGET_RUNTIME_SHA256': target['runtime_sha256'], 'TARGET_APK_SHA256': target['apk_sha256'],
         'TARGET_SDK_SHA256': target['sdk_sha256'] }
header = ''.join('#define ' + key + ' ' + json.dumps(value) + '\n' for key, value in pins.items())
header += '#define TARGET_APK_SIZE ' + str(target['apk_size']) + 'ull\n'
header += '#define TARGET_START_THUNK_OFFSET ' + target['start_thunk_offset'] + 'ull\n'
(out / 'target.h').write_text(header)
source = 'thread_probe.c' if args.module == 'thread-probe' else 'startup_compat.c'
base = ['xcrun', '--sdk', 'macosx', 'clang', '-arch', 'arm64', '-O1', '-g', '-Wall',
        '-Wno-deprecated-declarations', '-Wno-unused-function', '-I' + str(out), '-I' + str(ROOT / 'src/translation-layer'),
        '-I' + str(ROOT / 'src/translation-layer-next'), str(HERE / source)]
module = out / ('mingchao-' + args.module + '.dylib')
subprocess.run([*base, '-dynamiclib', '-o', str(module)], check=True)
subprocess.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(module)], check=True)
if args.test_apk and args.module == 'thread-probe':
    test = out / 'test-thread-probe'
    subprocess.run([*base, '-DHUSK_PATCH_TEST', '-L' + str(library.parent), '-lHuskRuntime',
                    '-Wl,-rpath,' + str(library.parent), '-o', str(test)], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(test)], check=True)
    subprocess.run([str(test), str(args.test_apk.resolve()), str(library)], check=True, timeout=20)
    smoke = out / 'test-host-threads'
    subprocess.run(['xcrun', '--sdk', 'macosx', 'clang', '-arch', 'arm64', '-O1',
                    str(HERE / 'test_host_threads.c'), '-o', str(smoke)], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(smoke)], check=True)
    flag = 'HUSK_PATCH_MINGCHAO_THREAD_PROBE'
    for enabled in ('0', '1'):
        env = dict(os.environ, DYLD_INSERT_LIBRARIES=str(module)); env[flag] = enabled
        subprocess.run([str(smoke)], env=env, check=True, timeout=5)
    (out / 'verification.json').write_text(json.dumps({
        'target_id': target['id'], 'module': 'thread-probe', 'runtime_sha256': target['runtime_sha256'],
        'module_sha256': hashlib.sha256(module.read_bytes()).hexdigest(),
        'native_regression': 'passed', 'ordinary_threads_disabled': 'passed',
        'ordinary_threads_enabled': 'passed', 'game_startup': 'not_established',
    }, indent=2) + '\n')
print(module)
