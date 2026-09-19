"""Review round 3, finding 12: a crash fallback whose restore applied but whose state was never saved.

After a crash fallback, restore_after_failed_change keeps ``recovery`` in ha.json and schedules the
configuration from before the switch for the version that is fallen back to.  The next boot applies that
restore, records the outcome (before the schedule is removed) and only then drops ``recovery``.  Killed in
between, the boot after that found no schedule any more: it read the still-present ``recovery`` as a restore
that had not happened and started the crashed version again - on the configuration the restore had just put
back, which that version then migrates forward.  These tests boot the second time from exactly the state
record() saved and check that the fallback stays where it is, that a restore that really failed still keeps
the crashed version, and that a restore recorded for another backup or before this fallback was planned is
not mistaken for it.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for

CRASHED = "2026.9.2"   # the version that crashes at boot: the fallback runs away from it
FALLBACK = "2026.8.3"  # the version fallen back to, whose configuration comes back from the pre-change backup
BEFORE_EVERYTHING = "2000-01-01T00:00:00"  # older than any timestamp this boot writes, whatever the clock says


def _make_venv(cfg, version):
    venv = os.path.join(cfg, f"venv-{version}")
    pkg = os.path.join("lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant")
    for folder in ("bin", pkg):
        os.makedirs(os.path.join(venv, folder))
    for marker in (".ok", "bin/python", os.path.join(pkg, "__init__.py")):
        open(os.path.join(venv, marker), "w").close()


class FallbackRestoreRaceTest(unittest.TestCase):

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        for d in (".storage", backupkit.STATE_DIR, "custom_components/x"):
            os.makedirs(os.path.join(self.cfg, d))
        with open(os.path.join(self.cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
            fh.write("{}")
        with open(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"current": CRASHED}, fh)
        for version in (CRASHED, FALLBACK):
            _make_venv(self.cfg, version)
        self.ep = entrypoint_for(self, self.cfg)
        self.saved = []

    def _storage(self, text=None):
        path = os.path.join(self.cfg, ".storage", "core.config_entries")
        if text is None:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return text

    def _save(self, state):
        self.saved.append(copy.deepcopy(state))
        return True

    def _boot(self, state, wanted=FALLBACK, current=CRASHED):
        """One apply_config_changes, with every ha.json write captured as it was made."""
        with mock.patch.object(self.ep, "log"), mock.patch.object(self.ep, "save_state", side_effect=self._save):
            return self.ep.apply_config_changes(state, wanted, current)

    def _plan_fallback(self):
        """What the boot that gives up on CRASHED leaves behind: the backup from before the switch, the storage
        CRASHED wrote, and the schedule plus ``recovery`` of restore_after_failed_change."""
        self._storage(f"the configuration of {FALLBACK}")
        backup = backupkit.create(self.cfg, "pre-ha", FALLBACK)["name"]
        self._storage(f"migrated by {CRASHED}")
        state = {"current": CRASHED, "desired": FALLBACK, "fallback_from": CRASHED,
                 "change": {"to": CRASHED, "mode": "keep", "backup": backup, "parts": ["storage"],
                            "applied": True, "at": "2026-09-19T10:00:00"}}
        with mock.patch.object(self.ep, "log"):
            self.assertTrue(self.ep.restore_after_failed_change(state, CRASHED, FALLBACK))
        self.assertTrue(backupkit.pending(self.cfg))
        return state, backup

    def _killed(self):
        """The state as record() saved it: the outcome of the restore is durable, ``recovery`` is still in it."""
        self.assertEqual(len(self.saved), 1, "the restore's outcome was written exactly once")
        killed = self.saved[0]
        self.assertTrue(killed["last_restore"]["ok"])
        self.assertIn("recovery", killed)
        return killed

    def test_a_restore_applied_before_the_kill_keeps_the_boot_on_the_fallback(self):
        state, _ = self._plan_fallback()
        self.assertEqual(self._boot(state), FALLBACK)
        self.assertNotIn("recovery", state)
        self.assertEqual(self._storage(), f"the configuration of {FALLBACK}")
        killed = self._killed()
        self.assertFalse(backupkit.pending(self.cfg), "the schedule is gone: the next boot finds no restore to apply")

        self.assertEqual(self._boot(killed), FALLBACK, "the crashed version was started on the restored configuration")
        self.assertNotIn("recovery", killed)
        self.assertNotEqual(killed.get("desired"), CRASHED)
        self.assertEqual(self._storage(), f"the configuration of {FALLBACK}")

    def test_a_recovery_without_a_timestamp_still_counts_its_restore(self):
        state, _ = self._plan_fallback()
        self.assertEqual(self._boot(state), FALLBACK)
        killed = self._killed()
        killed["recovery"].pop("at", None)  # written by a version of the manager that recorded none

        self.assertEqual(self._boot(killed), FALLBACK)
        self.assertNotIn("recovery", killed)

    def test_a_fallback_restore_that_really_failed_stays_on_the_crashed_version(self):
        state, _ = self._plan_fallback()
        with open(backupkit.pending_archive(self.cfg), "r+b") as fh:
            fh.truncate(64)  # a torn copy: the restore fails validation, nothing on the volume is touched

        self.assertEqual(self._boot(state), CRASHED)
        self.assertEqual(state["desired"], CRASHED)
        self.assertIn("recovery", state, "the configuration still has to come back at the next fallback")
        self.assertFalse(state["last_restore"]["ok"])
        self.assertEqual(self._storage(), f"migrated by {CRASHED}")

    def test_a_restore_of_another_backup_is_not_this_fallback_s_restore(self):
        state, _ = self._plan_fallback()
        backupkit.cancel_restore(self.cfg)  # the schedule was dropped: this fallback's restore never ran
        state["last_restore"] = {"at": BEFORE_EVERYTHING, "ok": True, "for_version": FALLBACK,
                                 "parts": ["storage"], "backup": "20260101-000000-pre-ha.zip"}

        self.assertEqual(self._boot(state), CRASHED)
        self.assertEqual(state["desired"], CRASHED)
        self.assertIn("recovery", state)

    def test_a_restore_of_the_same_backup_from_before_this_fallback_does_not_count(self):
        state, backup = self._plan_fallback()
        backupkit.cancel_restore(self.cfg)
        state["last_restore"] = {"at": BEFORE_EVERYTHING, "ok": True, "for_version": FALLBACK,
                                 "parts": ["storage"], "backup": backup}

        self.assertEqual(self._boot(state), CRASHED)
        self.assertEqual(state["desired"], CRASHED)
        self.assertIn("recovery", state)


if __name__ == "__main__":
    unittest.main()
