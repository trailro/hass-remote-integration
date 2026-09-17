"""Review round 14, store: boot sweeps of what a killed write leaves behind."""

import asyncio
import logging
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for


class YamlTmpSweepTest(unittest.TestCase):
    """Installer.yaml_write's temporary file, left by a kill between its mkstemp and its replace, stayed for good."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.ep = entrypoint_for(self, self.cfg)
        self.ydir = os.path.join(self.cfg, "integration_manager", "yaml")
        os.makedirs(self.ydir)

    def make(self, name, age):
        path = os.path.join(self.ydir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("a: 1\n")
        old = time.time() - age
        os.utime(path, (old, old))
        return name

    def test_old_leftovers_go_everything_else_stays(self):
        fd, real = tempfile.mkstemp(dir=self.ydir, prefix=".my_domain.yaml.", suffix=".tmp")  # as yaml_write names it
        os.close(fd)
        gone = [self.make(os.path.basename(real), 3600), self.make(".demo.yaml.ab12_x9z.tmp", 3600)]
        kept = [self.make(".demo.yaml.fresh123.tmp", 5), self.make("demo.yaml", 3600), self.make("x.json.abc.tmp", 3600),
                self.make(".demo.yaml.abc.tmp.bak", 3600), self.make(".Demo.yaml.abc.tmp.d", 3600)]
        os.symlink(os.path.join(self.ydir, "demo.yaml"), os.path.join(self.ydir, ".link.yaml.abcdefgh.tmp"))
        old = time.time() - 3600
        os.utime(os.path.join(self.ydir, ".link.yaml.abcdefgh.tmp"), (old, old), follow_symlinks=False)
        kept.append(".link.yaml.abcdefgh.tmp")
        self.ep.sweep_json_tmp_files()
        self.assertEqual(sorted(os.listdir(self.ydir)), sorted(kept))
        self.assertFalse(any(g in os.listdir(self.ydir) for g in gone))

    def test_no_yaml_directory(self):
        shutil.rmtree(self.ydir)
        self.ep.sweep_json_tmp_files()
        self.assertFalse(os.path.exists(self.ydir))


class ImportCommitLeftoverTest(unittest.TestCase):
    """The import's commit swallowed a failed delete of .storage/<store>.pre-import: the next boot then put the
    original back over the store the import had just brought in."""

    def setUp(self):
        from custom_components.integration_manager import ha_import

        self.ha_import = ha_import
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.storage = os.path.join(self.cfg, ".storage")
        src = os.path.join(self.cfg, ha_import.EXTRACT_DIR, ".storage")
        os.makedirs(src)
        os.makedirs(self.storage)
        with open(os.path.join(src, "hub.e1"), "w", encoding="utf-8") as fh:
            fh.write("from the backup")
        with open(os.path.join(self.storage, "hub.e1"), "w", encoding="utf-8") as fh:
            fh.write("this volume's own")

    def run_import(self, failing):
        ha_import = self.ha_import
        summary = {"domains": {"hub": {"entries": [{"entry_id": "e1", "data": {}}], "storage_files": ["hub.e1"]}}}

        async def executor(fn, *args):
            return fn(*args)

        async def async_add(_entry):
            return None

        config_entries = SimpleNamespace(async_entries=lambda _d=None: [], async_get_entry=lambda _i: None, async_add=async_add)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), config_entries=config_entries, async_add_executor_job=executor)
        real_remove = os.remove

        def remove(path, *args, **kwargs):
            if str(path).endswith(failing):
                raise OSError(5, "Input/output error")
            return real_remove(path, *args, **kwargs)

        with mock.patch.object(ha_import, "load_summary", return_value=summary), mock.patch.object(ha_import, "_forget_cached_stores"), \
                mock.patch.object(ha_import.os, "remove", remove), self.assertLogs(ha_import.__name__, logging.ERROR) as logs:
            asyncio.run(ha_import.apply(hass, mock.Mock(), "hub", "e1", None, None, align=False, copy_storage=True, running=False, cleanup=False))
        return "\n".join(logs.output)

    def read(self, name="hub.e1"):
        with open(os.path.join(self.storage, name), encoding="utf-8") as fh:
            return fh.read()

    def test_a_failed_delete_after_a_committed_import_is_not_put_back_at_boot(self):
        logs = self.run_import(failing=(".pre-import", ".pre-import.done"))
        self.assertIn("hub.e1", logs)
        self.assertEqual(sorted(os.listdir(self.storage)), ["hub.e1", "hub.e1.pre-import.done"])
        self.assertEqual(self.read(), "from the backup")
        ep = entrypoint_for(self, self.cfg)
        os.makedirs(ep.STATE_DIR, exist_ok=True)
        ep.clean_import_leftovers()
        self.assertEqual(sorted(os.listdir(self.storage)), ["hub.e1"])
        self.assertEqual(self.read(), "from the backup")

    def test_an_interrupted_import_is_still_put_back(self):
        ep = entrypoint_for(self, self.cfg)
        os.makedirs(ep.STATE_DIR, exist_ok=True)
        os.replace(os.path.join(self.storage, "hub.e1"), os.path.join(self.storage, "hub.e1.pre-import"))
        with open(os.path.join(self.storage, "hub.e1"), "w", encoding="utf-8") as fh:
            fh.write("half copied")
        ep.clean_import_leftovers()
        self.assertEqual(sorted(os.listdir(self.storage)), ["hub.e1"])
        self.assertEqual(self.read(), "this volume's own")

    def test_a_done_marker_is_not_in_a_backup(self):
        self.assertTrue(backupkit._excluded(".storage/hub.e1.pre-import.done"))


if __name__ == "__main__":
    unittest.main()
