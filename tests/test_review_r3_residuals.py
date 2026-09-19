"""Fourth external review: what the fixes of rounds two and three left open.

R3-08  the install *fallback* was made to require "nothing has ever run here", but the branch that
       *chooses* the version was not: with ha.json gone (or dropped as unreadable) and .storage full of
       configuration, `desired or current` is empty and the boot took the fresh-volume path - installing
       the image's own older default over a used volume and logging "fresh volume" while doing it.
R3-09  `_PIP_UNREACHABLE_RE` tells an offline pip from a genuinely missing wheel by the retry line above
       the error.  pip prints that line per failed attempt and carries on: a run that retried once and
       then got its answer carried it too, and the version check answered "could not check" - which lets
       an update through with nothing behind it.  A version list only an index can produce settles it.
R3-10  the health watchdog, twice: `_watchdog_save` wrote state.json before `restart()`, so ENOSPC ended
       the tick a minute before the restart, every minute; and `preflight.LOCK` / `_HA_LOCK` had no age
       limit, so a preflight that hung held the watchdog off for the rest of the process.
R3-11  the boot identity hand-over returned early for a deferred start that changed only the version of
       the domain already running (the identity does not carry the version), so the documents of entities
       the new version dropped stayed retained; and when the identity did change, the sweep ran before
       paho's CONNACK and answered 0 without looking.
"""

import asyncio
import errno
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import custom_components.integration_manager as im
from custom_components.integration_manager import patches, preflight
from tests.fakes import entrypoint_for
from tests.test_watchdog import _Base as WatchdogBase

RUNNING = "2026.10.1"  # what the operator runs on the volume
BAKED = "2026.8.3"  # what the image was built with: older


# ----- R3-08 ----------------------------------------------------------------


def _volume(test, ha_json=None, storage=True):
    cfg = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, cfg, True)
    os.makedirs(os.path.join(cfg, "integration_manager"))
    if storage:
        os.makedirs(os.path.join(cfg, ".storage"))
        with open(os.path.join(cfg, ".storage", "core.config_entries"), "w", encoding="utf-8") as fh:
            fh.write('{"version": 1}')
    if ha_json is not None:
        with open(os.path.join(cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            fh.write(ha_json if isinstance(ha_json, str) else json.dumps(ha_json))
    return cfg


class VersionChoiceOnAUsedVolumeTest(unittest.TestCase):
    """R3-08: no version recorded is not the same question as no volume."""

    def _prepare(self, cfg, *, on_volume=(), newest=None, **env):
        """_prepare() with `on_volume` the versions whose venvs are usable and `newest` PyPI's answer."""
        ep = entrypoint_for(self, cfg, HA_VERSION_DEFAULT=BAKED, **env)
        done, tried, pruned, logged = set(on_volume), [], [], []

        def install(version):
            tried.append(version)
            done.add(version)
            return True

        with mock.patch.object(ep, "latest_stable", return_value=newest), \
                mock.patch.object(ep, "fits_this_python", return_value=True), \
                mock.patch.object(ep, "ensure_apt_packages", lambda state: None), \
                mock.patch.object(ep, "ensure_extra_requirements", lambda version: None), \
                mock.patch.object(ep, "apply_config_changes", side_effect=lambda state, wanted, current: wanted), \
                mock.patch.object(ep, "installed_versions", side_effect=lambda: sorted(done)), \
                mock.patch.object(ep, "venv_ok", side_effect=lambda v: v in done), \
                mock.patch.object(ep, "prune", side_effect=lambda keep: pruned.append(keep)), \
                mock.patch.object(ep, "log", side_effect=logged.append), \
                mock.patch.object(ep, "install", side_effect=install):
            try:
                ep._prepare()
                exit_code = None
            except SystemExit as err:
                exit_code = err.code
        return SimpleNamespace(ep=ep, tried=tried, exit_code=exit_code, state=ep.load_state(),
                               pruned=pruned, log="\n".join(logged))

    def test_a_missing_ha_json_over_a_used_volume_is_not_a_fresh_volume(self):
        # the probe from the review: HA_VERSION_LATEST=0, so the fresh path had only the image default left
        cfg = _volume(self)
        res = self._prepare(cfg, on_volume=[RUNNING], HA_VERSION_LATEST="0")
        self.assertEqual(res.tried, [], "the venv on the volume is what this volume ran")
        self.assertEqual(res.state.get("current"), RUNNING)
        self.assertNotIn(BAKED, res.state.get("desired", ""))
        self.assertNotIn("fresh volume", res.log)

    def test_nothing_is_pruned_on_a_volume_whose_record_was_lost(self):
        cfg = _volume(self)
        res = self._prepare(cfg, on_volume=[RUNNING, BAKED], HA_VERSION_LATEST="0")
        self.assertEqual(res.pruned, [], "what the volume held is a guess: no venv is removed on this boot")

    def test_an_unreadable_ha_json_over_a_used_volume_is_not_fresh_either(self):
        cfg = _volume(self, ha_json="{not json")
        res = self._prepare(cfg, on_volume=[RUNNING], HA_VERSION_LATEST="0")
        self.assertEqual(res.tried, [])
        self.assertEqual(res.state.get("current"), RUNNING)

    def test_with_no_venv_left_it_installs_the_newest_release_not_the_image_default(self):
        # a Python bump took every venv with it: the newest release migrates .storage forward, the image's
        # own older default would be asked to read storage a newer Home Assistant has already migrated
        cfg = _volume(self)
        res = self._prepare(cfg, newest=RUNNING)
        self.assertEqual(res.tried, [RUNNING])
        self.assertIsNone(res.exit_code)
        self.assertNotIn("fresh volume", res.log)

    def test_with_no_venv_and_no_answer_from_pypi_the_boot_ends(self):
        cfg = _volume(self)
        res = self._prepare(cfg, HA_VERSION_LATEST="0")
        self.assertEqual(res.tried, [], "before the fix: ['2026.8.3'], over a populated .storage")
        self.assertEqual(res.exit_code, 1)
        self.assertIn("no Home Assistant version is recorded", res.state.get("last_error", ""))

    def test_pypi_unreachable_is_the_same_case(self):
        cfg = _volume(self)
        res = self._prepare(cfg, newest=None)  # latest_stable() answers None when PyPI cannot be reached
        self.assertEqual(res.tried, [])
        self.assertEqual(res.exit_code, 1)

    def test_a_truly_fresh_volume_still_gets_the_image_default(self):
        # F-08 and R2-01 are not undone: no ha.json, no .storage, nothing an older version could damage
        cfg = _volume(self, storage=False)
        res = self._prepare(cfg, HA_VERSION_LATEST="0")
        self.assertEqual(res.tried, [BAKED])
        self.assertIsNone(res.exit_code)
        self.assertIn("fresh volume", res.log)

    def test_a_fresh_volume_still_prefers_the_newest_release(self):
        cfg = _volume(self, storage=False)
        res = self._prepare(cfg, newest=RUNNING)
        self.assertEqual(res.tried, [RUNNING])
        self.assertIn("fresh volume", res.log)

    def test_a_recorded_version_is_still_what_decides(self):
        cfg = _volume(self, {"current": RUNNING, "desired": RUNNING, "proven": RUNNING})
        res = self._prepare(cfg, on_volume=[RUNNING], HA_VERSION_LATEST="0")
        self.assertEqual(res.tried, [])
        self.assertEqual(res.state.get("current"), RUNNING)
        self.assertNotEqual(res.pruned, [], "an intact record prunes as before")


# ----- R3-09 ----------------------------------------------------------------
#
# Captured from pip 25 in this image, not written from memory:
#
#   pip install --dry-run --no-deps --only-binary=:all: \
#       --extra-index-url http://127.0.0.1:1/simple requests==99.99.99
#   pip install --dry-run --no-deps --only-binary=:all: \
#       hri-no-such-package-xyzzy==1.0                      (index reachable)
#   PIP_INDEX_URL=https://127.0.0.1:1/simple pip install ... requests==2.32.5

RETRY = ("WARNING: Retrying (Retry(total=0, connect=None, read=None, redirect=None, status=None)) after connection "
         "broken by 'NewConnectionError(\"HTTPConnection(host=\\'127.0.0.1\\', port=1): Failed to establish a new "
         "connection: [Errno 111] Connection refused\")': /simple/requests/")
ANSWERED = ("ERROR: Could not find a version that satisfies the requirement requests==99.99.99 "
            "(from versions: 2.0.0, 2.31.0, 2.32.5, 2.34.2)\n"
            "ERROR: No matching distribution found for requests==99.99.99")
NO_SUCH = ("ERROR: Could not find a version that satisfies the requirement hri-no-such-package-xyzzy==1.0 "
           "(from versions: none)\nERROR: No matching distribution found for hri-no-such-package-xyzzy==1.0")
OFFLINE = ("ERROR: Could not find a version that satisfies the requirement requests==2.32.5 (from versions: none)\n"
           "ERROR: No matching distribution found for requests==2.32.5")


class PipUnreachableTest(unittest.TestCase):
    """R3-09: one retry line no longer turns a real answer into "could not check"."""

    def test_a_retry_line_with_a_version_list_is_an_index_that_answered(self):
        self.assertFalse(preflight._pip_unreachable(RETRY + "\n" + ANSWERED))

    def test_a_retry_line_with_no_version_list_is_an_index_that_did_not(self):
        self.assertTrue(preflight._pip_unreachable(RETRY + "\n" + OFFLINE))

    def test_no_retry_line_at_all_is_never_unreachable(self):
        self.assertFalse(preflight._pip_unreachable(NO_SUCH))
        self.assertFalse(preflight._pip_unreachable(ANSWERED))

    def test_an_empty_list_is_not_taken_for_an_answer(self):
        self.assertTrue(preflight._pip_unreachable(RETRY + "\nERROR: ... (from versions: none)"))

    def test_the_other_shapes_of_no_route_still_count(self):
        for line in ("Temporary failure in name resolution", "Read timed out", "ProxyError", "SSLError",
                     "Failed to establish a new connection"):
            with self.subTest(line=line):
                self.assertTrue(preflight._pip_unreachable(f"WARNING: {line}\n" + OFFLINE))


class HaVersionCheckRetryTest(unittest.TestCase):
    """The same thing where it decides something: the Home Assistant version check."""

    @staticmethod
    def _proc(stderr, returncode=1, stdout=""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_a_release_that_is_not_on_pypi_is_still_named_after_a_retry(self):
        err = RETRY + "\nERROR: Could not find a version that satisfies the requirement homeassistant==2099.1.0 " \
                      "(from versions: 2026.8.3, 2026.10.1)\nERROR: No matching distribution found"
        with mock.patch.object(preflight, "_pip_no_deps", return_value=self._proc(err)):
            pins, why = preflight._ha_pins("python", "2099.1.0")
        self.assertEqual(pins, [], "the index listed its releases: that answer is about the release, not the network")
        self.assertIn("no such Home Assistant release", why)

    def test_a_retry_with_nothing_behind_it_is_still_unreachable(self):
        with mock.patch.object(preflight, "_pip_no_deps", return_value=self._proc(RETRY + "\n" + OFFLINE)):
            pins, why = preflight._ha_pins("python", "2026.10.1")
        self.assertIsNone(pins)
        self.assertIn("PyPI could not be reached", why)

    def test_a_pin_without_a_wheel_is_a_blocker_even_when_a_request_was_retried(self):
        pins = json.dumps({"install": [{"metadata": {"requires_dist": ["aiohttp==3.14.0", "attrs==25.1.0"]}}]})

        def pip(_python, reqs):
            if reqs == ["homeassistant==2026.10.1"]:
                return self._proc("", returncode=0, stdout=pins)
            if any(r.startswith("aiohttp") for r in reqs):
                return self._proc(RETRY + "\nERROR: Could not find a version that satisfies the requirement "
                                          "aiohttp==3.14.0 (from versions: 3.13.0, 3.13.1)")
            return self._proc("", returncode=0, stdout="{}")

        with mock.patch.object(preflight, "_pip_no_deps", side_effect=pip):
            report = preflight._ha_wheel_check("python", "2026.10.1")
        self.assertTrue(report["checked"], "before the fix: checked False, and the update went out unchecked")
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing"], ["aiohttp==3.14.0"])

    def test_a_run_that_never_reached_an_index_is_still_could_not_check(self):
        pins = json.dumps({"install": [{"metadata": {"requires_dist": ["aiohttp==3.14.0"]}}]})
        calls = [self._proc(pins, returncode=0, stdout=pins), self._proc(RETRY + "\n" + OFFLINE)]
        with mock.patch.object(preflight, "_pip_no_deps", side_effect=lambda *a: calls.pop(0)):
            report = preflight._ha_wheel_check("python", "2026.10.1")
        self.assertFalse(report["checked"])
        self.assertTrue(report["ok"], "a check that did not run never refuses a version")
        self.assertIn("PyPI could not be reached", report["notes"][0])


# ----- R3-10 ----------------------------------------------------------------


def _full_disk(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


class WatchdogOnAFullDiskTest(WatchdogBase):
    """R3-10a: the restart the comment at restart() promises actually happens."""

    def test_a_full_disk_no_longer_stops_the_tick_before_the_restart(self):
        inst = self.installer()
        inst._save_state = _full_disk
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_awaited_once()  # before the fix: OSError out of every tick, forever

    def test_the_record_is_kept_in_memory_so_the_cap_still_applies_this_process(self):
        inst = self.installer(watchdog_max_per_day=1)
        inst._save_state = _full_disk
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        self.assertEqual(inst.watchdog_record()["attempts"], 1)
        inst.restart.reset_mock()
        for _ in range(600):
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("is the maximum"), "the daily cap is still enforced from memory")

    def test_the_full_disk_is_said_once_per_write_not_swallowed(self):
        inst = self.installer()
        inst._save_state = _full_disk
        with self.assertLogs("custom_components.integration_manager.installer", "WARNING") as logs:
            inst._watchdog_save(inst.watchdog_record())
        self.assertIn("No space left on device", "\n".join(logs.output))

    def test_a_writable_disk_still_writes_the_record(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        with open(inst.state_file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["watchdog"]["attempts"], 1)


class PreflightLockAgeTest(unittest.IsolatedAsyncioTestCase):
    """R3-10b: a preflight that hung is not one that is running."""

    async def test_a_held_lock_holds_the_watchdog_off(self):
        lock = preflight._TimedLock("a preflight")
        async with lock:
            self.assertTrue(lock.locked())
            self.assertLess(lock.held_s(), 1)

    async def test_a_lock_held_past_the_maximum_does_not(self):
        lock = preflight._TimedLock("a preflight")
        async with lock:
            lock._taken_at -= preflight.LOCK_MAX_HOLD_S + 1
            self.assertGreater(lock.held_s(), preflight.LOCK_MAX_HOLD_S)
            self.assertFalse(lock.locked(), "before the fix: True for the rest of the process")

    async def test_mutual_exclusion_is_untouched_by_the_age(self):
        lock = preflight._TimedLock("a preflight")
        await lock.acquire()
        lock._taken_at -= preflight.LOCK_MAX_HOLD_S + 1
        second = asyncio.ensure_future(lock.acquire())
        await asyncio.sleep(0)
        self.assertFalse(second.done(), "acquire still waits for the holder, however long it has held it")
        lock.release()
        await second
        self.assertTrue(lock.locked(), "and the new holder starts its own clock")
        lock.release()

    async def test_the_lock_is_free_again_after_the_release(self):
        lock = preflight._TimedLock("a preflight")
        async with lock:
            pass
        self.assertFalse(lock.locked())
        self.assertIsNone(lock.held_s())

    async def test_both_preflight_locks_are_timed(self):
        for lock in (preflight.LOCK, preflight._HA_LOCK):
            self.assertIsInstance(lock, preflight._TimedLock)

    @staticmethod
    def _scheduler():
        """As tests.test_review_r2_mqtt: everything the live check asks about answers "clear"."""
        import time as _time

        from custom_components.integration_manager.scheduler import Scheduler, WATCHDOG_BOOT_GRACE_S

        inst = SimpleNamespace(state=SimpleNamespace(domain="demo", pending_smoke=None, pending_start=None,
                                                     pending_rollback=None),
                               busy=False, smoke={}, manager=None, _entries_of=lambda _domain: [])
        sch = Scheduler.__new__(Scheduler)
        sch.hass = SimpleNamespace(is_running=True)
        sch.installer = inst
        sch._started = _time.monotonic() - WATCHDOG_BOOT_GRACE_S - 1
        return sch

    async def test_the_watchdog_stops_standing_down_for_a_hung_preflight(self):
        for lock in (preflight.LOCK, preflight._HA_LOCK):
            with self.subTest(lock=lock._what):
                sch = self._scheduler()
                await lock.acquire()
                try:
                    self.assertIn("preflight", sch._live_refusal() or "")
                    lock._taken_at -= preflight.LOCK_MAX_HOLD_S + 1
                    # before the fix: the same refusal for the rest of the process
                    self.assertIsNone(sch._live_refusal())
                finally:
                    lock.release()


# ----- R3-11 ----------------------------------------------------------------


class _Stats(dict):
    """The publisher's stats, with the CONNACK landing after ``after`` reads of it: paho sets the flag on
    its own thread, some time after connect_async returned."""

    def __init__(self, after: int):
        super().__init__(connected=False, connect_error="")
        self._after, self._reads = after, 0

    def get(self, key, default=None):
        if key == "connected" and not self["connected"]:
            self._reads += 1
            if self._reads >= self._after:
                self["connected"] = True
        return dict.get(self, key, default)


class FakePublisher:
    """What async_hand_identity_over reads: the stats dict and the two coroutines."""

    def __init__(self, enabled=True, connect_after=1, cleared=3):
        self.config = SimpleNamespace(enabled=enabled)
        self.stats = _Stats(connect_after)
        self.calls = []
        self._cleared = cleared

    async def async_after_start(self, res):
        self.calls.append(("after_start", res))

    async def async_clear_stale_docs(self):
        self.calls.append(("clear", None))
        return self._cleared if self.stats["connected"] else 0


class IdentityHandOverTest(unittest.IsolatedAsyncioTestCase):
    """R3-11: the two starts the hand-over walked past."""

    def setUp(self):
        self.installer = SimpleNamespace(instance_key="hass_demo")
        patch = mock.patch.object(im, "CONNACK_POLL_S", 0, create=True)  # the wait is real, the sleep between reads is not
        patch.start()
        self.addCleanup(patch.stop)

    async def _hand_over(self, publisher, before, started):
        await im.async_hand_identity_over(self.installer, publisher, before, started)

    async def test_a_version_switch_of_the_same_domain_is_handed_over(self):
        pub = FakePublisher()
        await self._hand_over(pub, "hass_demo", {"pre_update_backup": "b.zip"})
        self.assertEqual([k for k, _ in pub.calls], ["after_start", "clear"],
                         "before the fix: nothing, because the identity did not move")

    async def test_the_stale_documents_of_that_switch_are_cleared(self):
        pub = FakePublisher()
        started = {"pre_update_backup": "b.zip"}
        await self._hand_over(pub, "hass_demo", started)
        self.assertEqual(started.get("stale_docs_cleared"), 3, "before the fix: async_after_start was never called")

    async def test_a_start_that_moves_the_identity_waits_for_the_connack(self):
        pub = FakePublisher(connect_after=3)
        started = {"pre_update_backup": "b.zip"}
        await self._hand_over(pub, "hass_other", started)
        self.assertEqual(started.get("stale_docs_cleared"), 3, "before the fix: absent, the sweep ran before the CONNACK")
        self.assertEqual([k for k, _ in pub.calls], ["after_start", "clear"])

    async def test_an_unchanged_identity_without_a_version_switch_still_returns_early(self):
        pub = FakePublisher()
        await self._hand_over(pub, "hass_demo", {})
        self.assertEqual(pub.calls, [], "nothing moved and nothing was replaced: there is nothing to hand over")

    async def test_a_publisher_that_already_cleared_is_not_asked_twice(self):
        pub = FakePublisher()
        pub.stats["connected"] = True

        async def after_start(res):
            pub.calls.append(("after_start", res))
            res["stale_docs_cleared"] = 7

        pub.async_after_start = after_start
        started = {"pre_update_backup": "b.zip"}
        await self._hand_over(pub, "hass_other", started)
        self.assertEqual(started.get("stale_docs_cleared"), 7)
        self.assertEqual([k for k, _ in pub.calls], ["after_start"])

    async def test_mqtt_switched_off_is_not_waited_for(self):
        pub = FakePublisher(enabled=False, connect_after=10 ** 9)
        started = {"pre_update_backup": "b.zip"}
        await self._hand_over(pub, "hass_other", started)
        self.assertEqual([k for k, _ in pub.calls], ["after_start"])
        self.assertNotIn("stale_docs_cleared", started)

    async def test_a_refused_connection_is_not_waited_for_either(self):
        pub = FakePublisher(connect_after=10 ** 9)
        pub.stats["connect_error"] = "base topic hass_demo already carries retained topics that are not ours"
        started = {"pre_update_backup": "b.zip"}
        await self._hand_over(pub, "hass_other", started)
        self.assertEqual([k for k, _ in pub.calls], ["after_start"])

    async def test_a_broker_that_never_answers_gives_up_and_the_boot_goes_on(self):
        pub = FakePublisher(connect_after=10 ** 9)
        started = {"pre_update_backup": "b.zip"}
        with mock.patch.object(im, "CONNACK_WAIT_S", 0.0, create=True):
            await self._hand_over(pub, "hass_other", started)
        self.assertEqual([k for k, _ in pub.calls], ["after_start"])

    async def test_a_broker_error_never_costs_the_boot_its_ui(self):
        pub = FakePublisher()
        pub.async_after_start = mock.AsyncMock(side_effect=OSError("broker unreachable"))
        with self.assertLogs("custom_components.integration_manager", "ERROR"):
            await self._hand_over(pub, "hass_other", {"pre_update_backup": "b.zip"})


# ----- R3-15 (the .py half; the diff half extends test_review_r2_patches) ----


class ModulePatchScopeTest(unittest.TestCase):
    """A .py patch module does not say what it edits: its applies-to header does."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hri-r3-patches-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.comp = os.path.join(self.root, "custom_components", "demo")
        self.site = os.path.join(self.root, "site-packages")
        os.makedirs(self.comp)
        os.makedirs(self.site)
        self.ctx = patches.PatchContext(self.root, "demo", self.site, self.comp)

    def test_a_module_that_names_an_installed_distribution_reaches_outside(self):
        text = "# applies-to: some-lib<2.0\n# integration-version: 1.0.0\ndef status(ctx): return 'applied'\n"
        self.assertTrue(patches._reaches_site_packages("fix.py", text, self.ctx))

    def test_a_module_that_names_nothing_is_taken_for_the_integrations_own_code(self):
        text = "# integration-version: 1.0.0\ndef status(ctx): return 'applied'\n"
        self.assertFalse(patches._reaches_site_packages("fix.py", text, self.ctx))

    def test_a_diff_that_does_not_parse_reaches_nothing(self):
        self.assertFalse(patches._reaches_site_packages("x.patch", "--- a/x.py\n+++ b/x.py\n@@ -1,9 +1,9 @@\n a\n", self.ctx))

    def test_a_diff_whose_target_is_gone_reaches_nothing(self):
        text = "--- a/lib/gone.py\n+++ b/lib/gone.py\n@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\n"
        self.assertFalse(patches._reaches_site_packages("x.patch", text, self.ctx))


if __name__ == "__main__":
    unittest.main()
