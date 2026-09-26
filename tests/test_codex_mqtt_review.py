"""External review of 6b6f5ff, MQTT side: settings out of range that stop the next setup, the first
occurrence of a fresh event entity, on/off payloads that are neither, and the call caps under a burst."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r3_mqtt import BASE, _message, _publisher
from tests.test_r4_mqtt import _running_publisher, _settle

HUGE = 100000000000000  # timedelta(seconds=...) overflows well below this


def _config_publisher(stored=None):
    pub = object.__new__(mp.MqttPublisher)
    pub.hass = mock.Mock()
    pub.path = os.path.join(tempfile.mkdtemp(), "mqtt.json")
    if stored is not None:
        with open(pub.path, "w", encoding="utf-8") as fh:
            json.dump(stored, fh)
    pub.config = mp.MqttConfig()
    pub._saved = None
    pub._republish_unsub = None
    pub.hass.loop.time = lambda: 0.0  # the timer really schedules: a Mock clock is not one
    return pub


class OutOfRangeSettingsTest(unittest.TestCase):
    """F2: a value the form accepts must never be able to stop the next setup, where no view exists yet to fix it."""

    def test_a_stored_interval_out_of_range_still_arms_the_timer(self):
        pub = _config_publisher({"republish_interval_s": HUGE})
        with self.assertLogs(mp._LOGGER, "WARNING"):
            pub.config = pub._load()
        pub._arm_republish_timer()  # OverflowError here fails the setup of this component

    def test_every_numeric_setting_falls_back_to_its_default_on_load(self):
        pub = _config_publisher({"port": 999999, "qos": 9, "republish_interval_s": HUGE,
                                 "full_republish_interval_min": "soon", "host": "broker"})
        with self.assertLogs(mp._LOGGER, "WARNING"):
            config = pub._load()
        self.assertEqual((config.port, config.qos), (1883, 0))
        self.assertEqual((config.republish_interval_s, config.full_republish_interval_min), (300, 60))
        self.assertEqual(config.host, "broker")  # only what is unusable is dropped

    def test_the_form_cannot_store_an_interval_out_of_range(self):
        pub = _config_publisher()
        config = pub._validated({"republish_interval_s": HUGE, "full_republish_interval_min": HUGE})
        self.assertEqual(config.republish_interval_s, mp.INT_BOUNDS["republish_interval_s"][1])
        self.assertEqual(config.full_republish_interval_min, mp.INT_BOUNDS["full_republish_interval_min"][1])

    def test_the_intervals_still_clamp_at_their_minimum(self):
        config = _config_publisher()._validated({"republish_interval_s": 1, "full_republish_interval_min": 0})
        self.assertEqual((config.republish_interval_s, config.full_republish_interval_min), (30, 5))

    def test_port_and_qos_are_still_refused(self):
        pub = _config_publisher()
        for updates in ({"port": 0}, {"port": 70000}, {"port": "x"}, {"qos": 3}, {"qos": -1}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                pub._validated(updates)

    def test_a_bad_value_on_disk_is_not_carried_into_the_next_save(self):
        pub = _config_publisher({"republish_interval_s": HUGE})
        with self.assertLogs(mp._LOGGER, "WARNING"):
            config = pub._validated({"host": "broker"}, pub._read_saved())
        self.assertEqual(config.republish_interval_s, 300)


def _event_state(state):
    return SimpleNamespace(entity_id="event.button", domain="event", state=state)


class FirstEventTest(unittest.TestCase):
    """F3: the first real occurrence of a fresh event entity."""

    def _occurrences(self, seq):
        pub = object.__new__(mp.MqttPublisher)
        pub._last_event = {}
        pub.config, pub._connected = mp.MqttConfig(), False  # only the replay logic is under test: no discovery here
        published = []
        pub._publish_state = lambda state, is_event=False, force=True: published.append((state.state, is_event))
        for old, new in seq:
            pub._on_state(SimpleNamespace(data={"old_state": _event_state(old) if old else None,
                                                "new_state": _event_state(new)}))
        return [state for state, is_event in published if is_event]

    def test_the_first_occurrence_of_a_fresh_entity_is_published(self):
        self.assertEqual(self._occurrences([(None, "unknown"), ("unknown", "T1"), ("T1", "T2")]), ["T1", "T2"])

    def test_a_restored_timestamp_is_not_an_occurrence(self):
        self.assertEqual(self._occurrences([(None, "T1"), ("T1", "T2")]), ["T2"])

    def test_an_availability_flap_does_not_replay(self):
        self.assertEqual(self._occurrences([(None, "unknown"), ("unknown", "T1"),
                                            ("T1", "unavailable"), ("unavailable", "T1")]), ["T1"])

    def test_a_flap_this_process_did_not_see_does_not_replay(self):
        self.assertEqual(self._occurrences([("unavailable", "T1")]), [])


ON_OFF = (("switch", "state"), ("light", "state"), ("fan", "state"), ("humidifier", "state"), ("siren", "state"))


class OnOffPayloadTest(unittest.TestCase):
    """F4: a payload that is neither on nor off must be refused, not read as off."""

    def test_a_payload_that_is_neither_is_refused(self):
        for domain, field in ON_OFF:
            for payload in ("TOGGLE", " ", "0FF", "onn", "2"):
                with self.subTest(domain=domain, payload=payload), self.assertRaises(ValueError):
                    disc.command_to_service(domain, "x", field, payload)

    def test_the_accepted_tokens_still_map(self):
        for payload, service in ((" ON\n", "turn_on"), ("on", "turn_on"), ("TRUE", "turn_on"), ("1", "turn_on"),
                                 ("OFF", "turn_off"), ("false", "turn_off"), ("0", "turn_off")):
            for domain, field in ON_OFF:
                with self.subTest(domain=domain, payload=payload):
                    self.assertEqual(disc.command_to_service(domain, "x", field, payload)[1], service)

    def test_a_siren_json_state_is_checked_too(self):
        self.assertEqual(disc.command_to_service("siren", "s", "state", '{"state": "OFF", "tone": "a"}')[1], "turn_off")
        for payload in ('{"state": "TOGGLE"}', '{"tone": "a"}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                disc.command_to_service("siren", "s", "state", payload)

    def test_the_publisher_rejects_it_like_any_other_bad_command(self):
        pub = _publisher()
        pub._topics = {"switch.a": f"{BASE}/demo/switch/a"}
        pub.stats.update(commands=0, last_command=None)
        with self.assertLogs(mp._LOGGER, "WARNING"):
            pub._handle_message(_message(f"{BASE}/cmd/switch/a/state", "TOGGLE"))
        self.assertEqual(pub.history[-1]["state"], "rejected")
        self.assertEqual(pub.stats["commands"], 0)  # never counted, never called


class BurstCapTest(unittest.IsolatedAsyncioTestCase):
    """U2: what a burst of slow services can leave running is bounded by the caps."""

    async def test_a_burst_of_slow_calls_stops_at_the_cap(self):
        pub = _running_publisher()
        burst = 10 * mp.CALLS_IN_FLIGHT_MAX
        for i in range(burst):
            pub._on_call("light/turn_on", json.dumps({"_id": i}))
        await _settle()
        self.assertEqual(pub._in_flight, mp.CALLS_IN_FLIGHT_MAX)
        refused = [r for _d, _s, r in pub.results if r.get("ok") is False]
        self.assertEqual(len(refused), burst - mp.CALLS_IN_FLIGHT_MAX)
        self.assertTrue(all("too many calls in progress" in r["error"] for r in refused))
        # a refused call never ran, so its _id is free again; the history and the dedup memory are capped too
        self.assertEqual(len(pub._calls), mp.CALLS_IN_FLIGHT_MAX)
        self.assertEqual(len(pub.history), mp.HISTORY_MAX)
        pub.release.set()
        await _settle()
        self.assertEqual(pub._in_flight, 0)

    async def test_a_timed_out_handler_keeps_holding_its_slot(self):
        pub = _running_publisher()
        with mock.patch.object(mp, "CALL_TIMEOUT_S", 0), mock.patch.object(mp, "CALLS_IN_FLIGHT_MAX", 2):
            for i in (1, 2, 3):
                pub._on_call("light/turn_on", json.dumps({"_id": i}))
            await _settle()
            errors = [r["error"] for _d, _s, r in pub.results if r.get("ok") is False]
            self.assertEqual(len(errors), 3)
            self.assertEqual(sum("timeout after 0s" in e for e in errors), 2)
            self.assertEqual(sum("too many calls in progress" in e for e in errors), 1)
            self.assertEqual(pub._in_flight, 2)  # answered, but the handlers are still running
            pub.release.set()
            await _settle()
            self.assertEqual(pub._in_flight, 0)

    async def test_a_burst_of_slow_commands_stops_at_the_cap(self):
        pub = _running_publisher()
        pub.stats.update(commands=0, last_command=None)
        pub._topics = {"switch.a": f"{BASE}/demo/switch/a"}
        with mock.patch.object(mp, "CALLS_IN_FLIGHT_MAX", 2):
            for _ in range(20):
                pub._handle_message(_message(f"{BASE}/cmd/switch/a/state", "ON"))
            await _settle()
            self.assertEqual(pub._in_flight, 2)
            self.assertEqual(sum(r["state"] == "rejected" for r in pub.history), 18)
            pub.release.set()
            await _settle()
            self.assertEqual(pub._in_flight, 0)


if __name__ == "__main__":
    unittest.main()
