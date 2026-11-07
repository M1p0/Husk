#!/usr/bin/env python3
"""Exercise native compatibility controls and persistence on a task-local app."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/macos')]
from task_paths import task_directory, temporary_path
from compat_profiles import clone
from install_apk import atomic_json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--manager', type=Path, required=True)
parser.add_argument('--application', type=Path, required=True)
parser.add_argument('--compatibility', required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
task = task_directory('compatibility-ui')
out = temporary_path(args.out)
out.mkdir(parents=True, exist_ok=False)
manager = out / 'Manager.app'
clone(temporary_path(args.manager), manager)
application = temporary_path(args.application)
installed = json.loads((application / 'Contents/Resources/husk-app.json').read_text())
config_file = manager / 'Contents/Resources/installer.json'
config = json.loads(config_file.read_text())
config.update(applications=str(application.parent), runtime_store=installed['runtime_store'], scan_roots=[str(application.parent)])
config_file.write_text(json.dumps(config) + '\n')
subprocess.run(['codesign', '--force', '--sign', '-', '--timestamp=none', str(manager)], check=True)
state_root = out / 'ui'
state_root.mkdir()
control = out / 'control.jsonl'
control.touch()
state_file = state_root / 'state.json'

def state():
    try: return json.loads(state_file.read_text())
    except (OSError, ValueError): return {}

def wait(predicate, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        current = state()
        if predicate(current): return current
        time.sleep(.2)
    raise TimeoutError(str(state()))

def send(action, **fields):
    with control.open('a') as stream: stream.write(json.dumps({'action': action, **fields}) + '\n')

def start(log_name):
    with (out / log_name).open('w') as log:
        child = subprocess.Popen([str(manager / 'Contents/MacOS/HuskInstaller'), '--test-root', str(state_root),
            '--test-control', str(control), '--no-activate'], env=dict(os.environ, HUSK_TASK_TMPDIR=str(task)), stdout=log, stderr=log)
    wait(lambda s: s.get('pid') == child.pid and not s.get('busy', True))
    return child

child = start('first.log')
try:
    send('select', application=str(application))
    initial = wait(lambda s: not s.get('busy', True) and s.get('launch_options', {}).get('packs') and
                   s.get('selected', {}).get('application') == str(application))
    assert args.compatibility in [p['id'] for p in initial['launch_options']['packs']]
    send('configure', compatibility=args.compatibility)
    saved = wait(lambda s: not s.get('busy', True) and s.get('selected', {}).get('launch_configuration', {}).get('compatibility') == args.compatibility)
    assert saved['graphics_selection'] == 'vulkan'
    assert saved['runtime_selection'] == 'default'
    assert saved['runtime_editable'] is False
    lock = state_root / 'app-data' / installed['bundle_id'] / 'instance.lock'
    with lock.open('rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        send('configure', compatibility='none')
        rejected = wait(lambda s: not s.get('busy', True) and '正在运行' in s.get('message', '') and
                        s.get('compatibility_selection') == args.compatibility)
        assert rejected['selected']['launch_configuration']['compatibility'] == args.compatibility
    send('configure', compatibility='none')
    wait(lambda s: not s.get('busy', True) and s.get('selected', {}).get('launch_configuration', {}).get('compatibility') == 'none')
    send('configure', graphics='opengles')
    wait(lambda s: not s.get('busy', True) and s.get('selected', {}).get('launch_configuration', {}).get('graphics') == 'opengles')
    send('configure', compatibility=args.compatibility)
    wait(lambda s: not s.get('busy', True) and s.get('selected', {}).get('launch_configuration', {}).get('compatibility') == args.compatibility)
    send('render')
    wait(lambda s: (state_root / 'window.png').exists())
    send('quit'); child.wait(timeout=15)
    control.write_text('')
    child = start('reopen.log')
    send('select', application=str(application))
    reopened = wait(lambda s: not s.get('busy', True) and s.get('compatibility_selection') == args.compatibility)
    assert reopened['graphics_selection'] == 'vulkan' and reopened['runtime_selection'] == saved['runtime_selection']
    store = temporary_path(Path(installed['runtime_store']))
    default_file = store / 'default.json'
    previous = json.loads(default_file.read_text())
    alternate = next(p.name for p in store.iterdir() if (p / 'manifest.json').is_file() and p.name != previous['build_id'])
    application_settings = state_root / 'app-data' / installed['bundle_id'] / 'config/launch.json'
    settings_before = application_settings.read_bytes()
    try:
        atomic_json(default_file, {'schema': 1, 'build_id': alternate})
        send('refresh')
        wait(lambda s: not s.get('busy', True) and alternate in s.get('launch_options', {}).get('runtimes', [{}])[0].get('name', ''))
        assert application_settings.read_bytes() == settings_before
    finally:
        atomic_json(default_file, previous)
    send('refresh')
    wait(lambda s: not s.get('busy', True) and previous['build_id'] in s.get('launch_options', {}).get('runtimes', [{}])[0].get('name', ''))
    (out / 'results.json').write_text(json.dumps({'passed': True, 'native_controls_saved': True,
        'automatic_save': True, 'failed_change_restored_selection': True,
        'global_update_reflected_without_rebinding': True,
        'reopen_preserved_selection': True, 'runtime': reopened['runtime_selection'],
        'compatibility': args.compatibility}, indent=2) + '\n')
    print('PASS: native Runtime/backend/pack controls, signed shell update and manager reopen persistence', flush=True)
finally:
    if child.poll() is None:
        send('quit')
        try: child.wait(timeout=15)
        except subprocess.TimeoutExpired: child.kill(); child.wait()
