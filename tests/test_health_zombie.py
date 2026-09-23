"""The zombie safety net: an integration whose config entry stays loaded and whose
coordinator keeps re-writing the same states while no fresh data arrives.

- the staleness basis (per integration, opt-in): "updated" judges on the last value
  change, so re-writes of identical states no longer look like a live integration;
- the watchdog acts on a lasting "degraded" when opted in, and climbs a ladder: reload
  the config entries first, restart the process only if that did not help;
- GET /api/status carries the verdict; a transition to degraded/error is a WARNING."""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import views
from custom_components.integration_manager.manage_views import SettingsView
from custom_components.integration_manager.mqtt_rules import MqttRules
from custom_components.integration_manager.settings import Settings

from tests.test_watchdog import DOMAIN, ERROR, OK, _Base

NOW = 1_800_000_000.0
DEGRADED = {"state": "degraded", "reason": "no entity value change for 400 s"}


def _ts(t):
    return datetime.fromtimestamp(t, timezone.utc)


def _publisher(rules):
    pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig(enabled=True, host="mqtt")
    pub._connected, pub._moving, pub._stopping = False, False, False
    pub._live_base, pub._live_prefix, pub._key_provider = "hass_demo", "hass_demo_", lambda: "hass_demo"
    pub.rules = MqttRules(os.path.join(tempfile.mkdtemp(), "mqtt_rules.json"))
    pub.hass = mock.Mock()
    pub.hass.data = {}
    pub.stats, pub.history, pub._health_last = {}, [], {}
    pub._cleanup_pending, pub._cleanup_pending_lock = {}, threading.Lock()
    pub._started_at = NOW - 86400  # long past the boot grace
    pub._health_provider = lambda: {"integration": DOMAIN, "state": "ok", "reason": ""}
    pub._rules_provider = lambda domain: dict(rules)
    pub._health_announced = None
    return pub


class StaleBasisTest(unittest.TestCase):
    """The reproduction: 18 min without a value change, the coordinator re-writing every
    state each minute (last_reported 30 s old), stale_s 120."""

    def build(self, basis=None, mode="periodic", started_ago=None, with_state=True, grace=True, provider_state="ok"):
        rules = {"stale_s": 120, "mode": mode, "unavailable_pct": 50}
        if basis:
            rules["stale_basis"] = basis
        pub = _publisher(rules)
        if started_ago is not None:
            pub._started_at = NOW - started_ago
        if provider_state != "ok":
            pub._health_provider = lambda: {"integration": DOMAIN, "state": provider_state, "reason": "config entry 'Hub' is setup_retry"}
        ids = [f"sensor.demo_{i}" for i in range(3)]
        states = {i: SimpleNamespace(state="21.5", last_updated=_ts(NOW - 18 * 60), last_reported=_ts(NOW - 30))
                  for i in ids} if with_state else {}
        pub.hass.states.get.side_effect = states.get
        reg = mock.Mock(entities={i: SimpleNamespace(entity_id=i, platform=DOMAIN, disabled=False) for i in ids})
        with mock.patch.object(er, "async_get", return_value=reg), \
                mock.patch.object(mp, "async_get_platforms", return_value=[]), \
                mock.patch.object(mp, "_notification_count", return_value=0), \
                mock.patch.object(mp.time, "time", return_value=NOW):
            return pub.build_health(grace=grace)

    def test_updated_basis_calls_the_zombie_degraded(self):
        doc = self.build("updated")
        self.assertEqual(doc["state"], "degraded")
        self.assertIn("no entity value change for 1080 s", doc["reason"])
        self.assertEqual(doc["rules"]["stale_basis"], "updated")

    def test_reported_basis_keeps_todays_verdict(self):
        for basis in (None, "reported"):
            with self.subTest(basis=basis):
                doc = self.build(basis)
                self.assertEqual(doc["state"], "ok")
                self.assertEqual(doc.get("reason") or "", "")

    def test_event_mode_is_never_stale_whatever_the_basis(self):
        self.assertEqual(self.build("updated", mode="event")["state"], "ok")

    def test_no_entity_with_a_state_is_degraded_under_either_basis(self):
        """Entities registered, none with a state: the updated basis used to stop at its own branch and read ok."""
        for basis in ("reported", "updated"):
            with self.subTest(basis=basis):
                doc = self.build(basis, with_state=False)
                self.assertEqual((doc["state"], doc["reason"]), ("degraded", "no entities with a state yet"))

    def test_an_ok_that_only_the_boot_grace_allows_is_marked(self):
        """60 s after a start, stale_s 120: the checks are skipped, so the ok says nothing about the entities."""
        doc = self.build("updated", started_ago=60)
        self.assertEqual(doc["state"], "ok")
        self.assertIs(doc.get("grace"), True)

    def test_a_judged_verdict_carries_no_grace_mark(self):
        for kwargs in ({"basis": "reported"}, {"basis": "updated"}, {"basis": "updated", "started_ago": 60, "grace": False},
                       {"basis": "updated", "started_ago": 60, "provider_state": "error"}):
            with self.subTest(**kwargs):
                self.assertNotIn("grace", self.build(**kwargs))

    def test_the_settings_carry_the_basis(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        st = Settings(d)
        self.assertEqual(st.health_for(DOMAIN)["stale_basis"], "reported")
        st.data["health"] = {DOMAIN: {"stale_basis": "updated"}, "other": {"stale_basis": "bogus"}}
        self.assertEqual(st.health_for(DOMAIN)["stale_basis"], "updated")
        self.assertEqual(st.health_for("other")["stale_basis"], "reported")  # a hand-edited value it does not know


class _Request:
    content_type = "application/json"

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)


class SettingsApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = Settings(self.dir)
        self.view = SettingsView(SimpleNamespace(settings=self.settings, hass=None, _releases_cache={}, scheduler=None))

    def post(self, body):
        with mock.patch.object(type(self.settings), "async_save", mock.AsyncMock()):
            return json.loads(asyncio.run(self.view.post(_Request(body))).body.decode())

    def test_stale_basis_is_saved_and_checked(self):
        out = self.post({"health": {DOMAIN: {"stale_s": 120, "stale_basis": "updated"}}})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.settings.health_for(DOMAIN)["stale_basis"], "updated")
        out = self.post({"health": {DOMAIN: {"stale_basis": "changed"}}})
        self.assertFalse(out["ok"])
        self.assertIn("stale_basis", out["error"])

    def test_watchdog_on_degraded_is_a_boolean_setting(self):
        self.assertFalse(self.settings.watchdog()["on_degraded"])
        out = self.post({"watchdog_on_degraded": True})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["watchdog_on_degraded"])
        self.assertTrue(self.settings.watchdog()["on_degraded"])
        self.assertFalse(self.post({"watchdog_on_degraded": "yes"})["ok"])


class _LadderBase(_Base):
    def installer(self, **settings):
        inst = super().installer(**settings)
        inst.reload_entry = mock.AsyncMock(return_value=True)
        inst._entries_of = lambda dom: [SimpleNamespace(state=SimpleNamespace(value="loaded"), disabled_by=None,
                                                        title="Hub", entry_id="e1")]
        return inst

    def run_min(self, sch, minutes):
        for _ in range(minutes):
            self.tick(sch)


class LadderTest(_LadderBase):
    """Reload first, restart only if a window after the reload the verdict is still not ok."""

    def test_a_lasting_degraded_is_acted_on_only_when_opted_in(self):
        for opted in (False, True):
            with self.subTest(on_degraded=opted):
                inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=opted)
                sch = self.scheduler(inst)
                self.verdict = dict(DEGRADED)
                self.run_min(sch, 6)
                self.assertEqual(inst.reload_entry.await_count, 1 if opted else 0)
                inst.restart.assert_not_awaited()

    def test_degraded_reload_first_then_restart_a_window_later(self):
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True)
        sch = self.scheduler(inst)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 6)  # the first tick starts the clock; five minutes later the window has run out
        inst.reload_entry.assert_awaited_once_with("e1")
        inst.restart.assert_not_awaited()
        line = self.lines("reloading")[0]
        self.assertIn("health degraded for 5 min", line)
        self.assertIn(f"reloading {DOMAIN}'s entries", line)
        self.assertEqual(inst.watchdog_status()["reloads_24h"], 1)
        self.assertEqual(inst.watchdog_status()["last_reload"]["result"], "reloaded")
        self.run_min(sch, 4)
        inst.restart.assert_not_awaited()  # still inside the window that follows the reload
        self.assertEqual(inst.watchdog_pending["next"], "restart")
        self.run_min(sch, 1)
        inst.restart.assert_awaited_once()
        self.assertEqual(inst.reload_entry.await_count, 1)
        self.assertTrue(self.lines("has been degraded for 10 min"))

    def test_error_climbs_the_same_ladder(self):
        inst = self.installer(watchdog_after_min=5)
        sch = self.scheduler(inst)
        self.verdict = dict(ERROR)
        self.run_min(sch, 6)
        inst.reload_entry.assert_awaited_once()
        inst.restart.assert_not_awaited()
        self.run_min(sch, 5)
        inst.restart.assert_awaited_once()

    def test_an_ok_blip_after_the_reload_does_not_reset_the_ladder(self):
        """The reload rewrites every entity: the verdict reads ok for a couple of minutes on a zombie that is
        still dead.  The next stretch goes on to the restart instead of reloading again, for ever."""
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True)
        sch = self.scheduler(inst)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 6)
        self.assertEqual(inst.reload_entry.await_count, 1)
        self.verdict = dict(OK)
        self.run_min(sch, 2)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 6)
        self.assertEqual(inst.reload_entry.await_count, 1)
        inst.restart.assert_awaited_once()

    def test_a_real_recovery_resets_the_ladder(self):
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True)
        sch = self.scheduler(inst)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 6)
        self.verdict = dict(OK)
        self.run_min(sch, 6)  # ok for a whole window after the reload: it worked
        self.assertIsNone(sch._reloaded_at)
        self.assertTrue(self.lines("since the reload"))
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 6)
        self.assertEqual(inst.reload_entry.await_count, 2)  # a new episode starts with a reload again
        inst.restart.assert_not_awaited()

    def test_the_reload_cap_holds_and_then_the_restart_step_comes_first(self):
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True, watchdog_min_interval_min=15)
        sch = self.scheduler(inst)
        cap = inst.WATCHDOG_RELOADS_PER_DAY
        for _ in range(cap):  # episode after episode, each one fixed by its reload
            self.verdict = dict(DEGRADED)
            self.run_min(sch, 6)
            self.verdict = dict(OK)
            self.run_min(sch, 6)
        self.assertEqual(inst.reload_entry.await_count, cap)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 6)
        self.assertEqual(inst.reload_entry.await_count, cap)  # no seventh reload today
        inst.restart.assert_awaited_once()
        self.assertTrue(self.lines(f"{cap} automatic reloads in the last 24 h is the maximum"))
        # the cap survives the restart it led to: it lives in state.json
        again = self.installer(watchdog_after_min=5)
        self.assertEqual(len(again.watchdog_record()["reloads"]), cap)

    def test_refusals_hold_the_reload_back_too(self):
        cases = {
            "another action is running": lambda i, s: setattr(i, "busy", True),
            "a smoke test is pending": lambda i, s: setattr(i, "_smoke_pending", {"domain": DOMAIN, "tag": "1.0"}),
            "s to set up": lambda i, s: setattr(s, "_started", self.clock()),
            "a restore is scheduled": lambda i, s: setattr(s, "_scheduled_refusal", lambda: "a restore is scheduled for the next restart"),
        }
        for needle, stage in cases.items():
            with self.subTest(refusal=needle):
                self.emitted.clear()
                inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True)
                sch = self.scheduler(inst)
                stage(inst, sch)
                self.verdict = dict(DEGRADED)
                self.run_min(sch, 12)
                inst.reload_entry.assert_not_awaited()
                inst.restart.assert_not_awaited()
                self.assertTrue(self.lines("nothing is reloaded"), self.emitted)
                self.assertTrue(self.lines(needle), self.emitted)

    def test_the_restart_caps_still_apply_after_a_reload(self):
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True, watchdog_max_per_day=1)
        inst.state.watchdog = {"restarts": [self.clock() - 3600], "attempts": 0}
        sch = self.scheduler(inst)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 30)
        self.assertEqual(inst.reload_entry.await_count, 1)
        inst.restart.assert_not_awaited()
        self.assertIn("1 automatic restarts in the last 24 h", inst.watchdog_record()["gave_up"])

    def test_an_entry_that_fails_to_reload_is_recorded_and_the_ladder_goes_on(self):
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True)
        inst.reload_entry = mock.AsyncMock(side_effect=RuntimeError("boom"))
        sch = self.scheduler(inst)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 6)
        self.assertIn("RuntimeError: boom", inst.watchdog_status()["last_reload"]["result"])
        self.assertTrue(self.lines("did not complete"))
        self.run_min(sch, 5)
        inst.restart.assert_awaited_once()


class RecoveryTest(_LadderBase):
    """What counts as a recovery for the backoff and the give-up: ok for a whole window, the same rule as the
    ladder.  An ok the boot grace allows is no verdict at all, and the ok blip after a reload is not a recovery."""

    GRACE_OK = {"state": "ok", "reason": "", "grace": True}

    def restarted_once(self, **settings):
        """degraded -> reload -> restart, then the process that restart leads to, on the same state.json."""
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True, watchdog_min_interval_min=15, **settings)
        sch = self.scheduler(inst)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 11)
        inst.restart.assert_awaited_once()
        self.assertEqual(inst.watchdog_record()["attempts"], 1)
        again = self.installer(watchdog_after_min=5, watchdog_on_degraded=True, watchdog_min_interval_min=15, **settings)
        return again, self.scheduler(again)

    def test_a_grace_ok_after_the_restart_keeps_the_backoff(self):
        inst, sch = self.restarted_once()
        self.clock.advance(3600)  # past the minimum interval
        self.verdict = dict(self.GRACE_OK)
        self.run_min(sch, 5)  # stale_s 300: the whole grace reads ok, the zombie is as dead as before
        self.assertEqual(inst.watchdog_record()["attempts"], 1)
        self.assertEqual(inst.watchdog_window_s(), 10 * 60)
        self.assertFalse(self.lines("healthy again"))
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 10)
        inst.reload_entry.assert_not_awaited()  # 10 ticks: 9 min of degraded, the doubled window has not run out
        self.run_min(sch, 1)
        inst.reload_entry.assert_awaited_once()
        self.run_min(sch, 10)
        inst.restart.assert_awaited_once()
        self.assertEqual(inst.watchdog_record()["attempts"], 2)
        self.assertEqual(inst.watchdog_window_s(), 20 * 60)

    def test_a_grace_ok_does_not_end_the_stretch(self):
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True)
        sch = self.scheduler(inst)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 3)
        self.verdict = dict(self.GRACE_OK)  # an identity move restarts the grace in the same process
        self.run_min(sch, 1)
        self.assertIsNotNone(sch._bad_since)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 2)
        inst.reload_entry.assert_awaited_once()

    def test_a_give_up_survives_a_grace_ok(self):
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True, watchdog_max_per_day=1)
        inst.state.watchdog = {"restarts": [self.clock() - 3600], "attempts": 1,
                               "gave_up": "1 automatic restarts in the last 24 h is the maximum (1/day)"}
        sch = self.scheduler(inst)
        self.verdict = dict(self.GRACE_OK)
        self.run_min(sch, 15)
        rec = inst.watchdog_record()
        self.assertIn("1 automatic restarts", rec["gave_up"])
        self.assertEqual(rec["attempts"], 1)

    def test_a_real_ok_resets_the_backoff_only_after_a_whole_window(self):
        inst = self.installer(watchdog_after_min=5, watchdog_on_degraded=True, watchdog_max_per_day=1)
        inst.state.watchdog = {"restarts": [self.clock() - 3600], "attempts": 1,
                               "gave_up": "1 automatic restarts in the last 24 h is the maximum (1/day)"}
        sch = self.scheduler(inst)
        self.verdict = dict(OK)
        self.run_min(sch, 5)  # the first ok tick starts the clock: four minutes of ok so far
        rec = inst.watchdog_record()
        self.assertEqual((rec["attempts"], bool(rec["gave_up"])), (1, True))
        self.run_min(sch, 1)
        rec = inst.watchdog_record()
        self.assertEqual((rec["attempts"], rec["gave_up"]), (0, ""))
        self.assertEqual(len(rec["restarts"]), 1)
        self.assertEqual(len(self.lines("healthy again")), 1)
        self.run_min(sch, 30)
        self.assertEqual(len(self.lines("healthy again")), 1)

    def test_a_short_ok_after_a_reload_keeps_the_backoff(self):
        inst, sch = self.restarted_once()
        self.clock.advance(3600)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 11)  # the doubled window, then the reload
        inst.reload_entry.assert_awaited_once()
        self.verdict = dict(OK)  # the fresh states of the reload
        self.run_min(sch, 2)
        self.assertEqual(inst.watchdog_record()["attempts"], 1)
        self.assertEqual(inst.watchdog_window_s(), 10 * 60)
        self.verdict = dict(DEGRADED)
        self.run_min(sch, 11)
        inst.restart.assert_awaited_once()
        self.assertEqual(inst.reload_entry.await_count, 1)
        self.assertEqual(inst.watchdog_record()["attempts"], 2)

    def test_error_keeps_its_backoff_through_a_grace_ok_and_recovers_after_a_window(self):
        inst = self.installer(watchdog_after_min=5, watchdog_min_interval_min=15)
        sch = self.scheduler(inst)
        self.verdict = dict(ERROR)
        self.run_min(sch, 11)
        inst.reload_entry.assert_awaited_once()
        inst.restart.assert_awaited_once()
        again = self.installer(watchdog_after_min=5, watchdog_min_interval_min=15)
        sch2 = self.scheduler(again)
        self.clock.advance(3600)
        self.verdict = dict(self.GRACE_OK)
        self.run_min(sch2, 3)
        self.assertEqual(again.watchdog_record()["attempts"], 1)
        self.verdict = dict(ERROR)
        self.run_min(sch2, 21)  # the doubled window to the reload, then the doubled window to the restart
        again.reload_entry.assert_awaited_once()
        again.restart.assert_awaited_once()
        self.assertEqual(again.watchdog_record()["attempts"], 2)
        third = self.installer(watchdog_after_min=5, watchdog_min_interval_min=15)
        sch3 = self.scheduler(third)
        self.verdict = dict(OK)
        self.run_min(sch3, 6)
        self.assertEqual(third.watchdog_record()["attempts"], 0)

    def test_the_give_up_line_says_what_brings_it_back(self):
        inst = self.installer(watchdog_after_min=5)
        inst.watchdog_give_up("the cap")
        line = self.lines("not restarting any more")[0]
        self.assertIn("ok for 5 min", line)


class StatusApiTest(unittest.TestCase):
    def test_status_carries_the_verdict_from_memory(self):
        installer = mock.Mock()
        installer.status = mock.AsyncMock(return_value={"integration": DOMAIN})
        installer.hass.config.components = set()
        publisher = SimpleNamespace(_health_last={
            "state": "degraded", "reason": "no entity value change for 400 s", "since": "2026-09-23T10:00:00+0000",
            "updated_at": "2026-09-23T10:05:00+0000", "rules": {"stale_s": 120, "stale_basis": "updated"}})
        view = views.StatusView(installer, publisher)
        req = SimpleNamespace(headers={"X-Requested-With": "fetch"}, query={})
        body = json.loads(asyncio.run(view.get(req)).body)
        self.assertEqual(body["health"], {"state": "degraded", "reason": "no entity value change for 400 s", "basis": "updated",
                                          "since": "2026-09-23T10:00:00+0000", "updated_at": "2026-09-23T10:05:00+0000"})


class TransitionLogTest(unittest.TestCase):
    def test_warning_once_per_transition_info_on_recovery(self):
        pub = _publisher({"stale_s": 120, "mode": "periodic", "unavailable_pct": 50})
        pub.hass.state = CoreState.running
        docs = iter([{"state": "ok"}] * 2 + [{"state": "degraded", "reason": "no entity value change for 400 s"}] * 5
                    + [{"state": "ok"}] * 3)
        with mock.patch.object(pub, "build_health", side_effect=lambda: {"integration": DOMAIN, "updated_at": "t", **next(docs)}), \
                mock.patch.object(mp.events, "emit"), \
                self.assertLogs(mp._LOGGER, level=logging.INFO) as logs:
            out = [pub.publish_health() for _ in range(10)]
        warnings = [r for r in logs.records if r.levelno == logging.WARNING and "health:" in r.getMessage()]
        infos = [r for r in logs.records if r.levelno == logging.INFO and "ok again" in r.getMessage()]
        self.assertEqual(len(warnings), 1, [r.getMessage() for r in logs.records])
        self.assertIn("degraded", warnings[0].getMessage())
        self.assertIn("no entity value change", warnings[0].getMessage())
        self.assertEqual(len(infos), 1)
        self.assertEqual(out[-1]["since"], "t")


if __name__ == "__main__":
    unittest.main()
