"""The health watchdog: an "error" verdict that lasts restarts the process, within
strict limits, and says so everywhere.

Time is a fake clock (no sleeps): every test drives the scheduler's tick by hand and
moves the clock between ticks, so a window elapses exactly when the test says it does.
The refusal cases each stage one thing the manager is doing or has scheduled and check
that the tick keeps its hands off; the backoff, the rate cap and the give-up are checked
across a simulated restart of the manager, on the same state.json."""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import scheduler as scheduler_mod
from custom_components.integration_manager.installer import Installer, State
from custom_components.integration_manager.scheduler import Scheduler, WATCHDOG_BOOT_GRACE_S
from custom_components.integration_manager.settings import DEFAULTS, Settings

DOMAIN = "demo"
ERROR = {"state": "error", "reason": "config entry 'Hub' is setup_error: no route to host"}
OK = {"state": "ok", "reason": ""}


class Clock:
    """One clock for time.time() and time.monotonic(): the 24 h ledger and the
    unhealthy stretch move together, which is what really happens."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def __call__(self) -> float:
        return self.t


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = Clock()
        for name in ("time", "monotonic"):
            patcher = mock.patch(f"time.{name}", self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.emitted = []
        patcher = mock.patch.object(scheduler_mod.events, "emit", lambda kind, msg, **d: self.emitted.append((kind, msg)))
        patcher.start()
        self.addCleanup(patcher.stop)
        from custom_components.integration_manager import installer as installer_mod

        patcher = mock.patch.object(installer_mod.events, "emit", lambda kind, msg, **d: self.emitted.append((kind, msg)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.verdict = dict(ERROR)
        quiet = logging.getLogger("custom_components.integration_manager")
        was = quiet.level
        quiet.setLevel(logging.CRITICAL)  # the watchdog logs what it refuses and what it does: not on the test output
        self.addCleanup(quiet.setLevel, was)

    # ----- the pieces under test, wired by hand -----------------------------

    def installer(self, **settings):
        """A real Installer on a real (empty) volume: state.json is written and read
        back exactly as it is in the container."""
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.dir, components=set()), is_running=True,
                               async_add_executor_job=self._run_job, data={})
        inst = Installer.__new__(Installer)
        inst.hass = hass
        inst.config_dir = self.dir
        inst.state_dir = os.path.join(self.dir, "integration_manager")
        inst.state_file = os.path.join(inst.state_dir, "state.json")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        os.makedirs(inst.versions_dir, exist_ok=True)
        inst.settings = Settings(inst.state_dir)
        inst.settings.data.update({"watchdog": True, **settings})
        inst.state = inst._load_state()
        inst.state.domain = DOMAIN
        inst.state.installed = {DOMAIN: {"versions": {"1.0": {}}, "running_tag": "1.0"}}
        inst.busy = False
        inst.watchdog_pending = None
        inst._smoke_pending = None
        inst.health_source = lambda grace=True: self.verdict
        inst._entries_of = lambda dom: [SimpleNamespace(state=SimpleNamespace(value="loaded"), disabled_by=None, title="Hub")]
        inst.restart = mock.AsyncMock(return_value={"ok": True})
        # no reload path (reload_entry stays None): these tests cover the restart step, which is where a YAML-only
        # integration (nothing to reload) starts.  The reload step before it: test_health_zombie.LadderTest
        inst._rollback_undo = None
        inst.rollback_restore_refusal = lambda: None
        return inst

    @staticmethod
    async def _run_job(func, *args):
        return func(*args)

    def scheduler(self, inst):
        sch = Scheduler.__new__(Scheduler)
        sch.hass = inst.hass
        sch.installer = inst
        sch._unsub = sch._retry = sch._boot = sch._watchdog_unsub = None
        sch._started = self.clock() - WATCHDOG_BOOT_GRACE_S - 1  # past the boot grace unless a test says otherwise
        sch._bad_since = None
        sch._refused = False
        sch._reloaded_at = sch._ok_since = None
        sch._reload_skip_said = False
        sch._setup_since = None
        sch._announced = True  # the boot notification has its own test
        # nothing else is scheduled on the volume: the blocking checks answer "clear"
        sch._scheduled_refusal = lambda: None
        return sch

    def tick(self, sch, seconds: float = 60):
        """One watchdog tick, `seconds` after the previous one."""
        self.clock.advance(seconds)
        asyncio.run(sch._watchdog_tick())

    def lines(self, needle):
        return [m for _k, m in self.emitted if needle in m]


class DefaultsTest(_Base):
    def test_off_by_default(self):
        self.assertIs(DEFAULTS["watchdog"], False)
        self.assertEqual((DEFAULTS["watchdog_after_min"], DEFAULTS["watchdog_min_interval_min"],
                          DEFAULTS["watchdog_max_per_day"]), (15, 60, 3))

    def test_a_tick_with_the_setting_off_never_looks_at_the_verdict(self):
        inst = self.installer(watchdog=False)
        inst.health_source = mock.Mock(side_effect=AssertionError("the verdict must not be built"))
        sch = self.scheduler(inst)
        for _ in range(60):
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.assertIsNone(inst.state.watchdog)


class TimerTest(_Base):
    """One timer, armed with the boot check and cancelled with the others."""

    def test_the_tick_is_armed_once_by_the_boot_check_and_cancelled_on_stop(self):
        inst = self.installer(release_check=False)  # the boot check's other half is not under test here
        sch = self.scheduler(inst)
        sch._watchdog_unsub = None
        unsub = mock.Mock()
        with mock.patch.object(scheduler_mod, "async_track_time_interval", return_value=unsub) as track:
            asyncio.run(sch._boot_check(None))
            asyncio.run(sch._boot_check(None))
        track.assert_called_once()
        self.assertEqual(track.call_args.args[1], sch._watchdog_tick)
        self.assertEqual(track.call_args.args[2].total_seconds(), scheduler_mod.WATCHDOG_TICK_S)
        sch._on_stop(None)
        unsub.assert_called_once()
        self.assertIsNone(sch._watchdog_unsub)


class WindowTest(_Base):
    def test_error_must_last_the_whole_window_before_it_acts(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        for _ in range(15):  # 15 ticks a minute apart: the first one only starts the clock
            self.tick(sch)
            inst.restart.assert_not_awaited()
        self.tick(sch)
        inst.restart.assert_awaited_once()
        self.assertEqual(inst.watchdog_record()["attempts"], 1)

    def test_a_recovery_inside_the_window_cancels_it(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        for _ in range(10):
            self.tick(sch)
        self.verdict = dict(OK)
        self.tick(sch)
        self.assertIsNone(sch._bad_since)
        self.verdict = dict(ERROR)
        for _ in range(15):
            self.tick(sch)
        inst.restart.assert_not_awaited()  # the stretch started again from the recovery
        self.tick(sch)
        inst.restart.assert_awaited_once()

    def test_degraded_stopped_and_unconfigured_are_never_acted_on(self):
        for state in ("degraded", "stopped", "unconfigured"):
            with self.subTest(state=state):
                inst = self.installer()
                sch = self.scheduler(inst)
                self.verdict = {"state": state, "reason": "whatever"}
                for _ in range(120):
                    self.tick(sch)
                inst.restart.assert_not_awaited()
                self.assertIsNone(inst.watchdog_pending)

    def test_an_integration_that_was_never_configured_is_left_alone(self):
        """error, but the reason is that nothing configured it: a restart cannot fix that (build_health never
        answers the smoke test's "unconfigured", so the reason is what tells them apart)."""
        inst = self.installer()
        sch = self.scheduler(inst)
        self.verdict = {"state": "error", "reason": "not loaded (no config entry, no YAML setup)"}
        for _ in range(120):
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.assertIsNone(inst.watchdog_pending)
        self.verdict = dict(ERROR)  # a real fault after it is configured still acts
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_awaited_once()

    def test_a_health_check_that_raises_is_no_verdict_and_no_restart(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        inst.health_source = mock.Mock(side_effect=RuntimeError("entity registry not loaded"))
        for _ in range(120):
            self.tick(sch)
        inst.restart.assert_not_awaited()

    def test_the_window_is_measured_from_the_first_error_not_from_the_boot(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        self.verdict = dict(OK)
        for _ in range(60):
            self.tick(sch)
        self.verdict = dict(ERROR)
        for _ in range(15):
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.tick(sch)
        inst.restart.assert_awaited_once()


class RefusalTest(_Base):
    """Every reason the watchdog stands down.  The window has always elapsed, so the
    only thing that keeps the restart from happening is the refusal under test."""

    def _elapsed(self, inst, sch):
        for _ in range(16):
            self.tick(sch)

    def test_busy_a_pending_smoke_a_deferred_start_and_the_rest(self):
        cases = {
            "another action is running": lambda i, s: setattr(i, "busy", True),
            "still setting up": lambda i, s: setattr(i, "_entries_of", lambda d: [
                SimpleNamespace(state=SimpleNamespace(value="setup_in_progress"), disabled_by=None, title="Hub")]),
            "a smoke test is pending": lambda i, s: setattr(i, "_smoke_pending", {"domain": DOMAIN, "tag": "1.0"}),
            "a start is deferred": lambda i, s: setattr(i.state, "pending_start", {"domain": DOMAIN, "tag": "1.0"}),
            "a full rollback is waiting": lambda i, s: setattr(i.state, "pending_rollback", {"domain": DOMAIN, "tag": "1.0", "backup": "b.zip"}),
            "Home Assistant is not running": lambda i, s: setattr(i.hass, "is_running", False),
            "no integration is running": lambda i, s: setattr(i.state, "domain", None),
        }
        for needle, stage in cases.items():
            with self.subTest(refusal=needle):
                self.emitted.clear()
                inst = self.installer()
                sch = self.scheduler(inst)
                stage(inst, sch)
                self._elapsed(inst, sch)
                inst.restart.assert_not_awaited()
                self.assertTrue(self.lines(needle), f"no timeline line naming {needle!r}: {self.emitted}")

    def test_a_fresh_boot_refuses_however_bad_the_verdict_is(self):
        inst = self.installer(watchdog_after_min=5)
        sch = self.scheduler(inst)
        sch._started = self.clock()  # booted now: the integration has not had its grace yet
        for _ in range(8):           # 8 min of error, well past the 5 min window
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("s to set up"), self.emitted)
        self.clock.advance(WATCHDOG_BOOT_GRACE_S)
        self.tick(sch)
        inst.restart.assert_awaited_once()

    def test_the_persisted_pending_smoke_of_a_boot_also_refuses(self):
        inst = self.installer()
        inst.state.pending_smoke = {"domain": DOMAIN, "tag": "1.0", "can_rollback": True}
        sch = self.scheduler(inst)
        self._elapsed(inst, sch)
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("a smoke test is pending"))

    def test_a_held_version_change_lock_refuses(self):
        from custom_components.integration_manager import views

        inst = self.installer()
        sch = self.scheduler(inst)

        async def held():
            async with views._HA_CHANGE_LOCK:
                for _ in range(16):
                    self.clock.advance(60)
                    await sch._watchdog_tick()

        asyncio.run(held())
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("Home Assistant version change"))

    def test_a_running_import_refuses(self):
        from custom_components.integration_manager import import_views

        inst = self.installer()
        sch = self.scheduler(inst)

        async def held():
            async with import_views._IMPORT_LOCK:
                for _ in range(16):
                    self.clock.advance(60)
                    await sch._watchdog_tick()

        asyncio.run(held())
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("an import or upload"))

    def test_what_the_next_restart_would_apply_refuses(self):
        for needle, answer in (("a restore is scheduled", "a restore is scheduled for the next restart"),
                               ("full rollback restores its backup", "a full rollback restores its backup at the next restart: restart to finish it"),
                               ("switch to Home Assistant", "a switch to Home Assistant 2026.1.0 is scheduled for the next restart"),
                               ("an import from a Home Assistant backup", "an import from a Home Assistant backup is waiting on System: apply or clear it first")):
            with self.subTest(refusal=needle):
                self.emitted.clear()
                inst = self.installer()
                sch = self.scheduler(inst)
                sch._scheduled_refusal = lambda a=answer: a
                self._elapsed(inst, sch)
                inst.restart.assert_not_awaited()
                self.assertTrue(self.lines(needle), self.emitted)

    def test_one_refusal_line_per_stretch_not_one_a_minute(self):
        inst = self.installer()
        inst.busy = True
        sch = self.scheduler(inst)
        for _ in range(200):
            self.tick(sch)
        self.assertEqual(len(self.lines("nothing is restarted")), 1)

    def test_a_refusal_only_defers_the_restart(self):
        inst = self.installer()
        inst.busy = True
        sch = self.scheduler(inst)
        self._elapsed(inst, sch)
        inst.restart.assert_not_awaited()
        inst.busy = False
        self.tick(sch)
        inst.restart.assert_awaited_once()

    def test_the_blocking_checks_are_not_read_on_a_quiet_tick(self):
        """The volume is only looked at once the window has run out: a tick a minute
        must not read four JSON files for nothing."""
        inst = self.installer()
        sch = self.scheduler(inst)
        sch._scheduled_refusal = mock.Mock(return_value=None)
        for _ in range(15):
            self.tick(sch)
        sch._scheduled_refusal.assert_not_called()
        self.tick(sch)
        sch._scheduled_refusal.assert_called_once()


class BackoffTest(_Base):
    def _restart_once(self, inst, sch, minutes):
        for _ in range(minutes + 1):
            self.tick(sch)

    def test_the_window_doubles_after_every_restart_that_did_not_help(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        self.assertEqual(inst.watchdog_window_s(), 15 * 60)
        self._restart_once(inst, sch, 15)
        self.assertEqual(inst.restart.await_count, 1)
        self.assertEqual(inst.watchdog_window_s(), 30 * 60)  # attempt 2 needs twice as long
        self.clock.advance(3600)  # past the minimum interval
        for _ in range(30):
            self.tick(sch)
        self.assertEqual(inst.restart.await_count, 1)  # 30 min of error, not yet 30 min AFTER the doubling
        self.tick(sch)
        self.assertEqual(inst.restart.await_count, 2)
        self.assertEqual(inst.watchdog_window_s(), 60 * 60)

    def test_the_minimum_interval_defers_a_restart_the_window_would_allow(self):
        inst = self.installer(watchdog_after_min=5, watchdog_min_interval_min=60)
        sch = self.scheduler(inst)
        self._restart_once(inst, sch, 5)
        self.assertEqual(inst.restart.await_count, 1)
        for _ in range(30):  # 30 min: the doubled window (10 min) has passed, the hour has not
            self.tick(sch)
        self.assertEqual(inst.restart.await_count, 1)
        self.assertTrue(self.lines("less than 60 min ago"))
        for _ in range(35):
            self.tick(sch)
        self.assertEqual(inst.restart.await_count, 2)

    def test_the_daily_maximum_gives_up_once_and_says_so(self):
        inst = self.installer(watchdog_after_min=5, watchdog_min_interval_min=15, watchdog_max_per_day=2)
        sch = self.scheduler(inst)
        for _ in range(3):
            for _ in range(40):
                self.tick(sch)
        self.assertEqual(inst.restart.await_count, 2)
        self.assertIn("2 automatic restarts in the last 24 h", inst.watchdog_record()["gave_up"])
        for _ in range(600):
            self.tick(sch)
        self.assertEqual(inst.restart.await_count, 2)
        self.assertEqual(len(self.lines("not restarting any more")), 1)  # said once, not every minute

    def test_it_tries_again_once_the_24_h_window_has_moved_on(self):
        inst = self.installer(watchdog_after_min=5, watchdog_min_interval_min=15, watchdog_max_per_day=1)
        sch = self.scheduler(inst)
        for _ in range(20):  # the restart, then the doubled window, then the cap
            self.tick(sch)
        self.assertEqual(inst.restart.await_count, 1)
        self.assertIn("1 automatic restarts in the last 24 h", inst.watchdog_record()["gave_up"])
        self.clock.advance(86400 + 60)  # the restart ages out of the ledger
        self.tick(sch)
        self.assertEqual(inst.watchdog_record()["gave_up"], "")
        self.assertTrue(self.lines("24 h window has moved on"))
        self.assertEqual(inst.restart.await_count, 2)

    def test_recovery_resets_the_ladder_and_the_give_up_but_not_the_24_h_ledger(self):
        inst = self.installer(watchdog_after_min=5, watchdog_min_interval_min=15, watchdog_max_per_day=3)
        sch = self.scheduler(inst)
        for _ in range(10):
            self.tick(sch)
        self.assertEqual(inst.watchdog_record()["attempts"], 1)
        self.verdict = dict(OK)
        for _ in range(6):  # ok for a whole window (5 min from the first ok tick): a recovery, not a blip
            self.tick(sch)
        rec = inst.watchdog_record()
        self.assertEqual((rec["attempts"], rec["gave_up"]), (0, ""))
        self.assertEqual(len(rec["restarts"]), 1)  # the cap is a cap, not a score that recovery wipes
        self.assertEqual(inst.watchdog_window_s(), 5 * 60)
        self.assertTrue(self.lines("healthy again"))

    def test_a_refused_restart_costs_neither_a_slot_nor_a_step(self):
        inst = self.installer(watchdog_after_min=5)
        inst.restart = mock.AsyncMock(return_value={"ok": False, "error": "another action is running"})
        sch = self.scheduler(inst)
        for _ in range(10):
            self.tick(sch)
        inst.restart.assert_awaited_once()
        rec = inst.watchdog_record()
        self.assertEqual((rec["attempts"], rec["restarts"], rec["gave_up"]), (0, [], ""))
        self.assertIn("not restarted", rec["last"]["next"])
        self.assertTrue(self.lines("the restart was refused"))


class PersistenceTest(_Base):
    def test_the_rate_cap_holds_across_a_restart_of_the_manager(self):
        inst = self.installer(watchdog_after_min=5, watchdog_min_interval_min=60)
        sch = self.scheduler(inst)
        for _ in range(10):
            self.tick(sch)
        self.assertEqual(inst.restart.await_count, 1)

        # the process ends here; a new manager reads the same state.json
        again = self.installer(watchdog_after_min=5, watchdog_min_interval_min=60)
        self.assertEqual(len(again.watchdog_record()["restarts"]), 1)
        self.assertEqual(again.watchdog_record()["attempts"], 1)
        self.assertEqual(again.watchdog_window_s(), 10 * 60)  # the ladder survived too
        sch2 = self.scheduler(again)
        for _ in range(20):  # 20 min of error: past the doubled window, inside the hour
            self.tick(sch2)
        again.restart.assert_not_awaited()
        self.assertTrue(self.lines("less than 60 min ago"))

    def test_a_hand_edited_record_is_read_as_nothing_rather_than_breaking(self):
        inst = self.installer()
        inst.state.watchdog = {"restarts": ["yesterday", None, 1.0], "attempts": "many", "last": "no", "gave_up": None}
        rec = inst.watchdog_record()
        self.assertEqual((rec["restarts"], rec["attempts"], rec["last"], rec["gave_up"]), ([], 0, None, ""))

    def test_a_watchdog_record_of_the_wrong_type_is_reset_on_load(self):
        inst = self.installer()
        inst.state.watchdog = {"attempts": 2}
        inst._save_state()
        import json

        with open(inst.state_file, encoding="utf-8") as fh:
            data = json.load(fh)
        data["watchdog"] = ["not", "a", "record"]
        with open(inst.state_file, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        again = self.installer()
        self.assertIsNone(again.state.watchdog)


class VisibilityTest(_Base):
    def test_the_timeline_line_names_the_reason_the_attempt_and_what_comes_next(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        line = self.lines("restarting the process")[0]
        self.assertIn("in error for 15 min", line)
        self.assertIn("no route to host", line)
        self.assertIn("attempt 1", line)
        self.assertIn("30 min", line)   # the doubled window of the next attempt
        self.assertIn("2 left today", line)

    def test_the_status_api_carries_the_setting_the_stretch_and_the_last_action(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        for _ in range(5):
            self.tick(sch)
        status = inst.watchdog_status()
        self.assertTrue(status["enabled"])
        self.assertEqual((status["after_min"], status["min_interval_min"], status["max_per_day"]), (15, 60, 3))
        self.assertEqual(status["pending"], {"bad_for_s": 240, "window_s": 900, "reason": ERROR["reason"],
                                             "state": "error", "next": "restart"})  # nothing to reload in this fixture
        self.assertIsNone(status["last"])
        for _ in range(11):
            self.tick(sch)
        status = inst.watchdog_status()
        self.assertIsNone(status["pending"])  # the restart is the answer now, not a countdown
        self.assertEqual((status["last"]["attempt"], status["last"]["integration"]), (1, DOMAIN))
        self.assertEqual(status["last"]["reason"], ERROR["reason"])
        self.assertEqual((status["restarts_24h"], status["attempts"], status["window_min"]), (1, 1, 30))

    def test_the_notification_is_raised_at_the_boot_after_the_restart_once(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        with mock.patch("homeassistant.components.persistent_notification.async_create") as create:
            again = self.installer()
            again.announce_watchdog()
            again.announce_watchdog()
        create.assert_called_once()
        text = create.call_args.args[1]
        self.assertIn("restarted the process", text)
        self.assertIn("no route to host", text)
        self.assertEqual(create.call_args.kwargs["notification_id"], "hri_watchdog")
        third = self.installer()  # the stamp is on the volume: a later boot stays quiet
        with mock.patch("homeassistant.components.persistent_notification.async_create") as create:
            third.announce_watchdog()
        create.assert_not_called()

    def test_the_first_tick_of_a_boot_raises_it_even_with_the_setting_off(self):
        inst = self.installer()
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        again = self.installer(watchdog=False)
        sch2 = self.scheduler(again)
        sch2._announced = False
        with mock.patch.object(Installer, "announce_watchdog") as announce:
            self.tick(sch2)
            self.tick(sch2)
        announce.assert_called_once()

    def test_nothing_is_announced_when_the_watchdog_never_acted(self):
        inst = self.installer()
        with mock.patch("homeassistant.components.persistent_notification.async_create") as create:
            inst.announce_watchdog()
        create.assert_not_called()

    def test_the_record_is_on_the_volume_before_the_restart_is_asked_for(self):
        """The restart ends the process: what it did must already be saved."""
        inst = self.installer()
        saved = []

        async def restart():
            with open(inst.state_file, encoding="utf-8") as fh:
                saved.append(json.load(fh))
            return {"ok": True}

        inst.restart = restart
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["watchdog"]["attempts"], 1)
        self.assertEqual(len(saved[0]["watchdog"]["restarts"]), 1)
        self.assertIn("no route to host", saved[0]["watchdog"]["last"]["reason"])


if __name__ == "__main__":
    unittest.main()
