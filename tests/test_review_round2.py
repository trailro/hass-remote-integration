"""Fixes from the second review round: leftover recovery, power loss after a restore, superseded recovery."""

import importlib
import json
import os
import sys
import tempfile
import unittest

from custom_components.integration_manager.ha_updater import HaUpdater


class BootDecisionsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.environ["HRI_CONFIG"] = self.cfg
        sys.modules.pop("entrypoint", None)
        self.ep = importlib.import_module("entrypoint")
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        for version in ("2026.8.3", "2026.9.2"):
            venv = os.path.join(self.cfg, f"venv-{version}")
            ha_pkg = os.path.join("lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant")
            for folder in ("bin", ha_pkg):
                os.makedirs(os.path.join(venv, folder))
            for marker in (".ok", "bin/python", os.path.join(ha_pkg, "__init__.py")):
                open(os.path.join(venv, marker), "w").close()

    def tearDown(self):
        os.environ.pop("HRI_CONFIG", None)
        sys.modules.pop("entrypoint", None)

    def test_leftover_recovery_does_not_stop_a_switch_the_user_scheduled(self):
        state = {"recovery": {"backup": "old.zip", "from": "2026.9.2", "for": "2026.8.3"},
                 "change": {"to": "2026.8.3", "mode": "keep", "backup": "pre.zip", "at": "2026-09-15T10:00:00"}}
        self.assertEqual(self.ep.apply_config_changes(state, "2026.8.3", "2026.9.2"), "2026.8.3")
        self.assertTrue(state["change"]["applied"])
        self.assertNotIn("stopped", state.get("last_error") or "")

    def test_restore_applied_before_a_power_loss_counts_for_its_switch(self):
        state = {"change": {"to": "2026.8.3", "mode": "restore", "backup": "pre.zip", "at": "2026-09-15T10:00:00", "parts": ["storage"]},
                 "last_restore": {"ok": True, "for_version": "2026.8.3", "parts": ["storage"], "at": "2026-09-15T10:01:00"}}
        self.assertEqual(self.ep.apply_config_changes(state, "2026.8.3", "2026.9.2"), "2026.8.3")
        self.assertTrue(state["change"]["applied"])

    def test_an_older_restore_does_not_count_for_a_newer_switch(self):
        state = {"change": {"to": "2026.8.3", "mode": "restore", "backup": "pre.zip", "at": "2026-09-15T10:00:00", "parts": ["storage"]},
                 "last_restore": {"ok": True, "for_version": "2026.8.3", "parts": ["storage"], "at": "2026-09-01T10:01:00"}}
        self.assertEqual(self.ep.apply_config_changes(state, "2026.8.3", "2026.9.2"), "2026.9.2")

    def test_clean_start_already_emptied_before_a_power_loss_counts(self):
        with open(os.path.join(self.cfg, "integration_manager", "rebuild-pending.json"), "w", encoding="utf-8") as fh:
            json.dump({"to": "2026.8.3", "stage": "import", "backup": "pre.zip"}, fh)
        state = {"change": {"to": "2026.8.3", "mode": "rebuild", "backup": "pre.zip", "at": "2026-09-15T10:00:00"}}
        self.assertEqual(self.ep.apply_config_changes(state, "2026.8.3", "2026.9.2"), "2026.8.3")


class SupersededRecoveryTest(unittest.TestCase):
    def test_a_new_intention_drops_a_leftover_recovery(self):
        path = os.path.join(tempfile.mkdtemp(), "ha.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"current": "2026.9.2", "recovery": {"backup": "old.zip", "from": "2026.9.2", "for": "2026.8.3"}}, fh)
        up = object.__new__(HaUpdater)
        up.file = path
        self.assertNotIn("recovery", up.set_desired("2026.8.3", change={"to": "2026.8.3", "mode": "keep"}))
