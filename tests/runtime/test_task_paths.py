#!/usr/bin/env python3
"""Check explicit temporary-directory configuration and containment guards."""
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import task_paths


class TaskPathsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task = task_paths.task_directory("task-paths-tests")

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="task-paths-", dir=self.task / "tmp"))
        self.environment = patch.dict(os.environ)
        self.environment.start()
        os.environ.pop("HUSK_TASK_TMPDIR", None)
        os.environ.pop("HUSK_TASK_ROOT", None)
        self.cached_task = task_paths._task
        task_paths._task = None
        self.pycache_prefix = sys.pycache_prefix

    def tearDown(self):
        self.environment.stop()
        task_paths._task = self.cached_task
        sys.pycache_prefix = self.pycache_prefix
        shutil.rmtree(self.work)

    def test_missing_configuration_does_not_use_system_tmpdir(self):
        with self.assertRaisesRegex(ValueError, "set HUSK_TASK_TMPDIR"):
            task_paths.task_directory()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_explicit_task_and_child_environment(self):
        target = self.work / "arbitrary" / "nested" / "task"
        os.environ["HUSK_TASK_TMPDIR"] = str(target)
        self.assertEqual(task_paths.task_directory(), target)
        self.assertEqual(task_paths.task_directory("another-label"), target)
        for name in ["tmp", "tmp/pycache", "tmp/cocoa-home", "tmp/cache",
                     "tmp/clang-module-cache", "tmp/swift-module-cache"]:
            directory = target / name
            self.assertTrue(directory.is_dir())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for variable in ["TMPDIR", "TMP", "TEMP"]:
            self.assertEqual(os.environ[variable], str(target / "tmp"))
        for variable in ["CFFIXED_USER_HOME", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX",
                         "CLANG_MODULE_CACHE_PATH", "SWIFT_MODULECACHE_PATH"]:
            self.assertTrue(Path(os.environ[variable]).is_relative_to(target / "tmp"))

    def test_configured_parent_generates_a_task(self):
        parent = self.work / "custom-task-parent"
        os.environ["HUSK_TASK_ROOT"] = str(parent)
        target = task_paths.task_directory("a/b label")
        self.assertEqual(target.parent, parent)
        self.assertTrue(target.name.startswith("a-b-label-"))
        self.assertEqual(os.environ["HUSK_TASK_TMPDIR"], str(target))

    def test_relative_paths_and_filesystem_root_are_rejected(self):
        for variable in ["HUSK_TASK_TMPDIR", "HUSK_TASK_ROOT"]:
            for value in ["relative/path", "/"]:
                with self.subTest(variable=variable, value=value):
                    os.environ[variable] = value
                    with self.assertRaises(ValueError):
                        task_paths.task_directory()
                    del os.environ[variable]
        self.assertEqual(list(self.work.iterdir()), [])

    def test_explicit_task_must_belong_to_configured_parent(self):
        os.environ["HUSK_TASK_ROOT"] = str(self.work / "allowed")
        os.environ["HUSK_TASK_TMPDIR"] = str(self.work / "outside")
        with self.assertRaisesRegex(ValueError, "child of HUSK_TASK_ROOT"):
            task_paths.task_directory()
        self.assertFalse((self.work / "outside").exists())

    def test_symlinked_cache_is_rejected_without_touching_target(self):
        target = self.work / "task"
        outside = self.work / "outside"
        (target / "tmp").mkdir(parents=True)
        outside.mkdir(mode=0o755)
        mode = stat.S_IMODE(outside.stat().st_mode)
        (target / "tmp/cache").symlink_to(outside, target_is_directory=True)
        os.environ["HUSK_TASK_TMPDIR"] = str(target)
        with self.assertRaisesRegex(ValueError, "not a symlink"):
            task_paths.task_directory()
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), mode)
        self.assertEqual(list(outside.iterdir()), [])

    def test_output_cannot_escape_task_through_path_or_symlink(self):
        target = self.work / "task"
        os.environ["HUSK_TASK_TMPDIR"] = str(target)
        task_paths.task_directory()
        outside = self.work / "outside"
        outside.mkdir()
        (target / "escape").symlink_to(outside, target_is_directory=True)
        self.assertEqual(task_paths.temporary_path(target / "evidence/result"),
                         target / "evidence/result")
        for path in [target, outside, target / "../outside/file", target / "escape/file"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                task_paths.temporary_path(path)


if __name__ == "__main__":
    unittest.main()
