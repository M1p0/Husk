#!/usr/bin/env python3
"""Package explicitly supplied, pinned artifacts for installer import."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / 'tools'))
from task_paths import task_directory, temporary_path

parser = argparse.ArgumentParser(description=__doc__)
for name in ('thread-probe', 'startup', 'moltenvk', 'out'):
    parser.add_argument('--' + name, type=Path, required=True)
args = parser.parse_args()
task_directory('compat-pack-build')
out = temporary_path(args.out)
out.mkdir(parents=True, exist_ok=False)
target = json.loads((HERE / 'target.json').read_text())

def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()

if digest(args.moltenvk) != target['startup_compat']['moltenvk_sha256']:
    raise SystemExit('MoltenVK fingerprint mismatch')
modules = []
for name, source, values in [
    ('thread-probe', args.thread_probe, {'HUSK_PATCH_MINGCHAO_THREAD_PROBE': '1'}),
    ('startup', args.startup, {'HUSK_PATCH_MINGCHAO_UE4_CODE': '1', 'HUSK_PATCH_MINGCHAO_V8': '1'}),
]:
    path = 'modules/' + name + '.dylib'
    (out / path).parent.mkdir(exist_ok=True)
    shutil.copy2(source, out / path)
    module = {'id': name, 'path': path, 'sha256': digest(out / path), 'environment': values}
    if name == 'startup':
        module['graphics_environment'] = {'vulkan': {'HUSK_PATCH_MINGCHAO_VULKAN': '1'}}
    modules.append(module)
(out / 'drivers').mkdir()
shutil.copy2(args.moltenvk, out / 'drivers/libMoltenVK.dylib')
manifest = {
    'schema': 1, 'id': 'mingchao-3.7.1-abi1', 'name': '鸣潮 3.7.1 兼容补丁（实验）', 'experimental': True,
    'target': {key: target[key] for key in ('android_package', 'apk_sha256')} |
              {'android_version_name': target['android_version']},
    'runtime': target['patch_api'],
    'graphics': ['vulkan'], 'default_graphics': 'vulkan',
    'environment': {}, 'modules': modules,
    'resources': [{'path': 'drivers/libMoltenVK.dylib', 'sha256': target['startup_compat']['moltenvk_sha256'],
                   'environment': 'HUSK_PATCH_MINGCHAO_MOLTENVK', 'graphics': 'vulkan'}],
}
(out / 'compatibility.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
print(out / 'compatibility.json')
