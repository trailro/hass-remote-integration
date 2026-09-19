"""Second external review, the boot path.

R2-01  the fallback added for F-08 was guarded by "no usable venv", which is also what a Python bump in
       the image leaves behind: on a volume that had been running 2026.10.1, one failed install of it
       (a network blip is enough) installed the image's own older default over a .storage that 2026.10.1
       had already migrated, rewrote desired to it, took no backup and scheduled no restore.  The extra
       attempt now happens only when nothing has ever run on the volume.
R2-09  the infinity of F-17, in ha.json this time: entrypoint._count raised OverflowError out of
       _prepare (the container died before Home Assistant started), and run.py's stop path raised it out
       of the STOP listener and out of the boot signal handler, so that stop never happened.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import run
from tests.fakes import entrypoint_for

INFINITY = float("inf")  # what json.load makes of 1e999, Infinity and -Infinity

RUNNING = "2026.10.1"  # what the operator runs on the volume
BAKED = "2026.8.3"  # what the image was built with: older


def _volume(test, ha_json=None, storage=True):
    """A /config with the manager's state dir, optionally a .storage and an ha.json."""
    cfg = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, cfg, True)
    os.makedirs(os.path.join(cfg, "integration_manager"))
    if storage:
        os.makedirs(os.path.join(cfg, ".storage"))
        with open(os.path.join(cfg, ".storage", "core.config_entries"), "w", encoding="utf-8") as fh:
            fh.write('{"version": 1}')
    if ha_json is not None:
        with open(os.path.join(cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            json.dump(ha_json, fh)
    return cfg


class UsedVolumeIsNotDowngradedTest(unittest.TestCase):
    """R2-01: an install that fails on a volume with a Home Assistant on it fails the boot, visibly."""

    def _prepare(self, cfg, installs, **env):
        """_prepare() against a volume whose venvs are all unusable (the image's Python moved on)."""
        ep = entrypoint_for(self, cfg, HA_VERSION_DEFAULT=BAKED, **env)
        tried = []

        def install(version):
            tried.append(version)
            return installs.get(version, False)

        done = {v for v, ok in installs.items() if ok}
        with mock.patch.object(ep, "latest_stable", return_value=RUNNING), \
                mock.patch.object(ep, "fits_this_python", return_value=True), \
                mock.patch.object(ep, "ensure_apt_packages", lambda state: None), \
                mock.patch.object(ep, "ensure_extra_requirements", lambda version: None), \
                mock.patch.object(ep, "apply_config_changes", side_effect=lambda state, wanted, current: wanted), \
                mock.patch.object(ep, "installed_versions", side_effect=lambda: sorted(done & set(tried))), \
                mock.patch.object(ep, "venv_ok", side_effect=lambda v: v in done and v in tried), \
                mock.patch.object(ep, "install", side_effect=install):
            try:
                ep._prepare()
                exit_code = None
            except SystemExit as err:
                exit_code = err.code
        return ep, tried, exit_code, ep.load_state()

    def test_a_failed_install_on_a_volume_that_has_run_ends_the_boot(self):
        # the probe from the review: tried ['2026.10.1', '2026.8.3'] ... current 2026.8.3 desired 2026.8.3
        cfg = _volume(self, {"current": RUNNING, "desired": RUNNING, "proven": RUNNING})
        ep, tried, exit_code, state = self._prepare(cfg, {BAKED: True})
        self.assertEqual(tried, [RUNNING], "the image's own older version is never installed over a used volume")
        self.assertEqual(exit_code, 1, "a failed boot the operator can see, not a silent downgrade")
        self.assertEqual(state.get("desired"), RUNNING, "the version the operator runs stays the wanted one")
        self.assertEqual(state.get("current"), RUNNING, "and nothing was recorded as installed")
        self.assertIn(RUNNING, state.get("last_error", ""))

    def test_the_storage_of_the_newer_version_is_left_alone(self):
        cfg = _volume(self, {"current": RUNNING, "desired": RUNNING, "proven": RUNNING})
        self._prepare(cfg, {BAKED: True})
        self.assertTrue(os.path.isfile(os.path.join(cfg, ".storage", "core.config_entries")))

    def test_an_ha_json_without_current_but_with_storage_is_not_fresh_either(self):
        # load_state drops a current it cannot read back (and _recovered_current finds nothing once no venv
        # passes venv_ok): the state file alone can say "fresh" for a volume full of configuration
        cfg = _volume(self, {"desired": RUNNING})
        ep, tried, exit_code, state = self._prepare(cfg, {BAKED: True})
        self.assertEqual(tried, [RUNNING])
        self.assertEqual(exit_code, 1)
        self.assertEqual(state.get("desired"), RUNNING)

    def test_a_version_in_ha_json_is_enough_without_a_storage(self):
        # installed but never booted: no .storage yet, still not a volume to put another version on
        cfg = _volume(self, {"current": RUNNING, "desired": RUNNING}, storage=False)
        ep, tried, exit_code, state = self._prepare(cfg, {BAKED: True})
        self.assertEqual(tried, [RUNNING])
        self.assertEqual(exit_code, 1)

    def test_a_previous_version_alone_also_counts_as_used(self):
        cfg = _volume(self, {"desired": RUNNING, "previous": BAKED}, storage=False)
        ep, tried, exit_code, state = self._prepare(cfg, {BAKED: True})
        self.assertEqual(tried, [RUNNING])
        self.assertEqual(exit_code, 1)

    def test_a_truly_fresh_volume_still_gets_the_image_default(self):
        # F-08 is not undone: no ha.json, no .storage, so there is nothing an older version could damage
        cfg = _volume(self, storage=False)
        ep, tried, exit_code, state = self._prepare(cfg, {BAKED: True})
        self.assertEqual(tried, [RUNNING, BAKED])
        self.assertIsNone(exit_code)
        self.assertEqual(state.get("desired"), BAKED)

    def test_a_working_older_venv_is_still_used_as_before(self):
        # the ordinary case: the volume has a venv this Python can run, so the boot goes on with it
        cfg = _volume(self, {"current": BAKED, "desired": RUNNING, "proven": BAKED})
        ep = entrypoint_for(self, cfg, HA_VERSION_DEFAULT=BAKED)
        tried = []
        with mock.patch.object(ep, "latest_stable", return_value=RUNNING), \
                mock.patch.object(ep, "fits_this_python", return_value=True), \
                mock.patch.object(ep, "ensure_apt_packages", lambda state: None), \
                mock.patch.object(ep, "ensure_extra_requirements", lambda version: None), \
                mock.patch.object(ep, "apply_config_changes", side_effect=lambda state, wanted, current: wanted), \
                mock.patch.object(ep, "installed_versions", return_value=[BAKED]), \
                mock.patch.object(ep, "venv_ok", side_effect=lambda v: v == BAKED), \
                mock.patch.object(ep, "install", side_effect=lambda v: tried.append(v) or False):
            ep._prepare()
        self.assertEqual(tried, [RUNNING])
        self.assertEqual(ep.load_state().get("desired"), BAKED, "running what is there is recorded, as before")


class InfiniteBootFailuresTest(unittest.TestCase):
    """R2-09, the entrypoint half: boot_failures is read before anything else happens."""

    def _entrypoint(self, raw):
        cfg = _volume(self, storage=False)
        with open(os.path.join(cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            fh.write(raw)
        return entrypoint_for(self, cfg)

    def test_an_infinite_count_reads_back_as_zero(self):
        ep = self._entrypoint('{"current": "2026.8.3", "desired": "2026.8.3", "boot_failures": 1e999}')
        state = ep.load_state()
        self.assertEqual(state["boot_failures"], INFINITY, "the file is read as it is; only the count is guarded")
        self.assertEqual(ep._count(state["boot_failures"]), 0)  # before the fix: OverflowError

    def test_the_json_word_infinity_is_the_same_case(self):
        ep = self._entrypoint('{"boot_failures": -Infinity}')
        self.assertEqual(ep._count(ep.load_state()["boot_failures"]), 0)

    def test_a_not_a_number_counts_as_zero_too(self):
        ep = self._entrypoint('{"boot_failures": NaN}')
        self.assertEqual(ep._count(ep.load_state()["boot_failures"]), 0)

    def test_the_boot_goes_on_and_the_count_is_written_back_as_a_number(self):
        # _prepare reads the count before it does anything: an OverflowError here killed the container
        # with no Home Assistant started and nothing in the UI to say why
        ep = self._entrypoint('{"current": "%s", "desired": "%s", "proven": "%s", "boot_failures": 1e999}'
                              % (BAKED, BAKED, BAKED))
        with mock.patch.object(ep, "fits_this_python", return_value=True), \
                mock.patch.object(ep, "ensure_apt_packages", lambda state: None), \
                mock.patch.object(ep, "ensure_extra_requirements", lambda version: None), \
                mock.patch.object(ep, "apply_config_changes", side_effect=lambda state, wanted, current: wanted), \
                mock.patch.object(ep, "installed_versions", return_value=[BAKED]), \
                mock.patch.object(ep, "venv_ok", side_effect=lambda v: v == BAKED), \
                mock.patch.object(ep, "install", side_effect=AssertionError("nothing to install")):
            ep._prepare()
        self.assertEqual(ep.load_state()["boot_failures"], 1, "counted from 0, and the infinity is gone")

    def test_an_ordinary_count_is_untouched(self):
        ep = self._entrypoint('{"boot_failures": 3}')
        self.assertEqual(ep._count(ep.load_state()["boot_failures"]), 3)


class InfiniteBootFailuresStopPathTest(unittest.TestCase):
    """R2-09, the run.py half: the same value on the way out of a boot."""

    def setUp(self):
        self.cfg = _volume(self, storage=False)
        self.path = os.path.join(self.cfg, "integration_manager", "ha.json")
        for patch in (mock.patch.object(run, "CONFIG_DIR", self.cfg), mock.patch.object(run, "_boot_settled", False)):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, raw):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(raw)

    def state(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_stop_path_survives_an_infinite_count(self):
        self.write('{"current": "2026.8.3", "boot_failures": 1e999}')
        with self.assertLogs(run._LOGGER, "WARNING") as logs:
            run._undo_boot_failure()  # before the fix: OverflowError out of the STOP listener / signal handler
        self.assertEqual(self.state()["boot_failures"], 0, "the bad value is replaced by a count")
        self.assertEqual(self.state()["current"], "2026.8.3", "the rest of ha.json is kept")
        self.assertIn("boot_failures", "\n".join(logs.output))

    def test_an_ordinary_count_still_goes_down_by_one(self):
        self.write('{"boot_failures": 3}')
        run._undo_boot_failure()
        self.assertEqual(self.state()["boot_failures"], 2)

    def test_the_undo_is_still_taken_only_once(self):
        self.write('{"boot_failures": 3}')
        run._undo_boot_failure()
        run._undo_boot_failure()
        self.assertEqual(self.state()["boot_failures"], 2)

    def test_a_signal_during_the_imports_leaves_the_file_alone(self):
        # _early_stop's own read of the count: an infinity there raised out of update_json into the blanket
        # except Exception, so the process still exited 0 - but with a warning nobody ever saw
        self.write('{"boot_failures": 1e999}')
        exits = []
        with mock.patch.dict(os.environ, {"HRI_CONFIG": self.cfg}), \
                mock.patch.object(os, "_exit", exits.append):
            run._early_stop(15, None)
        self.assertEqual(exits, [0], "the stop still exits cleanly")
        self.assertEqual(self.state()["boot_failures"], INFINITY, "nothing is written for a count it cannot read")

    def test_a_signal_during_the_imports_still_takes_an_ordinary_count_back(self):
        self.write('{"boot_failures": 2}')
        with mock.patch.dict(os.environ, {"HRI_CONFIG": self.cfg}), mock.patch.object(os, "_exit", lambda code: None):
            run._early_stop(15, None)
        self.assertEqual(self.state()["boot_failures"], 1)

    def test_marking_the_boot_ok_is_unaffected(self):
        self.write('{"boot_failures": 1e999, "current": "%s"}' % run.HA_VERSION)
        run._mark_boot_ok()
        self.assertEqual(self.state()["boot_failures"], 0)
        self.assertEqual(self.state()["proven"], run.HA_VERSION)


if __name__ == "__main__":
    unittest.main()
