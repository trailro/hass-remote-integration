"""The six MQTT findings of the external review, each with the shape that used to go wrong.

F-04  an MQTT 3.1.1 broker echoes our own clear of a retained command back, and it was read as a value
F-09  "remove orphans" for a manager entity published a removal key the main Home Assistant never saw
F-10  a config entity category on a mirrored or rule-edited sensor, which the main Home Assistant refuses
F-11  a service call's _id had no size limit and was kept in three places
F-12  the dedup cache remembered refusals of calls that never ran
F-18  parity compared states that can never match (button, scene, notify, event)
"""

import asyncio
import collections
import inspect
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import mqtt_rules as mr
from custom_components.integration_manager import parity

BASE = "hass_fix"
PREFIX = BASE + "_"


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0)


def _publisher(loop=None, calls=None, **config):
    """A publisher built field by field: no HA instance, no broker (as the other MQTT tests do)."""
    pub = object.__new__(mp.MqttPublisher)
    pub.hass = mock.Mock()
    pub.hass.data = {}
    if loop is not None:
        pub.hass.loop = loop
        pub.hass.async_create_task = loop.create_task
    else:
        pub.hass.loop.call_soon_threadsafe = lambda f: f()
        pub.hass.async_create_task = lambda coro: coro.close()
    if calls is not None:
        async def async_call(domain, service, data, blocking=True, return_response=False):
            calls.append((domain, service, dict(data)))
        pub.hass.services.async_call = async_call
    pub.config = mp.MqttConfig(**config)
    pub.history = collections.deque(maxlen=mp.HISTORY_MAX)
    pub._calls = {}
    pub._calls_lock = threading.Lock()
    pub._in_flight = 0
    pub._cleared_cmds = {}
    pub.stats = {"calls": 0, "commands": 0, "last_call": None, "last_command": None, "published": 0, "cleared": 0,
                 "unchanged_skipped": 0, "oversized_skipped": 0, "last_oversized": None, "services_published": 0,
                 "connected": True, "connect_error": ""}
    pub._client = FakeClient()
    pub._connected = True
    pub._connected_at = 0.0
    pub._broker_max_packet = 0
    pub._oversized_warned = set()
    pub._last_hash = {}
    pub._last_disconnect = ""
    pub._manager_absent_sent = True
    pub._moving = False
    pub._stopping = False
    pub._live_base = BASE
    pub._live_prefix = PREFIX
    pub._key_provider = lambda: BASE
    pub._topics = {}
    pub._range_pending = {}
    pub._services_published = set()
    pub.results = []
    return pub


class Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload.encode(), retain


class RetainedClearEchoTest(unittest.TestCase):
    """F-04: on MQTT 3.1.1 the subscription has no noLocal, and the broker strips the retain flag on a live
    delivery, so our own clear of a retained command comes back looking like a command whose payload is ""
    - which text and notify are the two platforms that take as a value."""

    def _scenario(self, domain, object_id, field, second_payload="", pause=False):
        async def run():
            calls = []
            pub = _publisher(asyncio.get_running_loop(), calls, qos=1)
            pub._topics = {f"{domain}.{object_id}": f"{BASE}/x/{domain}/{object_id}"}
            pub.hass.states.get.return_value = mock.Mock(attributes={})
            topic = f"{BASE}/cmd/{domain}/{object_id}/{field}"
            pub._handle_message(Msg(topic, "hello", retain=True))
            cleared = [p for p in pub._client.published if p[0] == topic and p[1] in ("", None)]
            if pause:  # the echo arrives after the window: it is a command again, not an echo
                pub._cleared_cmds = {t: at - mp.CLEARED_ECHO_WINDOW_S - 1 for t, at in pub._cleared_cmds.items()}
            pub._handle_message(Msg(topic, second_payload))
            for _ in range(6):
                await asyncio.sleep(0)
            return cleared, calls, pub
        return asyncio.run(run())

    def test_the_echoed_clear_of_a_text_command_is_not_an_empty_write(self):
        cleared, calls, pub = self._scenario("text", "note", "value")
        self.assertEqual(len(cleared), 1)  # the retained command was cleared, as before
        self.assertEqual(calls, [])  # and the clear coming back set nothing
        self.assertEqual(list(pub.history), [])  # the retained command is refused without a history row, as before

    def test_the_echoed_clear_of_a_notify_command_sends_nothing(self):
        _cleared, calls, _pub = self._scenario("notify", "phone", "message")
        self.assertEqual(calls, [])

    def test_only_the_first_empty_payload_on_that_topic_is_dropped(self):
        """A second one is a real (and refused) command of "", not the echo."""
        async def run():
            calls = []
            pub = _publisher(asyncio.get_running_loop(), calls, qos=1)
            pub._topics = {"text.note": f"{BASE}/x/text/note"}
            pub.hass.states.get.return_value = mock.Mock(attributes={})
            topic = f"{BASE}/cmd/text/note/value"
            pub._handle_message(Msg(topic, "hello", retain=True))
            pub._handle_message(Msg(topic, ""))  # the echo
            pub._handle_message(Msg(topic, ""))  # a real empty command from the main HA
            for _ in range(6):
                await asyncio.sleep(0)
            return calls
        calls = asyncio.run(run())
        self.assertEqual(calls, [("text", "set_value", {"entity_id": "text.note", "value": ""})])

    def test_an_empty_payload_long_after_the_clear_is_a_command_again(self):
        _cleared, calls, _pub = self._scenario("text", "note", "value", pause=True)
        self.assertEqual(calls, [("text", "set_value", {"entity_id": "text.note", "value": ""})])

    def test_a_real_command_right_after_the_clear_still_runs(self):
        _cleared, calls, _pub = self._scenario("text", "note", "value", second_payload="hello again")
        self.assertEqual(calls, [("text", "set_value", {"entity_id": "text.note", "value": "hello again"})])

    def test_the_topics_remembered_are_bounded(self):
        pub = _publisher()
        for i in range(mp.CLEARED_ECHO_MAX * 3):
            pub._note_cleared(f"{BASE}/cmd/text/n{i}/value")
        self.assertLessEqual(len(pub._cleared_cmds), mp.CLEARED_ECHO_MAX)

    def test_the_mqtt5_subscription_still_asks_for_nolocal(self):
        """The fix is the belt for 3.1.1; MQTT 5 keeps solving it at the broker."""
        self.assertIn("noLocal=True", inspect.getsource(mp.MqttPublisher._on_connect))


class ManagerOrphanRemovalTest(unittest.TestCase):
    """F-09: parity derives our_entity_id from the unique id, and the manager's components are not named after
    theirs (unique id manager_restart, component key button_<base>_restart)."""

    def _manager(self):
        return disc.manager_device(BASE, PREFIX, dict.fromkeys(("status", "health", "manager", "cmd"), "t"),
                                   "demo", "1.0", True)

    def test_a_manager_unique_id_maps_back_to_its_component(self):
        _did, _block, comps = self._manager()
        for entity_id, comp in comps.items():
            with self.subTest(entity_id=entity_id):
                self.assertEqual(disc.manager_entity_id(BASE, comp["unique_id"][len(PREFIX):]), entity_id)

    def test_an_unknown_unique_id_maps_to_nothing(self):
        self.assertIsNone(disc.manager_entity_id(BASE, "manager_not_a_component"))

    def test_the_removal_form_carries_the_key_the_parent_was_given(self):
        did, block, comps = self._manager()
        pub = _publisher(manager_discovery=True, manager_commands=True)
        pub._group_by_device = lambda: ({}, {})
        pub._manager_discovery = lambda: (did, block, comps)
        # what parity sends for the restart button: the unique id without the prefix
        self.assertTrue(pub.remove_discovered_component(did, "manager_restart", "button"))
        payload = json.loads(pub._client.published[-1][1])
        self.assertEqual(payload["components"]["button_hass_fix_restart"], {"platform": "button"})
        self.assertNotIn("manager_restart", payload["components"])
        # every sibling is still announced with it
        self.assertIn("sensor_hass_fix_health", payload["components"])

    def test_the_health_entities_map_too(self):
        did, block, comps = self._manager()
        pub = _publisher(manager_discovery=True, manager_commands=True)
        pub._group_by_device = lambda: ({}, {})
        pub._manager_discovery = lambda: (did, block, comps)
        self.assertTrue(pub.remove_discovered_component(did, "health_online", "binary_sensor"))
        payload = json.loads(pub._client.published[-1][1])
        self.assertEqual(payload["components"]["binary_sensor_hass_fix_integration"], {"platform": "binary_sensor"})

    def test_a_unique_id_no_manager_component_has_is_refused(self):
        """It reported success and removed nothing; there is nothing of ours under that key."""
        did, block, comps = self._manager()
        pub = _publisher(manager_discovery=True, manager_commands=True)
        pub._group_by_device = lambda: ({}, {})
        pub._manager_discovery = lambda: (did, block, comps)
        self.assertFalse(pub.remove_discovered_component(did, "manager_nonsense", "button"))
        self.assertEqual(pub._client.published, [])

    def test_a_normal_device_entity_is_untouched(self):
        pub = _publisher(discovery_enabled=True)
        pub._group_by_device = lambda: ({f"{BASE}_dev": ({"identifiers": [f"{BASE}_dev"]}, {"sensor.b": {"platform": "sensor"}})}, {})
        pub._manager_discovery = lambda: (f"{BASE}_manager", {"identifiers": [f"{BASE}_manager"]}, {})
        self.assertTrue(pub.remove_discovered_component(f"{BASE}_dev", "sensor.a", "sensor"))
        payload = json.loads(pub._client.published[-1][1])
        self.assertEqual(payload["components"]["sensor_a"], {"platform": "sensor"})


class ConfigEntityCategoryTest(unittest.TestCase):
    """F-10: Home Assistant refuses a sensor or binary_sensor whose entity category is config, and the entity
    then never appears on the main HA - which parity reports as permanently missing."""

    def test_home_assistant_refuses_it_on_exactly_these_platforms(self):
        """Read off the Home Assistant installed here, so the constant cannot drift away from it."""
        import pathlib

        import homeassistant.components as components
        root = pathlib.Path(components.__file__).parent
        refused = set()
        for domain in set(disc.NATIVE) | mr.CONFIG_CATEGORY_REFUSED:
            init = root / domain / "__init__.py"
            if init.exists() and "entity category is set to config" in init.read_text(encoding="utf-8"):
                refused.add(domain)
        self.assertEqual(refused, set(mr.CONFIG_CATEGORY_REFUSED))

    def test_a_mirrored_entity_loses_the_config_category(self):
        from homeassistant.core import State
        from homeassistant.helpers import entity_registry as er
        entry = mock.Mock(spec=er.RegistryEntry)
        entry.name, entry.original_name = "Schedule", None
        entry.icon = entry.original_icon = None
        entry.entity_category = SimpleNamespace(value="config")
        entry.disabled = False
        comp = disc._mirror_as_sensor(entry, State("date.schedule", "2026-01-01", {}, validate_entity_id=False),
                                      f"{BASE}/x/date/schedule", PREFIX)
        self.assertEqual(comp["platform"], "sensor")
        self.assertEqual(comp["entity_category"], "diagnostic")

    def test_a_diagnostic_mirror_keeps_its_category(self):
        from homeassistant.core import State
        from homeassistant.helpers import entity_registry as er
        entry = mock.Mock(spec=er.RegistryEntry)
        entry.name, entry.original_name = "Uptime", None
        entry.icon = entry.original_icon = None
        entry.entity_category = SimpleNamespace(value="diagnostic")
        entry.disabled = False
        comp = disc._mirror_as_sensor(entry, State("remote.tv", "on", {}, validate_entity_id=False),
                                      f"{BASE}/x/remote/tv", PREFIX)
        self.assertEqual(comp["entity_category"], "diagnostic")

    def test_a_native_platform_keeps_a_config_category(self):
        """Only the read-only platforms refuse it: a button or a number announced as itself keeps it."""
        comp = {"platform": "button", "unique_id": "u"}
        self.assertIsNone(mr.entity_category_problem(comp, "config"))
        self.assertEqual(mr.MqttRules.apply_component(mr.MqttRules("/nonexistent.json"), comp, {"entity_category": "config"})["entity_category"], "config")

    def test_a_rule_cannot_put_config_on_a_sensor(self):
        rules = mr.MqttRules("/nonexistent.json")
        comp = {"platform": "sensor", "unique_id": "u", "name": "n"}
        out = rules.apply_component(comp, {"entity_category": "config", "name": "Renamed"})
        self.assertNotIn("entity_category", out)
        self.assertEqual(out["name"], "Renamed")  # the rest of the rule still applies
        binary = rules.apply_component({"platform": "binary_sensor", "unique_id": "b"}, {"entity_category": "config"})
        self.assertNotIn("entity_category", binary)

    def test_a_rule_can_still_set_diagnostic(self):
        rules = mr.MqttRules("/nonexistent.json")
        out = rules.apply_component({"platform": "sensor", "unique_id": "u"}, {"entity_category": "diagnostic"})
        self.assertEqual(out["entity_category"], "diagnostic")

    def test_setting_the_rule_is_refused_like_an_unfit_device_class(self):
        rules = mr.MqttRules("/nonexistent.json")
        rules.components = lambda pattern: [("sensor.power", {"platform": "sensor", "unique_id": "u"})]
        with self.assertRaises(ValueError) as caught:
            rules.set("sensor.power", entity_category="config")
        self.assertIn("entity_category config does not fit sensor.power", str(caught.exception))
        self.assertEqual(rules.rules, {})
        with self.assertRaises(ValueError):
            rules.replace_all({"sensor.power": {"entity_category": "config"}})
        rules.set("sensor.power", entity_category="diagnostic")
        self.assertEqual(rules.rules["sensor.power"], {"entity_category": "diagnostic"})


class CallIdSizeTest(unittest.TestCase):
    """F-11: the _id is held in the dedup map for DEDUP_WINDOW_S, kept in the history and echoed in every
    result and every /api/mqtt/commands poll."""

    def _call(self, payload, has_service=True):
        async def run():
            pub = _publisher(asyncio.get_running_loop())
            pub.hass.services.has_service.return_value = has_service
            pub.hass.services.supports_response.return_value = mock.Mock()
            answers = []
            pub._publish_result = lambda d, s, res: answers.append(res)
            pub._on_call("light/turn_on", payload)
            for _ in range(8):
                await asyncio.sleep(0)
            return answers, pub
        return asyncio.run(run())

    def test_an_oversized_id_is_refused_with_an_answer(self):
        big = "z" * 250_000
        answers, pub = self._call(json.dumps({"_id": big, "entity_id": "light.a"}))
        self.assertEqual(len(answers), 1)
        self.assertIn("at most 128", answers[0]["error"])
        self.assertFalse(answers[0]["ok"])
        self.assertEqual(pub._calls, {})  # nothing remembered for the dedup window
        self.assertLessEqual(len(json.dumps(answers[0])), 500)  # neither the answer
        self.assertLessEqual(len(str(pub.history[-1]["id"])), mp.CALL_ID_MAX_BYTES)  # nor the history row
        self.assertTrue(str(answers[0]["id"]).endswith("…"))  # enough to recognise the call

    def test_an_oversized_id_of_any_shape_is_refused(self):
        for value in ({"a": "y" * 1000}, ["y" * 1000], "y" * 200):
            with self.subTest(value=type(value).__name__):
                answers, pub = self._call(json.dumps({"_id": value, "entity_id": "light.a"}))
                self.assertIn("at most 128", answers[0]["error"])
                self.assertEqual(pub._calls, {})

    def test_an_id_that_fits_is_kept_exactly(self):
        async def run():
            pub = _publisher(asyncio.get_running_loop())
            DedupOfRefusalsTest._known(pub)
            answers = []
            pub._publish_result = lambda d, s, res: answers.append(res)
            pub._on_call("light/turn_on", json.dumps({"_id": "automation-42", "entity_id": "light.a"}))
            for _ in range(8):
                await asyncio.sleep(0)
            return answers, pub
        answers, pub = asyncio.run(run())
        self.assertEqual([a["id"] for a in answers], ["automation-42"])
        self.assertEqual(list(pub._calls), [mp._call_key("light", "turn_on", "automation-42")])

    def test_the_limit_counts_bytes_not_characters(self):
        self.assertIsNone(mp._call_id_problem("é" * 60))          # 122 bytes as JSON
        self.assertIsNotNone(mp._call_id_problem("é" * 70))       # 142
        self.assertIsNone(mp._call_id_problem(None))
        self.assertIsNone(mp._call_id_problem(12345))

    def test_an_oversized_id_in_a_payload_that_never_parsed_is_cut_too(self):
        big = "z" * 250_000
        answers, pub = self._call('{"_id": "' + big + '", "entity_id": ')  # broken JSON
        self.assertLessEqual(len(str(answers[0]["id"])), mp.CALL_ID_MAX_BYTES)
        self.assertLessEqual(len(str(pub.history[-1]["id"])), mp.CALL_ID_MAX_BYTES)

    def test_a_crash_answer_cuts_it_as_well(self):
        pub = _publisher()
        pub._publish_result = lambda d, s, res: pub.results.append(res)
        pub._call_crashed("light/turn_on", json.dumps({"_id": "z" * 250_000}), ValueError("boom"))
        self.assertLessEqual(len(str(pub.results[-1]["id"])), mp.CALL_ID_MAX_BYTES)
        self.assertLessEqual(len(str(pub.history[-1]["id"])), mp.CALL_ID_MAX_BYTES)


class DedupOfRefusalsTest(unittest.TestCase):
    """F-12: a call refused before async_call never ran, so its _id must not be answered from history for the
    next five minutes - an integration still loading at boot answers "unknown service" for a moment."""

    def _twice(self, first, second, payload=None):
        payload = payload or json.dumps({"_id": "boot-1", "entity_id": "light.a"})

        async def run():
            pub = _publisher(asyncio.get_running_loop())
            answers = []
            pub._publish_result = lambda d, s, res: answers.append(res)
            for setup in (first, second):
                setup(pub)
                pub._on_call("light/turn_on", payload)
                for _ in range(8):
                    await asyncio.sleep(0)
            return answers, pub
        return asyncio.run(run())

    @staticmethod
    def _unknown(pub):
        pub.hass.services.has_service.return_value = False

    @staticmethod
    def _known(pub):
        pub.hass.services.has_service.return_value = True
        pub.hass.services.supports_response.return_value = mock.Mock()
        pub._call_target_problem = lambda *a: None
        pub._in_flight = 0
        pub.hass.services.async_call = mock.AsyncMock()

    def test_unknown_service_at_boot_does_not_poison_the_id(self):
        answers, _pub = self._twice(self._unknown, self._known)
        self.assertIn("unknown service", answers[0]["error"])
        self.assertNotIn("duplicate", answers[0])
        self.assertTrue(answers[-1]["ok"])  # the retry ran
        self.assertNotIn("duplicate", answers[-1])

    def test_a_refused_target_does_not_poison_the_id(self):
        def missing(pub):
            pub.hass.services.has_service.return_value = True
            pub.hass.services.supports_response.return_value = mock.Mock()
            pub._call_target_problem = lambda *a: "light.a is not an entity this container publishes"

        answers, _pub = self._twice(missing, self._known)
        self.assertIn("not an entity", answers[0]["error"])
        self.assertTrue(answers[-1]["ok"])

    def test_the_in_flight_cap_still_does_not_poison_it(self):
        def full(pub):
            pub.hass.services.has_service.return_value = True
            pub.hass.services.supports_response.return_value = mock.Mock()
            pub._call_target_problem = lambda *a: None
            pub._in_flight = mp.CALLS_IN_FLIGHT_MAX

        answers, _pub = self._twice(full, self._known)
        self.assertIn("too many calls", answers[0]["error"])
        self.assertTrue(answers[-1]["ok"])

    def test_a_call_that_really_ran_is_still_answered_from_history(self):
        answers, pub = self._twice(self._known, self._known)
        self.assertTrue(answers[0]["ok"])
        self.assertTrue(answers[-1]["duplicate"])
        self.assertEqual(len(pub._calls), 1)

    def test_an_internal_error_is_still_answered_from_history(self):
        """Not a refusal: the contract of test_r14_mqtt is that a repeat gets that answer back, rather than
        being sent down the same broken path again."""
        def crashing(pub):
            pub.hass.services.has_service.return_value = True
            pub.hass.services.supports_response = mock.Mock(side_effect=RuntimeError("the service went away"))
            pub._call_target_problem = lambda *a: None
            pub._internal_error = lambda what, err: f"internal error in {what}"

        answers, _pub = self._twice(crashing, self._known)
        self.assertIn("internal error", answers[0]["error"])
        self.assertTrue(answers[-1]["duplicate"])


class ParityStateComparisonTest(unittest.TestCase):
    """F-18: for button, scene, notify and event the state is a local "last triggered" timestamp on each side
    (the command-only platforms have no state topic at all), so a raw comparison always differs."""

    def _parity(self, platform, entity_id, ours_state, parent_state):
        hass = mock.Mock()
        hass.states.async_entity_ids.return_value = []
        hass.states.get.return_value = SimpleNamespace(state=ours_state)
        publisher = mock.Mock(prefix=PREFIX, base_topic=BASE)
        publisher.config.discovery_enabled, publisher.config.manager_discovery = True, True
        uid = PREFIX + entity_id
        publisher.discovery_preview.return_value = [{
            "discovery_id": f"{BASE}_dev", "device": {"name": "dev"},
            "components": {entity_id: {"unique_id": uid, "platform": platform, "name": "n",
                                       "default_entity_id": entity_id}}}]
        client = mock.Mock()
        client.url = "http://parent"
        client.commands = mock.AsyncMock(return_value=[
            [{"unique_id": uid, "platform": "mqtt", "entity_id": entity_id}],
            [],
            [{"entity_id": entity_id, "state": parent_state}],
            {"components": ["mqtt"], "version": "2026.9.3"},
        ])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            return asyncio.run(parity.compute_parity(hass, mock.Mock(), publisher))

    def test_a_button_whose_timestamps_differ_is_not_flagged(self):
        res = self._parity("button", "button.restart", "2026-09-19T10:00:00+00:00", "2026-09-19T11:22:33+00:00")
        (row,) = res["matched"]
        self.assertFalse(row["state_differs"])
        self.assertFalse(row["state_comparable"])
        self.assertEqual(res["summary"]["state_differs"], 0)

    def test_the_other_command_only_platforms_too(self):
        for platform, entity_id in (("scene", "scene.movie"), ("notify", "notify.phone"), ("event", "event.doorbell")):
            with self.subTest(platform=platform):
                res = self._parity(platform, entity_id, "2026-09-19T10:00:00+00:00", "unknown")
                self.assertFalse(res["matched"][0]["state_differs"])

    def test_a_sensor_whose_state_really_differs_is_still_flagged(self):
        res = self._parity("sensor", "sensor.power", "21.5", "19.0")
        (row,) = res["matched"]
        self.assertTrue(row["state_differs"])
        self.assertTrue(row["state_comparable"])
        self.assertEqual(res["summary"]["state_differs"], 1)

    def test_a_matching_sensor_is_not_flagged(self):
        (row,) = self._parity("sensor", "sensor.power", "21.5", "21.5")["matched"]
        self.assertFalse(row["state_differs"])

    def test_a_button_unavailable_on_the_parent_is_still_reported(self):
        """Availability is real on those platforms: only the state comparison goes."""
        (row,) = self._parity("button", "button.restart", "2026-09-19T10:00:00+00:00", "unavailable")["matched"]
        self.assertTrue(row["parent_unavailable"])
        self.assertFalse(row["state_differs"])


class OrphanSweepSetTest(unittest.TestCase):
    """C-4: the set of live document topics was rebuilt inside the loop over every retained topic."""

    def test_the_live_topic_set_is_built_once(self):
        source = inspect.getsource(mp.MqttPublisher._async_sweep_orphans)
        self.assertIn("live_topics = set(self._topics.values())", source)
        body = source.split("for topic, payload in found.items():", 1)[1]
        self.assertNotIn("set(self._topics.values())", body)


if __name__ == "__main__":
    unittest.main()
