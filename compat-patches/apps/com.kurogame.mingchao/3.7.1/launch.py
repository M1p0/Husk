#!/usr/bin/env python3
"""Launch only explicitly selected, pinned modules in an isolated app state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / 'tools'))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config', type=Path, required=True)
args = parser.parse_args()
config = json.loads(args.config.read_text())
target = json.loads((HERE / 'target.json').read_text())
task = task_directory('mingchao-launch')
state = temporary_path(Path(config['state_root']).resolve(strict=True))
host = Path(config['app_host']).resolve(strict=True)
runtime = Path(config['runtime']).resolve(strict=True)
modules = {name: Path(path).resolve(strict=True) for name, path in config['modules'].items()}
features = set(config['features'])
allowed = {'thread-probe', 'ue4-code', 'v8', 'vulkan'}
if not features or not features <= allowed:
    raise SystemExit('Select features explicitly from: ' + ', '.join(sorted(allowed)))
if ('thread-probe' in features and 'thread-probe' not in modules) or (features - {'thread-probe'} and 'startup' not in modules):
    raise SystemExit('A selected feature has no explicitly supplied module')
if set(modules) - {'thread-probe', 'startup'}:
    raise SystemExit('Unknown module; directory scanning is not supported')

def matches(path, expected):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest() == expected

if not matches(runtime / 'lib/libHuskRuntime.dylib', target['runtime_sha256']):
    raise SystemExit('Runtime fingerprint mismatch')
apks = list((host.parent.parent / 'Resources/package').glob('*.apk'))
if len(apks) != 1 or not matches(apks[0], target['apk_sha256']):
    raise SystemExit('APK fingerprint mismatch')
for name, path in modules.items():
    expected = config['module_sha256'].get(name)
    if not expected or not matches(path, expected):
        raise SystemExit('Module fingerprint mismatch: ' + name)
environment = dict(os.environ)
# Flags are confined to this child; inherited diagnostic patches are excluded.
for key in list(environment):
    if key.startswith(('HUSK_PATCH_MINGCHAO_', 'HUSK_DIAG_', 'TL_ORIGINAL_CODE_VIEW')) or key == 'DYLD_INSERT_LIBRARIES':
        environment.pop(key)
selected = []
if 'thread-probe' in features:
    selected.append(modules['thread-probe'])
if features - {'thread-probe'}:
    selected.append(modules['startup'])
environment.update(DYLD_INSERT_LIBRARIES=':'.join(map(str, selected)), TL_ORIGINAL_CODE_VIEW='1')
for feature, flag in {
    'thread-probe': 'THREAD_PROBE', 'ue4-code': 'UE4_CODE', 'v8': 'V8', 'vulkan': 'VULKAN'
}.items():
    if feature in features:
        environment['HUSK_PATCH_MINGCHAO_' + flag] = '1'
if 'vulkan' in features:
    driver = Path(config['moltenvk']).resolve(strict=True)
    if not matches(driver, target['startup_compat']['moltenvk_sha256']):
        raise SystemExit('MoltenVK fingerprint mismatch')
    environment['HUSK_PATCH_MINGCHAO_MOLTENVK'] = str(driver)
logs = temporary_path(task / 'interactive-launch-logs')
logs.mkdir(exist_ok=True)
control = temporary_path(logs / ('control-' + uuid.uuid4().hex + '.jsonl'))
control.touch(exist_ok=True)
with (logs / 'launcher.log').open('a') as log:
    process = subprocess.Popen([str(host), '--runtime', str(runtime), '--state-root', str(state),
                                '--control', str(control)], env=environment,
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
(logs / 'process.json').write_text(json.dumps({'pid': process.pid, 'state_root': str(state),
                                             'control': str(control), 'features': sorted(features)}, indent=2) + '\n')
print('Started isolated patched app, PID', process.pid)
