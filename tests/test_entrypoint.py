"""entrypoint.apply_config_changes: what a boot does with a scheduled restore and a version change."""

import importlib
import json
import os
import sys
import tempfile
import unittest
import zipfile

import backupkit


def make_backup(cfg, name, ha_version):
    path = os.path.join(cfg, backupkit.BACKUP_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(backupkit.MARKER, json.dumps({"installed": {}}))
        zf.writestr(".storage/core.config_entries", json.dumps({"from": ha_version}))
        zf.writestr("integration_manager/settings.json", json.dumps({"from": ha_version}))
        zf.writestr("backup-info.json", json.dumps({"ha_version": ha_version}))


class ApplyConfigChangesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.environ["HRI_CONFIG"] = self.cfg
        sys.modules.pop("entrypoint", None)
        self.ep = importlib.import_module("entrypoint")
        os.makedirs(os.path.join(self.cfg, ".storage"))
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        with open(os.path.join(self.cfg, ".storage", "core.config_entries"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"from": "2026.9.2"}))
        with open(os.path.join(self.cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
            fh.write("{}")
        for version in ("2026.8.3", "2026.9.2"):
            venv = os.path.join(self.cfg, f"venv-{version}")
            ha_pkg = os.path.join("lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant")
            for folder in ("bin", ha_pkg):
                os.makedirs(os.path.join(venv, folder))
            for marker in (".ok", "bin/python", os.path.join(ha_pkg, "__init__.py")):  # what venv_ok looks for
                open(os.path.join(venv, marker), "w").close()

    def tearDown(self):
        os.environ.pop("HRI_CONFIG", None)
        sys.modules.pop("entrypoint", None)

    def storage(self):
        with open(os.path.join(self.cfg, ".storage", "core.config_entries"), encoding="utf-8") as fh:
            return json.load(fh)["from"]

    def test_restore_of_a_newer_backup_is_dropped_when_an_older_version_boots(self):
        make_backup(self.cfg, "new.zip", "2026.9.2")
        backupkit.schedule_restore(self.cfg, "new.zip", None)  # allowed when scheduled: the newer version was wanted then
        state = {}
        self.assertEqual(self.ep.apply_config_changes(state, "2026.8.3", "2026.8.3"), "2026.8.3")
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertIn("dropped", state["last_error"])
        self.assertEqual(self.storage(), "2026.9.2")  # untouched

    def test_restore_without_storage_of_a_newer_backup_still_applies(self):
        make_backup(self.cfg, "new.zip", "2026.9.2")
        backupkit.schedule_restore(self.cfg, "new.zip", ["manager"])
        state = {}
        self.ep.apply_config_changes(state, "2026.8.3", "2026.8.3")
        self.assertTrue((state.get("last_restore") or {}).get("ok"), state)

    def test_downgrade_needs_its_own_restore_with_storage(self):
        make_backup(self.cfg, "old.zip", "2026.8.3")
        backupkit.schedule_restore(self.cfg, "old.zip", ["manager"])  # by hand, no .storage
        state = {"change": {"to": "2026.8.3", "mode": "restore", "backup": "pre.zip"}}
        self.assertEqual(self.ep.apply_config_changes(state, "2026.8.3", "2026.9.2"), "2026.9.2")
        self.assertEqual(state["desired"], "2026.9.2")
        self.assertNotIn("change", state)

    def test_downgrade_with_its_own_restore_boots_the_target(self):
        make_backup(self.cfg, "old.zip", "2026.8.3")
        backupkit.schedule_restore(self.cfg, "old.zip", ["storage"], for_version="2026.8.3")
        state = {"change": {"to": "2026.8.3", "mode": "restore", "backup": "pre.zip", "parts": ["storage"]}}
        self.assertEqual(self.ep.apply_config_changes(state, "2026.8.3", "2026.9.2"), "2026.8.3")
        self.assertTrue(state["change"]["applied"])
        self.assertEqual(self.storage(), "2026.8.3")

    def test_recovery_restores_the_parts_the_switch_replaced(self):
        make_backup(self.cfg, "pre.zip", "2026.8.3")
        state = {"change": {"to": "2026.9.2", "mode": "restore", "backup": "pre.zip", "parts": ["storage", "manager"], "applied": True}}
        self.assertTrue(self.ep.restore_after_failed_change(state, "2026.9.2", "2026.8.3"))
        self.assertEqual(state["recovery"]["parts"], ["storage", "manager"])
        self.assertEqual(backupkit.pending_parts(self.cfg), ["storage", "manager"])
