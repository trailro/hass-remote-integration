"""E2E campaign 0.17.0, integration lifecycle.

mb F1: Stop was accepted while a full rollback's restore waited for the restart; the restore brought the entries
back enabled and the integration ran with the manager recording nothing.  Stop, uninstall and removing a version
the rollback involves are refused now, and a boot that finds an installed integration with enabled entries while
nothing is recorded as running adopts it.
mb F2: a second full rollback advised Cancel restore, which is refused for a rollback's restore.
mb F3: starting a tag that is not in the store answered needs_force ("no manifest.json").
mb F5: a degraded version was kept, but its change report was dropped.
mb F7: the flow API answered 500 for an unknown config entry id.
ud 9: the "ha.json was corrupt" notification came back after every restore (the marker lived in state.json).
ud 6: an unparseable registry.json became {} without a log line."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
import jsonio
from homeassistant.config_entries import UnknownEntry

import custom_components.integration_manager as manager
from custom_components.integration_manager import flows as flows_mod, installer as inst_mod, preflight, views
from custom_components.integration_manager.installer import Installer, State
from tests import test_r13_life_rollback as r13


class _RollbackScheduled(unittest.TestCase):
    def setUp(self):
        r13.FullRollbackVersusCancelRestoreTest.setUp(self)
        res = asyncio.run(self.inst.rollback_full("demo"))
        self.assertTrue(res["ok"], res)
        self.assertTrue(backupkit.pending(self.cfg))
        self.assertEqual((self.inst.state.domain, self.inst.running_tag), ("demo", "v1"))

    def assert_rollback_refusal(self, res):
        self.assertFalse(res["ok"], res)
        self.assertIn("full rollback", res["error"])
        self.assertIn("restart to finish it", res["error"])
        self.assertIn("start demo v2 again to undo it", res["error"])
        self.assertNotIn("cancel", res["error"], "Cancel restore is refused for a rollback's restore")
        self.assertFalse(self.inst.busy)
        self.assertTrue(backupkit.pending(self.cfg), "the rollback's restore stays scheduled")


class ActionsDuringAFullRollbackTest(_RollbackScheduled):
    def test_stop_is_refused(self):
        res = asyncio.run(self.inst.stop())
        self.assert_rollback_refusal(res)
        self.assertEqual(self.inst.state.domain, "demo")
        self.assertEqual(self.inst.state.pending_smoke, {"domain": "demo", "tag": "v1", "can_rollback": False},
                         "the verdict after the rollback's restart is still scheduled")

    def test_uninstall_is_refused(self):
        res = asyncio.run(self.inst.uninstall("demo"))
        self.assert_rollback_refusal(res)
        self.assertIn("demo", self.inst.state.installed)

    def test_removing_a_version_the_rollback_involves_is_refused(self):
        for tag in ("v1", "v2"):  # the version it goes back to, and the one starting again undoes it
            with self.subTest(tag=tag):
                self.assert_rollback_refusal(asyncio.run(self.inst.remove_version("demo", tag)))
                self.assertIn(tag, self.inst.state.installed["demo"]["versions"])

    def test_a_second_full_rollback_gives_advice_that_works(self):
        self.assert_rollback_refusal(asyncio.run(self.inst.rollback_full("demo")))

    def test_install_names_the_rollback(self):
        self.assert_rollback_refusal(asyncio.run(self.inst.install("v3", "demo")))

    def test_without_the_undo_the_answer_only_says_restart(self):
        self.inst._rollback_undo = None  # a rollback whose way back cannot be started again
        res = asyncio.run(self.inst.stop())
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "a full rollback restores its backup at the next restart: restart to finish it")

    def test_undone_rollback_no_longer_refuses(self):
        self.assertTrue(asyncio.run(self.inst.start("demo", "v2"))["ok"])
        self.assertIsNone(self.inst.rollback_restore_refusal())

    def test_a_restore_by_hand_is_not_a_rollback(self):
        self.inst.state.rollback_backup = None
        self.assertIsNone(self.inst.rollback_restore_refusal())
