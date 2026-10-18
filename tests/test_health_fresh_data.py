"""What counts as fresh data for the health verdict.

A passive test of ramses_cc 0.61.1 on a dead feed (half-open TCP, nothing received) kept the
"updated" basis ok for 14 minutes: the device status sensors rewrite staleness_seconds every
minute, which moves last_updated.  With those disabled the verdict went degraded, then back to
ok five minutes later when a sensor's value expired to unknown.  So: a change to unknown or
unavailable is never fresh, nor is a change of only attributes that tick with the clock, and a
state an entity comes back with (a reload, an outage) is fresh only when it is a new value."""

import asyncio
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.manage_views import SettingsView
from custom_components.integration_manager.settings import Settings

from tests.test_health_zombie import _publisher, _Request

T0 = 1_800_000_000.0
STALE_S = 300


def _ts(t):
    return datetime.fromtimestamp(t, timezone.utc)


class _Sim:
    """A Home Assistant state machine reduced to what the health verdict reads: a write of the same state and
    attributes only moves last_reported (no state_changed); any other write fires state_changed."""

    def __init__(self, domain, rules=None, registry=None):
        self.domain = domain
        self.pub = _publisher({"stale_s": STALE_S, "mode": "periodic", "unavailable_pct": 50, "stale_basis": "updated",
                               **(rules or {})})
        self.pub._health_provider = lambda: {"integration": domain, "state": "ok", "reason": ""}
        self.pub._started_at = T0 - 86400  # past the boot grace
        self.pub._fresh, self.pub._fresh_since = {}, T0
        self.states: dict[str, State] = {}
        self.registry = {i: SimpleNamespace(entity_id=i, platform=domain, disabled=False) for i in (registry or ())}
        self.pub.hass.states.get.side_effect = lambda i: self.states.get(i)

    def _reg(self):
        return mock.Mock(entities=self.registry, async_get=self.registry.get)

    def write(self, t, entity_id, state, **attrs):
        self.registry.setdefault(entity_id, SimpleNamespace(entity_id=entity_id, platform=self.domain, disabled=False))
        old = self.states.get(entity_id)
        if old is not None and old.state == state and dict(old.attributes) == attrs:
            self.states[entity_id] = State(entity_id, state, attrs, last_changed=old.last_changed, last_reported=_ts(t),
                                           last_updated=old.last_updated)
            return
        changed = _ts(t) if old is None or old.state != state else old.last_changed
        new = State(entity_id, state, attrs, last_changed=changed, last_reported=_ts(t), last_updated=_ts(t))
        self.states[entity_id] = new
        self._event(entity_id, old, new)

    def remove(self, entity_id, keep_registry=True):
        old = self.states.pop(entity_id)
        if not keep_registry:
            self.registry.pop(entity_id)
        self._event(entity_id, old, None)

    def _event(self, entity_id, old, new):
        event = SimpleNamespace(data={"entity_id": entity_id, "old_state": old, "new_state": new})
        with mock.patch.object(er, "async_get", return_value=self._reg()), \
                mock.patch.object(self.pub, "_publish_state"), mock.patch.object(self.pub, "_refresh_discovery_if_reshaped"), \
                mock.patch.object(self.pub, "_clear"):
            self.pub._on_state(event)

    def health(self, t):
        with mock.patch.object(er, "async_get", return_value=self._reg()), \
                mock.patch.object(mp, "async_get_platforms", return_value=[]), \
                mock.patch.object(mp, "_notification_count", return_value=0), \
                mock.patch.object(mp.time, "time", return_value=t):
            return self.pub.build_health()


def _status_attrs(t, last_heard):
    """A ramses_cc 0.61.1 device status sensor: ages recomputed at every write."""
    return {"last_seen": _ts(last_heard).isoformat(), "staleness_seconds": t - last_heard, "heartbeat_timeout": 3600.0,
            "consecutive_missed_polls": 0, "best_rssi": -60, "rssi_quality": "good", "is_stale": False,
            "rssi_per_hgi": {"18:010399": -60}, "last_known_rssi": -60, "last_known_rssi_per_hgi": {"18:010399": -60},
            "last_rssi_seen": _ts(last_heard).isoformat(), "last_rssi_age_seconds": t - last_heard}


class DeadFeedTest(unittest.TestCase):
    """The two cases of the passive test, then the same feed alive."""

    def ramses(self, status_id="binary_sensor.trv_04_092960_status"):
        sim = _Sim("ramses_cc")
        sim.write(T0, "sensor.thm_22_216226_temperature", "21.5")
        sim.write(T0, status_id, "on", **_status_attrs(T0, T0))
        sim.write(T0 + 30, "sensor.thm_22_216226_temperature", "21.6")  # the last packet
        sim.write(T0 + 30, status_id, "on", **_status_attrs(T0 + 30, T0 + 30))
        return sim

    def test_attributes_ticking_every_minute_on_a_dead_feed_turn_degraded(self):
        for status_id in ("binary_sensor.trv_04_092960_status", "binary_sensor.trv_link"):  # in the glob, renamed out
            with self.subTest(status_id=status_id):
                sim = self.ramses(status_id)
                for minute in range(1, 15):
                    t = T0 + 30 + 60 * minute
                    attrs = _status_attrs(t, T0 + 30)
                    if minute >= 5:  # the per-gateway RSSI tracker expired, the polls go unanswered
                        attrs.update(best_rssi=None, rssi_quality=None, is_stale=True, rssi_per_hgi={},
                                     consecutive_missed_polls=minute - 4)
                    sim.write(t, status_id, "on", **attrs)
                    sim.write(t, "sensor.thm_22_216226_temperature", "21.6")  # the coordinator re-writes it
                doc = sim.health(T0 + 30 + STALE_S + 1)
                self.assertEqual((doc["state"], doc["reason"]), ("degraded", f"no entity value change for {STALE_S + 1} s"))
                self.assertEqual(sim.health(T0 + 30 + 14 * 60)["state"], "degraded")

    def test_the_status_sensor_turning_off_on_its_own_is_no_fresh_data(self):
        sim = self.ramses()
        sim.write(T0 + 200, "binary_sensor.trv_04_092960_status", "off", **_status_attrs(T0 + 200, T0 + 30))
        self.assertEqual(sim.health(T0 + 30 + STALE_S + 1)["state"], "degraded")

    def test_a_value_expiring_to_unknown_keeps_it_degraded(self):
        sim = self.ramses()
        self.assertEqual(sim.health(T0 + 30 + 359)["state"], "degraded")
        sim.write(T0 + 30 + 660, "sensor.thm_22_216226_temperature", "unknown")
        sim.write(T0 + 30 + 660, "climate.zone_01", "unavailable")  # an entity appearing unavailable
        doc = sim.health(T0 + 30 + 661)
        self.assertEqual((doc["state"], doc["reason"]), ("degraded", "no entity value change for 661 s"))
        self.assertEqual(doc["last_state_update_age_s"], 661)

    def test_the_value_coming_back_after_unknown_is_fresh_when_it_is_new(self):
        sim = self.ramses()
        sim.write(T0 + 700, "sensor.thm_22_216226_temperature", "unknown")
        sim.write(T0 + 800, "sensor.thm_22_216226_temperature", "21.6")  # the value it had
        self.assertEqual(sim.health(T0 + 801)["state"], "degraded")
        sim.write(T0 + 900, "sensor.thm_22_216226_temperature", "20.9")
        self.assertEqual(sim.health(T0 + 901)["state"], "ok")

    def test_a_real_value_change_counts(self):
        sim = self.ramses()
        sim.write(T0 + 300, "sensor.thm_22_216226_temperature", "21.7")
        doc = sim.health(T0 + 30 + STALE_S + 1)
        self.assertEqual(doc["state"], "ok")
        self.assertEqual(doc["last_state_update_age_s"], 31)

    def test_a_climate_attribute_change_counts(self):
        """A climate entity's state is its mode: the temperature it measures is an attribute."""
        sim = _Sim("ramses_cc")
        sim.write(T0, "climate.zone_01", "heat", current_temperature=21.0, temperature=21.5)
        sim.write(T0 + 250, "climate.zone_01", "heat", current_temperature=21.1, temperature=21.5)
        self.assertEqual(sim.health(T0 + 250 + STALE_S - 1)["state"], "ok")
        self.assertEqual(sim.health(T0 + 250 + STALE_S + 1)["state"], "degraded")


class ReloadTest(unittest.TestCase):
    """A reload takes the entities away (unavailable, or removed) and puts them back with what they had."""

    def setUp(self):
        self.sim = _Sim("demo")
        self.sim.write(T0, "sensor.demo_t", "21.5")
        self.sim.write(T0, "climate.demo", "heat", current_temperature=21.0)
        self.sim.write(T0 + 10, "sensor.demo_t", "21.6")  # the last fresh data

    def test_states_put_back_with_their_old_values_are_not_fresh(self):
        t = T0 + 600
        self.sim.write(t, "sensor.demo_t", "unavailable", restored=True)
        self.sim.remove("climate.demo")
        self.sim.write(t + 5, "sensor.demo_t", "21.6")
        self.sim.write(t + 5, "climate.demo", "heat", current_temperature=21.0)
        doc = self.sim.health(t + 6)
        self.assertEqual((doc["state"], doc["reason"]), ("degraded", "no entity value change for 596 s"))

    def test_a_new_value_after_a_reload_is_fresh(self):
        t = T0 + 600
        self.sim.remove("climate.demo")
        self.sim.write(t + 5, "climate.demo", "heat", current_temperature=19.5)
        self.assertEqual(self.sim.health(t + 6)["state"], "ok")

    def test_an_entity_gone_from_the_registry_is_forgotten(self):
        self.sim.remove("sensor.demo_t", keep_registry=False)
        self.assertNotIn("sensor.demo_t", self.sim.pub._fresh)
        self.sim.remove("climate.demo")  # its registry entry stays: put back by a reload
        self.assertIn("climate.demo", self.sim.pub._fresh)

    def test_a_new_entity_first_value_is_no_fresh_data(self):
        """Nothing changed since the listener started: the verdict counts from that start."""
        sim = _Sim("demo")
        sim.write(T0 + 100, "sensor.demo_new", "5")
        doc = sim.health(T0 + STALE_S + 1)
        self.assertEqual((doc["state"], doc["reason"]), ("degraded", f"no entity value change for {STALE_S + 1} s"))


class ConfiguredIgnoreTest(unittest.TestCase):
    def run_case(self, rules):
        sim = _Sim("demo", rules)
        sim.write(T0, "sensor.demo_t", "21.5", uptime=0)
        sim.write(T0, "sensor.demo_link", "ok")
        for minute in range(1, 7):
            sim.write(T0 + 60 * minute, "sensor.demo_t", "21.5", uptime=60 * minute)
            sim.write(T0 + 60 * minute, "sensor.demo_link", "ok" if minute % 2 else "lost")
        return sim.health(T0 + 6 * 60 + 1)

    def test_an_attribute_and_an_entity_set_in_the_rules_are_left_out(self):
        self.assertEqual(self.run_case({})["state"], "ok")
        self.assertEqual(self.run_case({"ignore_attributes": ["uptime"]})["state"], "ok")  # the link still changes
        doc = self.run_case({"ignore_attributes": ["upt*"], "ignore_entities": ["sensor.*_link"]})
        self.assertEqual((doc["state"], doc["reason"]), ("degraded", "no entity value change for 361 s"))

    def test_a_built_in_age_attribute_is_left_out_for_every_integration(self):
        sim = _Sim("demo")
        sim.write(T0, "sensor.demo_t", "21.5", data_age_seconds=0)
        for minute in range(1, 7):
            sim.write(T0 + 60 * minute, "sensor.demo_t", "21.5", data_age_seconds=60 * minute)
        self.assertEqual(sim.health(T0 + 6 * 60 + 1)["state"], "degraded")

    def test_the_reported_basis_is_unchanged_by_the_ignore_lists(self):
        doc = self.run_case({"stale_basis": "reported", "ignore_entities": ["sensor.*"]})
        self.assertEqual(doc["state"], "ok")


class ReportedBasisTest(unittest.TestCase):
    def test_writes_of_unknown_do_not_keep_it_alive(self):
        sim = _Sim("demo", {"stale_basis": "reported"})
        sim.write(T0, "sensor.demo_t", "21.5")
        sim.write(T0, "sensor.demo_v", "3")
        for minute in range(1, 7):
            sim.write(T0 + 60 * minute, "sensor.demo_v", "unavailable" if minute % 2 else "unknown")
        doc = sim.health(T0 + 6 * 60 + 1)
        self.assertEqual((doc["state"], doc["reason"]), ("degraded", "no entity report for 361 s"))

    def test_an_integration_without_any_value_is_judged_as_before(self):
        """Buttons, scenes and events that never fired are "unknown": their writes still count."""
        sim = _Sim("demo", {"stale_basis": "reported"})
        sim.write(T0, "button.demo_restart", "unknown")
        sim.write(T0 + 200, "button.demo_restart", "unknown", friendly_name="Restart")
        self.assertEqual(sim.health(T0 + STALE_S + 1)["state"], "ok")


class IgnoreSettingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = Settings(self.dir)
        self.view = SettingsView(SimpleNamespace(settings=self.settings, hass=None, _releases_cache={}, scheduler=None))

    def post(self, body):
        with mock.patch.object(type(self.settings), "async_save", mock.AsyncMock()):
            return json.loads(asyncio.run(self.view.post(_Request(body))).body.decode())

    def test_the_ignore_lists_are_saved_and_checked(self):
        out = self.post({"health": {"demo": {"stale_basis": "updated", "ignore_attributes": ["uptime", " uptime ", "Last seen"],
                                             "ignore_entities": ["binary_sensor.*_link"]}}})
        self.assertTrue(out["ok"], out)
        rules = self.settings.health_for("demo")
        self.assertEqual(rules["ignore_attributes"], ["uptime", "Last seen"])
        self.assertEqual(rules["ignore_entities"], ["binary_sensor.*_link"])
        for bad in ({"ignore_attributes": "uptime"}, {"ignore_attributes": [3]}, {"ignore_attributes": ["x" * 101]},
                    {"ignore_attributes": ["a\nb"]}, {"ignore_entities": ["Sensor.Bad Name"]},
                    {"ignore_entities": [f"sensor.e{i}" for i in range(51)]}):
            with self.subTest(bad=bad):
                out = self.post({"health": {"demo": bad}})
                self.assertFalse(out["ok"], out)
                self.assertIn(f"health.demo.{next(iter(bad))}", out["error"])
        out = self.post({"health": {"demo": {"ignore_attributes": []}}})
        self.assertTrue(out["ok"], out)
        self.assertNotIn("demo", self.settings.data["health"])

    def test_a_hand_edited_list_of_the_wrong_shape_is_left_out(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(f"{d}/settings.json", "w", encoding="utf-8") as fh:
            json.dump({"health": {"demo": {"ignore_attributes": "uptime", "ignore_entities": ["sensor.*_link"]}}}, fh)
        rules = Settings(d).health_for("demo")
        self.assertEqual((rules["ignore_attributes"], rules["ignore_entities"]), ([], ["sensor.*_link"]))
        self.assertEqual(Settings(d).health_for("other")["ignore_entities"], [])


class StateListenerTest(unittest.TestCase):
    def test_the_listener_starts_the_tracking(self):
        pub = _publisher({})
        pub.hass.bus.async_listen = mock.Mock()
        pub.hass.async_add_executor_job = mock.AsyncMock(return_value=False)
        pub.config.enabled = False
        pub._arm_republish_timer = mock.Mock()
        with mock.patch.object(mp, "async_track_time_interval"), mock.patch.object(mp, "async_dispatcher_connect"), \
                mock.patch.object(mp.time, "time", return_value=T0):
            asyncio.run(pub.async_start())
        self.assertEqual((pub._fresh, pub._fresh_since), ({}, T0))


if __name__ == "__main__":
    unittest.main()
