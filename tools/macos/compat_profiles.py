"""Explicit compatibility-pack import and per-app cold-start configuration.

Pack manifests are data. Application names, native offsets and SDK behavior live
in their separately built modules, never in the installer or launcher.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
ENVIRONMENT = re.compile(r"HUSK_PATCH_[A-Z0-9_]+|TL_ORIGINAL_CODE_VIEW")
GRAPHICS = {"auto", "opengles", "vulkan"}
DEFAULT = {"schema": 1, "runtime": "default", "graphics": "auto", "compatibility": "none"}


def api():
    import install_apk
    return install_apk


def read(path):
    if path.stat().st_size > 1 << 20:
        raise ValueError("compatibility manifest is too large")
    result = json.loads(path.read_text())
    if not isinstance(result, dict) or result.get("schema") != 1:
        raise ValueError("invalid compatibility manifest")
    return result


def environment(values):
    if not isinstance(values, dict) or len(values) > 32:
        raise ValueError("invalid compatibility environment")
    for key, value in values.items():
        if not ENVIRONMENT.fullmatch(key) or not isinstance(value, str) or len(value) > 4096 or "\0" in value:
            raise ValueError("unsupported compatibility environment entry")
    return dict(values)


def matching(pack, row):
    return all(pack["target"].get(key) == row.get(key) for key in
               ("android_package", "android_version_name", "apk_sha256"))


def requirements(value):
    if (not isinstance(value, dict) or set(value) != {'patch_abi', 'capabilities'} or
            type(value['patch_abi']) is not int or not 1 <= value['patch_abi'] <= 0xffffffff or
            type(value['capabilities']) is not int or not 0 <= value['capabilities'] < (1 << 63)):
        raise ValueError('invalid patch interface requirements')
    return value


def supports(manifest, required):
    actual = manifest.get('patch_api', {})
    if not isinstance(actual, dict): return False
    return (actual.get('abi_version') == required['patch_abi'] and
            type(actual.get('capabilities')) is int and
            actual['capabilities'] & required['capabilities'] == required['capabilities'])


def validate_pack(pack, root):
    a = api()
    if not IDENTIFIER.fullmatch(pack.get("id", "")) or not isinstance(pack.get("name"), str) or not 1 <= len(pack["name"]) <= 160:
        raise ValueError("invalid compatibility pack identity")
    target = pack.get("target", {})
    if (not all(isinstance(target.get(k), str) and target[k] for k in ("android_package", "android_version_name", "apk_sha256"))
            or not re.fullmatch(r"[0-9a-f]{64}", target["apk_sha256"])):
        raise ValueError("invalid compatibility target")
    graphics = pack.get("graphics")
    if not isinstance(graphics, list) or not graphics or not set(graphics) <= GRAPHICS or pack.get("default_graphics") not in graphics:
        raise ValueError("invalid compatibility graphics options")
    runtime = pack.get("runtime", {})
    requirements(runtime)
    environment(pack.get("environment", {}))
    modules = pack.get("modules")
    if not isinstance(modules, list) or not 1 <= len(modules) <= 16:
        raise ValueError("select an explicit module list")
    names = set()
    for module in modules:
        if not IDENTIFIER.fullmatch(module.get("id", "")) or module["id"] in names:
            raise ValueError("duplicate or invalid module identity")
        names.add(module["id"])
        path = a.safe_file(root, module["path"])
        if not re.fullmatch(r"[0-9a-f]{64}", module.get("sha256", "")) or a.sha256(path) != module["sha256"]:
            raise ValueError("compatibility module fingerprint mismatch")
        a.run(["codesign", "--verify", "--strict", str(path)])
        environment(module.get("environment", {}))
        for backend, values in module.get("graphics_environment", {}).items():
            if backend not in graphics: raise ValueError("unsupported module graphics option")
            environment(values)
    for resource in pack.get("resources", []):
        path = a.safe_file(root, resource["path"])
        if not re.fullmatch(r"[0-9a-f]{64}", resource.get("sha256", "")) or a.sha256(path) != resource["sha256"]:
            raise ValueError("compatibility resource fingerprint mismatch")
        environment({resource["environment"]: str(path)})
        if resource.get("graphics") not in graphics: raise ValueError("unsupported resource graphics option")


def catalog(store):
    path = store / "compatibility.json"
    value = read(path) if path.exists() else {"schema": 1, "packs": []}
    packs = []
    for item in value.get("packs", []):
        manifest = api().safe_file(store, item["manifest"])
        if api().sha256(manifest) != item["sha256"]: raise ValueError("compatibility catalog fingerprint mismatch")
        pack = read(manifest)
        if pack.get("id") != item.get("id"): raise ValueError("compatibility catalog identity mismatch")
        packs.append((pack, manifest))
    return packs


def settings(row):
    path = Path(row["data_directory"]) / "config/launch.json"
    try:
        return read(path) if path.exists() else dict(DEFAULT)
    except (OSError, ValueError) as error:
        # Keep the installed app visible so the manager can repair its settings.
        return dict(DEFAULT, configuration_error=str(error))


def choices(store, row=None):
    store = store.resolve(strict=True)
    runtimes = [{"id": "default", "name": "跟随全局默认"}]
    current = read(store / 'default.json')['build_id']
    runtimes[0]['name'] = '跟随全局（' + current + '）'
    packs = [{"id": "none", "name": "不使用兼容补丁", "graphics": ["auto", "opengles"], "default_graphics": "auto"}]
    for pack, _ in catalog(store):
        if row is not None and matching(pack, row):
            packs.append({key: pack[key] for key in ("id", "name", "graphics", "default_graphics")} |
                         {"runtime": "default", "experimental": pack.get("experimental", True)})
    return {"runtimes": runtimes, "packs": packs, "selected": settings(row) if row else dict(DEFAULT)}


def clone(source, destination):
    # APFS clones avoid duplicating large APK and Java Runtime payloads.
    subprocess.run(["/bin/cp", "-cR", str(source), str(destination)], check=True, capture_output=True)


def import_pack(manifest, store):
    a = api()
    manifest = manifest.resolve(strict=True)
    store = store.resolve(strict=True)
    pack = read(manifest)
    validate_pack(pack, manifest.parent)
    with a.directory_lock(store / ".compatibility.lock"):
        # Store only the declared modules/resources, never scan a source directory.
        stage = Path(tempfile.mkdtemp(prefix="compat-pack-", dir=a.staging_directory(store)))
        payload = stage / "payload"
        payload.mkdir()
        for item in [*pack["modules"], *pack.get("resources", [])]:
            target = payload / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(a.safe_file(manifest.parent, item["path"]), target)
        a.atomic_json(payload / "compatibility.json", pack)
        sha = a.sha256(payload / "compatibility.json")
        packs_root = store / "compat-packs"
        packs_root.mkdir(exist_ok=True)
        final = packs_root / (pack["id"] + "-" + sha[:16])
        if final.exists():
            if a.sha256(a.safe_file(final, "compatibility.json")) != sha: raise ValueError("pack publication collision")
            shutil.rmtree(a.temporary_path(payload))
        else:
            a.publish_exclusive(payload, final)
        stage.rmdir()
        index = store / "compatibility.json"
        value = read(index) if index.exists() else {"schema": 1, "packs": []}
        value["packs"] = [item for item in value["packs"] if item["id"] != pack["id"]]
        value["packs"].append({"id": pack["id"], "manifest": str((final / "compatibility.json").relative_to(store)), "sha256": sha})
        a.atomic_json(index, value)
        return {"ok": True, "id": pack["id"], "runtime": "default"}


def resolve(row, runtime_id, graphics, pack_id):
    a = api()
    store = Path(row["runtime_store"]).resolve(strict=True)
    if runtime_id != "default": raise ValueError("应用统一跟随全局运行库，不能固定单独版本")
    if graphics not in GRAPHICS: raise ValueError("invalid graphics selection")
    result = {"schema": 1, "runtime": runtime_id, "graphics": graphics, "compatibility": pack_id, "apk_sha256": row["apk_sha256"]}
    actual_id = read(store / "default.json")["build_id"]
    if not isinstance(actual_id, str) or not IDENTIFIER.fullmatch(actual_id):
        raise ValueError("全局运行库版本标识无效")
    runtime = store / actual_id
    with a.version_lease(runtime): runtime_manifest = a.validate_runtime(runtime)
    if pack_id == "none":
        if graphics == "vulkan": raise ValueError("所选配置未提供 Vulkan 驱动，请选择配套兼容包")
        return result
    found = [(p, f) for p, f in catalog(store) if p["id"] == pack_id]
    if len(found) != 1: raise ValueError("compatibility pack is not imported")
    pack, manifest = found[0]
    if not matching(pack, row): raise ValueError("兼容包与 APK 包名、版本或指纹不匹配")
    if graphics not in pack["graphics"]:
        raise ValueError("兼容包不支持所选图形后端")
    validate_pack(pack, manifest.parent)
    if not supports(runtime_manifest, pack["runtime"]):
        raise ValueError("全局运行库不支持补丁要求的接口版本或能力，请更新运行库或补丁")
    values = environment(pack.get("environment", {}))
    modules, resources = [], []
    for module in pack["modules"]:
        modules.append({"path": str(a.safe_file(manifest.parent, module["path"])), "sha256": module["sha256"]})
        values.update(environment(module.get("environment", {})))
        values.update(environment(module.get("graphics_environment", {}).get(graphics, {})))
    for item in pack.get("resources", []):
        if item["graphics"] != graphics: continue
        path = str(a.safe_file(manifest.parent, item["path"]))
        resources.append({"path": path, "sha256": item["sha256"]})
        values[item["environment"]] = path
    result["execution"] = {"runtime_requirements": pack["runtime"], "modules": modules,
                           "resources": resources, "environment": values,
                           "manifest": {"path": str(manifest), "sha256": a.sha256(manifest)}}
    return result


def configure(application, runtime_id, graphics, pack_id, host=None):
    a = api()
    row = a.managed_application(application)
    data = Path(row["data_directory"])
    data.mkdir(parents=True, exist_ok=True)
    with a.directory_lock(application.parent / ".husk-install.lock"), a.directory_lock(data / "instance.lock"):
        result = resolve(row, runtime_id, graphics, pack_id)
        configuration = data / "config/launch.json"
        configuration.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = None
        if host:
            host = host.resolve(strict=True)
            a.run(["codesign", "--verify", "--strict", str(host)])
            template_hash = a.sha256(host)
            metadata = json.loads(a.safe_file(application, "Contents/Resources/husk-app.json").read_text())
            if metadata.get("launcher_template_sha256") != template_hash:
                stage = Path(tempfile.mkdtemp(prefix="compat-host-update-", dir=a.staging_directory(application.parent)))
                replacement = stage / application.name
                clone(application, replacement)
                shutil.copy2(host, replacement / "Contents/MacOS/HuskAppHost")
                metadata["launcher_template_sha256"] = template_hash
                a.atomic_json(replacement / "Contents/Resources/husk-app.json", metadata)
                a.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(replacement)])
                a.run(["codesign", "--verify", "--deep", "--strict", str(replacement)])
                # Keep the complete old signed shell as a task-local recovery copy.
                backup = stage / "previous.app"
                a.publish_exclusive(application, backup)
                try:
                    a.publish_exclusive(replacement, application)
                except BaseException:
                    a.publish_exclusive(backup, application)
                    raise
                a.sync_directory(application.parent)
        try:
            a.atomic_json(configuration, result)
        except BaseException:
            if backup is not None and backup.exists():
                a.publish_exclusive(application, stage / "failed.app")
                a.publish_exclusive(backup, application)
                a.sync_directory(application.parent)
            raise
    return {"ok": True, "application": str(application), "selected": result}
