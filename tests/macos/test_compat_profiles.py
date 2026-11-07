#!/usr/bin/env python3
"""Validate pack import, per-app configuration, rejection and signed host update."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/macos')]
from task_paths import task_directory, temporary_path
import install_apk as installer
import compat_profiles as profiles

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--application', type=Path, required=True)
parser.add_argument('--pack', type=Path, required=True)
parser.add_argument('--runtime-a', type=Path, required=True)
parser.add_argument('--runtime-b', type=Path, required=True)
parser.add_argument('--host', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
task_directory('compat-profile-test')
out = temporary_path(args.out)
out.mkdir(parents=True, exist_ok=False)
os.environ['HUSK_APPS_DATA_BASE'] = str(out / 'data')
source = installer.managed_application(args.application)
source_store = Path(source['runtime_store'])
store = out / 'store'
store.mkdir()
(store / '.leases').mkdir()
for version in [args.runtime_a, args.runtime_b]:
    profiles.clone(version, store / version.name)
    (store / '.leases' / (version.name + '.lock')).touch()
default = {'schema': 1, 'build_id': args.runtime_a.name}
installer.atomic_json(store / 'default.json', default)
before = (store / 'default.json').read_bytes()
application = out / args.application.name
profiles.clone(args.application, application)
config = application / 'Contents/Resources/husk-app.json'
value = json.loads(config.read_text())
value['runtime_store'] = str(store)
config.write_text(json.dumps(value) + '\n')
installer.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(application)])
row = installer.managed_application(application)
original_apk = installer.sha256(application / 'Contents/Resources/package' / row['apk_file'])
checks = []

def record(name):
    checks.append(name)
    print('PASS:', name, flush=True)

def rejects(operation):
    try:
        operation()
    except (ValueError, OSError):
        return
    raise AssertionError('invalid configuration was accepted')

result = profiles.import_pack(args.pack, store)
pack_id = result['id']
pack = profiles.catalog(store)[0][0]
runtime = 'default'
assert (store / 'default.json').read_bytes() == before
assert profiles.settings(row) == profiles.DEFAULT
record('pack import preserves global default and does not activate a patch')
options = profiles.choices(store, row)
assert len(options['packs']) == 2
unrelated = dict(row, android_package='unrelated.example')
assert len(profiles.choices(store, unrelated)['packs']) == 1
record('only exact matching applications see the compatibility pack')
rejects(lambda: profiles.resolve(dict(row, apk_sha256='0' * 64), runtime, 'vulkan', pack_id))
rejects(lambda: profiles.resolve(dict(row, android_version_name='different'), runtime, 'vulkan', pack_id))
rejects(lambda: profiles.resolve(row, runtime, 'opengles', pack_id))
rejects(lambda: profiles.resolve(row, args.runtime_a.name, 'vulkan', pack_id))
assert not profiles.supports({'patch_api': {'abi_version': 2, 'capabilities': 31}}, pack['runtime'])
assert not profiles.supports({'patch_api': {'abi_version': 1, 'capabilities': 1}}, pack['runtime'])
rejects(lambda: profiles.resolve(row, 'default', 'vulkan', 'none'))
record('APK fingerprints, versions, patch ABI, capabilities, per-app pins and backend mismatches are rejected')
execution = profiles.resolve(row, runtime, 'vulkan', pack_id)['execution']
module = Path(execution['modules'][0]['path'])
original = module.read_bytes()
module.write_bytes(original + b'changed')
rejects(lambda: profiles.resolve(row, runtime, 'vulkan', pack_id))
module.write_bytes(original)
record('changed module bytes are rejected')
profiles.configure(application, runtime, 'vulkan', pack_id, args.host)
saved = profiles.settings(row)
assert saved['runtime'] == runtime and saved['graphics'] == 'vulkan' and saved['compatibility'] == pack_id
assert json.loads(config.read_text())['launcher_template_sha256'] == installer.sha256(args.host)
assert installer.sha256(application / 'Contents/Resources/package' / row['apk_file']) == original_apk
installer.run(['codesign', '--verify', '--deep', '--strict', str(application)])
record('configuration persists and host upgrade preserves APK bytes and valid app signature')
lock = Path(row['data_directory']) / 'instance.lock'
with lock.open('rb') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rejects(lambda: profiles.configure(application, 'default', 'auto', 'none'))
assert profiles.settings(row) == saved
record('running-instance lock prevents configuration changes')
profiles.configure(application, 'default', 'auto', 'none')
assert 'execution' not in profiles.settings(row)
assert (store / 'default.json').read_bytes() == before
record('per-app reset removes modules and preserves global default')
profiles.configure(application, runtime, 'vulkan', pack_id)
saved = profiles.settings(row)
assert 'runtime_sha256' not in saved['execution'] and saved['runtime'] == 'default'
installer.atomic_json(store / 'default.json', {'schema': 1, 'build_id': args.runtime_b.name})
assert profiles.resolve(row, 'default', 'vulkan', pack_id) == saved
assert profiles.settings(row) == saved
record('same app configuration and patch follow a different global Runtime without rewriting or recompiling')
installer.atomic_json(store / 'default.json', default)
(out / 'results.json').write_text(json.dumps({'passed': True, 'checks': checks,
    'application': str(application), 'state_root': row['data_directory'], 'store': str(store)}, indent=2) + '\n')
