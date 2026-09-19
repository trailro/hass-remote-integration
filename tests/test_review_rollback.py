"""Two version switches without the restart in between: what a failed smoke test goes back to.

An external review found that the second switch records the first switch's target as the rollback
target, although that version never ran - the process still has the version before it imported.  A
rollback then lands on code nobody has seen boot, and the rejection clears the record, so there is no
automatic way back to the version that did work.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import installer as inst_mod
from custom_components.integration_manager.installer import Installer, State
from tests.test_camp_state import _tmp, _write, _yielding_hass

A, B, C = "1.0.0", "1.1.0", "1.2.0"


class SwitchWithoutRestartTest(unittest.TestCase):
    def installer(self):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        versions = {tag: {"installed_at": tag, "version": tag} for tag in (A, B, C)}
        inst.state = State(domain="demo", installed={"demo": {"running_tag": A, "previous_tag": None,
                                                              "pre_update_backup": None, "versions": versions}})
        for tag in (A, B, C):
            _write(os.path.join(inst._version_dir("demo", tag), "manifest.json"),
                   json.dumps({"domain": "demo", "version": tag}))
            _write(os.path.join(inst._version_dir("demo", tag), "__init__.py"), f"x = {tag!r}\n")
        inst._ensure_deployed("demo", A)
        inst.busy = False
        # the domain is set up: a switch cannot take effect without a restart, which is the whole point
        inst.hass = _yielding_hass(config=SimpleNamespace(components={"demo"}))
        inst.settings = SimpleNamespace(backup_keep=5, int_=lambda key, lo=0, hi=0: 300, bool_=lambda key: False)
        inst._save_state = lambda: None
        inst._smoke_handle = inst._smoke_pending = None
        inst._smoke_waiting, inst._smoke_rechecked = {}, set()
        inst._loaded_tags, inst._code_hash = {"demo": A}, {}  # this process imported A
        inst._abandoned_switch, inst._restart_before_uninstall = {}, {}
        inst._requirements_for = mock.AsyncMock(return_value=[])
        inst._install_requirements = lambda reqs, force=False: []
        inst._apply_patches = lambda domain: "n/a"
        inst._loadable = mock.AsyncMock(return_value=True)
        inst._enable_entries = mock.AsyncMock(return_value=[])
        inst.async_backup = mock.AsyncMock(side_effect=[{"name": "before-B.zip"}, {"name": "before-C.zip"}])
        return inst

    def start(self, inst, tag):
        # the entity/service comparison needs a real registry; this test is about the version bookkeeping
        with mock.patch.object(inst_mod.events, "emit"), \
                mock.patch.object(inst_mod.change_report, "snapshot", return_value={"entities": [], "services": []}):
            return asyncio.run(inst.start("demo", tag))

    def rec(self, inst):
        return inst.state.installed["demo"]

    def test_the_first_switch_records_the_version_that_runs(self):
        inst = self.installer()
        self.start(inst, B)
        self.assertEqual((self.rec(inst)["running_tag"], self.rec(inst)["previous_tag"]), (B, A))

    def test_a_second_switch_keeps_the_version_that_actually_ran(self):
        inst = self.installer()
        self.start(inst, B)
        self.start(inst, C)
        rec = self.rec(inst)
        self.assertEqual(rec["running_tag"], C)
        self.assertEqual(rec["previous_tag"], A, "B never ran: a rollback to it lands on code nobody booted")

    def test_the_backup_taken_before_the_version_that_ran_was_left_is_kept(self):
        inst = self.installer()
        self.start(inst, B)
        self.assertEqual(self.rec(inst)["pre_update_backup"], "before-B.zip")
        self.start(inst, C)
        self.assertEqual(self.rec(inst)["pre_update_backup"], "before-B.zip",
                         "the rollback target is A, so its backup is the one that belongs to it")

    def test_a_switch_after_a_restart_records_the_version_that_ran(self):
        """The guard must not freeze previous_tag: once the restart happened, the record is the truth again."""
        inst = self.installer()
        self.start(inst, B)
        inst._loaded_tags["demo"] = B  # the restart happened and B is what runs now
        self.start(inst, C)
        self.assertEqual(self.rec(inst)["previous_tag"], B)
