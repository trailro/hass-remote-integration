"""End-to-end version journeys: config flow versions, minimum HA, fallback backup labels, restores keeping settings."""

import io
import json
import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace

import backupkit
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager.ha_updater import HaUpdater
from custom_components.integration_manager.installer import Installer
from custom_components.integration_manager.preflight import _config_flow_version


class ConfigFlowVersionTest(unittest.TestCase):
    def _dir(self, text):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "config_flow.py"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return d

    def test_version_read_without_importing(self):
        self.assertEqual(_config_flow_version(self._dir("class F(ConfigFlow, domain='x'):\n    VERSION = 2\n")), 2)
        self.assertEqual(_config_flow_version(self._dir("class F(ConfigFlow, domain='x'):\n    pass\n")), 1)
        self.assertIsNone(_config_flow_version(self._dir("class Other:\n    VERSION = 5\n")))
        self.assertIsNone(_config_flow_version(tempfile.mkdtemp()))


class HacsMinHaTest(unittest.TestCase):
    def test_min_ha_from_the_repository_root(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("owner-repo-abc123/hacs.json", json.dumps({"name": "x", "homeassistant": "2026.9.0"}))
            zf.writestr("owner-repo-abc123/custom_components/x/manifest.json", "{}")
        self.assertEqual(Installer._hacs_min_ha(buf.getvalue()), "2026.9.0")
        empty = io.BytesIO()
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("owner-repo-abc123/custom_components/x/manifest.json", "{}")
        self.assertIsNone(Installer._hacs_min_ha(empty.getvalue()))


class BackupLabelAndSettingsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        state = os.path.join(self.cfg, backupkit.STATE_DIR)
        os.makedirs(os.path.join(self.cfg, ".storage"))
        os.makedirs(state)
        for rel, text in ((".storage/core.config_entries", "{}"), (f"{backupkit.STATE_DIR}/state.json", "{}"),
                          (f"{backupkit.STATE_DIR}/settings.json", '{"backup_keep": 0}'),
                          (f"{backupkit.STATE_DIR}/ha.json", '{"current": "2026.8.3"}')):
            with open(os.path.join(self.cfg, rel), "w", encoding="utf-8") as fh:
                fh.write(text)

    def test_fallback_copy_records_the_storage_owner(self):
        self.assertEqual(backupkit.create(self.cfg, "x")["ha_version"], "2026.8.3")
        self.assertEqual(backupkit.create(self.cfg, "pre-restore", "2026.9.2")["ha_version"], "2026.9.2")

    def test_restore_keeps_settings_the_backup_lacks(self):
        backupkit._wipe_trees(self.cfg, [f"{backupkit.STATE_DIR}/state.json"], ["manager"])
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, backupkit.STATE_DIR, "settings.json")))
        backupkit._wipe_trees(self.cfg, [f"{backupkit.STATE_DIR}/settings.json"], ["manager"])
        self.assertFalse(os.path.isfile(os.path.join(self.cfg, backupkit.STATE_DIR, "settings.json")))

    def test_pre_restore_copies_are_never_a_downgrade_source(self):
        up = HaUpdater.__new__(HaUpdater)
        listed = [{"name": "20260915-091320-pre-restore.zip", "ha_version": "2026.8.3"},
                  {"name": "20260915-091314-pre-ha-2026.8.3.zip", "ha_version": "2026.8.3"}]
        self.assertEqual(up.config_backup_for("2026.8.3", listed)["name"], "20260915-091314-pre-ha-2026.8.3.zip")


class UpdateRespectsMinimumHaTest(unittest.TestCase):
    def test_release_needing_a_newer_ha_is_not_offered(self):
        versions = {"v3.0.0": {"min_ha": "2024.1.0"}, "v3.1.0": {"min_ha": "2999.1.0"}}
        inst = SimpleNamespace(running="demo", LOCAL_TAG="local", updates={},
                               state=SimpleNamespace(installed={"demo": {"versions": versions}}),
                               min_ha_of=lambda d, t: versions.get(t, {}).get("min_ha"))
        dev = md.ManagerDevice.__new__(md.ManagerDevice)
        dev.installer = inst
        self.assertEqual(dev.integration_latest(), "v3.0.0")


if __name__ == "__main__":
    unittest.main()
