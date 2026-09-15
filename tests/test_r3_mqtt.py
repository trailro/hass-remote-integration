"""Third review, MQTT side: HA minimum version on the update entity, call scope, deep and huge payloads, non-finite
numbers, notify.persistent_notification, action limits, masked secrets, health expiry, identity leftovers, reserved
integration names in topic paths."""

import asyncio
import collections
import json
import math
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import mqtt_publisher as mp
from tests.fakes import FakeInstaller

BASE = "hass_t"


def _publisher(exclude=()):
    pub = object.__new__(mp.MqttPublisher)
    pub.hass = mock.Mock()
    pub.hass.data = {}
    pub.hass.loop.call_soon_threadsafe = lambda f: None  # the service itself never runs in these tests
    pub.history = collections.deque(maxlen=mp.HISTORY_MAX)
    pub._calls = {}
    pub.stats = {"calls": 0, "last_call": None}
    pub.config = mock.Mock(exclude_integrations=list(exclude))
    pub._moving = False
    pub._live_base = BASE
    pub._topics = {}
    pub.results = []
    pub._publish_result = lambda domain, service, result: pub.results.append((domain, service, result))
    return pub


def _message(topic, payload):
    return SimpleNamespace(topic=topic, payload=payload if isinstance(payload, bytes) else payload.encode(), retain=False)


class UpdateMinimumHaTest(unittest.TestCase):
    """#7: a tag that needs 2026.9.0 is not an update for 2026.9.0b2."""

    def test_beta_does_not_satisfy_its_release(self):
        device = md.ManagerDevice.__new__(md.ManagerDevice)
        device.installer = FakeInstaller(running="demo", versions=("1.0.0", "2.0.0"))
        device.installer.min_ha_of = lambda _d, tag: "2026.9.0" if tag == "2.0.0" else None
        with mock.patch.object(md, "HA_VERSION", "2026.9.0b2"):
            self.assertEqual(device.integration_latest(), "1.0.0")
        with mock.patch.object(md, "HA_VERSION", "2026.9.0"):
            self.assertEqual(device.integration_latest(), "2.0.0")


class CallScopeTest(unittest.TestCase):
    """#21: the target check refuses what it cannot read, expands groups, and looks at other entity fields."""

    def setUp(self):
        self.pub = _publisher()
        self.pub._topics = {"switch.published": "t", "group.published": "t", "media_player.published": "t", "light.published": "t"}
        members = {"group.published": ["switch.hidden"], "group.hidden": ["switch.published"]}
        self.pub.hass.states.get = lambda eid: State(eid, "on", {"entity_id": members[eid]}) if eid in members else None

    def test_unreadable_target_is_refused(self):
        problem = self.pub._call_target_problem({"area_id": [{}], "entity_id": "switch.hidden"})
        self.assertIsNotNone(problem)
        self.assertIsNotNone(self.pub._call_target_problem({"area_id": [{}], "entity_id": "switch.published"}))

    def test_group_members_are_checked(self):
        self.assertIn("switch.hidden", self.pub._call_target_problem({"entity_id": "group.published"}) or "")

    def test_an_unpublished_group_is_refused_even_with_published_members(self):
        self.assertIn("group.hidden", self.pub._call_target_problem({"entity_id": "group.hidden"}) or "")

    def test_entity_fields_in_the_data_are_checked(self):
        for data, hidden in (({"entity_id": "media_player.published", "group_members": ["media_player.hidden"]}, "media_player.hidden"),
                             ({"media_player_entity_id": "media_player.hidden"}, "media_player.hidden"),
                             ({"source_entity_ids": ["light.published", "light.hidden"]}, "light.hidden"),
                             ({"entities": {"light.hidden": "on"}}, "light.hidden"),
                             ({"entities": ["light.hidden"]}, "light.hidden"),
                             ({"snapshot_entities": "light.published, light.hidden"}, "light.hidden"),
                             ({"options": {"nested": {"entity_id": "light.hidden"}}}, "light.hidden")):
            with self.subTest(data=data):
                self.assertIn(hidden, self.pub._call_target_problem(data) or "")

    def test_published_entities_and_plain_data_pass(self):
        self.assertIsNone(self.pub._call_target_problem({"entity_id": "switch.published", "group_members": ["media_player.published"],
                                                         "entities": {"light.published": {"state": "on"}}, "message": "light.hidden is text"}))
        # a device_id resolves through the registries (a mock hass has none: the real path is probed against a HomeAssistant instance)
        with mock.patch("homeassistant.helpers.target.async_extract_referenced_entity_ids", return_value=mock.Mock(referenced=set(), indirectly_referenced=set())):
            self.assertIsNone(self.pub._call_target_problem({"device_id": "18:000730", "verb": "RQ", "code": "000A"}))

    def test_on_call_answers_the_refusal(self):
        pub = self.pub
        pub.hass.services.has_service.return_value = True

        async def run():
            ran = []
            pub.hass.loop.call_soon_threadsafe = lambda f: ran.append(f)
            pub.hass.async_create_task = lambda coro: coro
            pub._on_call("switch/turn_on", json.dumps({"area_id": [{}], "entity_id": "switch.hidden"}))
            await ran[0]()

        asyncio.run(run())
        self.assertEqual(len(pub.results), 1)
        self.assertFalse(pub.results[0][2]["ok"])
        pub.hass.services.async_call.assert_not_called()


class BadPayloadTest(unittest.TestCase):
    """#22, #23: deep, huge and non-finite payloads always get one answer and one history row."""

    def _call(self, payload):
        pub = _publisher()
        pub._on_message(None, None, _message(f"{BASE}/call/light/turn_on", payload))
        return pub

    def _assert_rejected(self, pub, text=None):
        self.assertEqual(len(pub.results), 1, pub.results)
        self.assertFalse(pub.results[0][2]["ok"])
        self.assertEqual(len(pub.history), 1)
        self.assertEqual(pub.history[0]["state"], "rejected")
        if text:
            self.assertIn(text, pub.results[0][2]["error"])

    def test_deep_nesting(self):
        for payload in ("{" + '"a":' * 100000 + "1" + "}" * 100000, '{"a":' + "[" * 100000 + "]" * 100000 + "}",
                        '{"_id": 7, "a": ' + "[" * 65 + "]" * 65 + "}"):
            with self.subTest(size=len(payload)):
                pub = self._call(payload)
                self._assert_rejected(pub, "bad payload")

    def test_brackets_inside_strings_do_not_count(self):
        pub = _publisher()
        pub.hass.loop.call_soon_threadsafe = lambda f: pub.results.append("scheduled")
        pub._on_call("light/turn_on", json.dumps({"entity_id": "light.x", "text": "[" * 5000 + '"\\"{'}))
        self.assertEqual(pub.results, ["scheduled"])

    def test_nesting_at_the_limit_is_accepted(self):
        pub = _publisher()
        pub.hass.loop.call_soon_threadsafe = lambda f: pub.results.append("scheduled")
        pub._on_call("light/turn_on", '{"a": ' + "[" * (mp.CALL_MAX_DEPTH - 1) + "]" * (mp.CALL_MAX_DEPTH - 1) + "}")
        self.assertEqual(pub.results, ["scheduled"])

    def test_huge_payload(self):
        pub = self._call(json.dumps({"_id": 1, "text": "x" * (mp.CALL_MAX_BYTES + 1)}))
        self._assert_rejected(pub, "bad payload")

    def test_non_finite_numbers(self):
        for value in ("1e999", "-1e999", "NaN", "Infinity"):
            with self.subTest(value=value):
                pub = self._call('{"entity_id": "light.x", "brightness": %s}' % value)
                self._assert_rejected(pub, "bad payload")
        pub = _publisher()
        pub.hass.loop.call_soon_threadsafe = lambda f: pub.results.append("scheduled")
        pub._on_call("light/turn_on", '{"brightness": 1e3}')
        self.assertEqual(pub.results, ["scheduled"])

    def test_denied_domain_with_a_deep_payload_still_answers(self):
        pub = self._call("{" + '"a":' * 100000 + "1" + "}" * 100000)
        pub2 = _publisher()
        pub2._on_message(None, None, _message(f"{BASE}/call/shell_command/x", "[" * 100000))
        self.assertEqual(len(pub2.results), 1)
        self.assertEqual(len(pub2.history), 1)
        self.assertEqual(len(pub.results), 1)


class NotifyPersistentNotificationTest(unittest.TestCase):
    """#24: notify.persistent_notification creates a persistent notification like persistent_notification.create."""

    def test_refused_over_mqtt(self):
        pub = _publisher()
        pub._on_call("notify/persistent_notification", '{"message": "fake"}')
        self.assertEqual(len(pub.results), 1)
        self.assertFalse(pub.results[0][2]["ok"])
        self.assertIn("not callable over MQTT", pub.results[0][2]["error"])
        self.assertEqual(pub.history[0]["state"], "rejected")

    def test_other_notify_services_stay_callable(self):
        pub = _publisher()
        pub.hass.loop.call_soon_threadsafe = lambda f: pub.results.append("scheduled")
        pub._on_call("notify/send_message", '{"entity_id": "notify.x", "message": "hi"}')
        self.assertEqual(pub.results, ["scheduled"])

    def test_hidden_from_the_mqtt_catalog(self):
        pub = _publisher()
        pub._connected = True
        pub._services_published = set()
        published = {}
        pub._publish_if_changed = lambda topic, payload, qos=None: published.__setitem__(topic, json.loads(payload))
        rows = [{"domain": "notify", "custom": False, "services": [{"name": "persistent_notification"}, {"name": "send_message"}]}]
        with mock.patch.object(mp, "service_rows", mock.AsyncMock(return_value=rows)):
            asyncio.run(pub._publish_services())
        self.assertEqual([s["name"] for s in published[f"{BASE}/services/notify"]["services"]], ["send_message"])
        self.assertEqual(pub.stats["services_published"], 1)


class ActionLimitFileTest(unittest.TestCase):
    """#25, #26."""

    def _device(self, d, last_run=None):
        device = md.ManagerDevice.__new__(md.ManagerDevice)
        device.hass = mock.Mock()
        device.publisher = mock.Mock()
        device.publisher.async_publish_manager_result = mock.AsyncMock()
        device.publisher.async_after_start = mock.AsyncMock()
        device._action_lock = asyncio.Lock()
        device._running = None
        device.last_action = None
        device._runs_file = os.path.join(d, "integration_manager", md.RUNS_FILE)
        device._last_run = dict(last_run or {})
        device._do_backup = mock.AsyncMock(return_value={"ok": True})
        return device

    def test_a_failed_limit_write_still_runs_and_answers(self):
        with tempfile.TemporaryDirectory() as d:
            device = self._device(d)
            with mock.patch.object(md.writer, "async_write", mock.AsyncMock(side_effect=OSError(28, "No space left on device"))), \
                    mock.patch.object(md.events, "emit"):
                res = asyncio.run(device.async_action("backup"))
            self.assertTrue(res["ok"])
            self.assertIsNone(device._running)
            device.publisher.async_publish_manager_result.assert_awaited_once()

    def test_non_finite_or_future_timestamps_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            for last in (math.inf, -math.inf, math.nan, time.time() + 10 * 365 * 86400):
                with self.subTest(last=last):
                    device = self._device(d, {"backup": last})
                    with mock.patch.object(md.events, "emit"):
                        res = asyncio.run(device.async_action("backup"))
                    self.assertTrue(res["ok"], res)

    def test_a_recent_run_still_limits_with_a_wait_within_the_interval(self):
        with tempfile.TemporaryDirectory() as d:
            for last in (time.time() - 10, time.time() + 30):  # a clock stepped back a little still counts
                device = self._device(d, {"backup": last})
                with mock.patch.object(md.events, "emit"):
                    res = asyncio.run(device.async_action("backup"))
                self.assertFalse(res["ok"])
                wait = int(res["error"].rsplit("in ", 1)[1].split()[0])
                self.assertLessEqual(wait, md.MIN_INTERVAL_S["backup"] + 1)

    def test_loaded_timestamps_skip_non_finite_values(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "integration_manager"))
            with open(os.path.join(d, "integration_manager", md.RUNS_FILE), "w") as f:
                f.write('{"backup": 1e999, "check_updates": 5, "restart": true}')
            installer = FakeInstaller()
            installer.config_dir = d
            hass = mock.Mock()
            with mock.patch.object(md, "LoopLag"):
                device = md.ManagerDevice(hass, installer, mock.Mock(), mock.Mock())
            self.assertEqual(device._last_run, {"check_updates": 5.0})


class MaskedSecretsTest(unittest.TestCase):
    """#27."""

    def test_secret_keys_are_masked(self):
        for key in ("code", "usercode", "user_code", "pin", "passcode", "password", "secret", "token", "PIN", "Code"):
            with self.subTest(key=key):
                self.assertNotIn("4321", mp._mask_codes(json.dumps({"entity_id": "lock.a", key: "4321"})))
                self.assertNotIn("4321", mp._mask_codes(f"lock.unlock {{'{key}': 4321}}"))

    def test_similar_keys_stay(self):
        for kept in ('{"code_format": "number"}', '{"zipcode": "12345"}', '{"barcode": "5"}', '{"pincode_length": 4}',
                     '{"token_type": "x"}', '{"spin": 3}', '{"code_slot": 2}', '{"passwords_enabled": true}'):
            with self.subTest(kept=kept):
                self.assertEqual(mp._mask_codes(kept), kept)


class HealthExpiryTest(unittest.TestCase):
    """#28."""

    def test_health_components_expire(self):
        topics = {"status": f"{BASE}/status", "health": f"{BASE}/health", "manager": f"{BASE}/manager", "cmd": f"{BASE}/manager/cmd"}
        comps = disc.manager_device(BASE, BASE + "_", topics, "demo", "1.0.0", True)[2]
        for eid in (f"binary_sensor.{BASE}_integration", f"sensor.{BASE}_health"):
            self.assertEqual(comps[eid]["expire_after"], 3 * mp.HEALTH_INTERVAL_S)

    def _pub(self, sample):
        pub = _publisher()
        log = []
        pub.manager = mock.Mock()
        pub.manager.async_sample = sample
        pub.publish_health = lambda: log.append("health")
        pub.publish_manager = lambda: log.append("manager")
        pub._publish_manager_discovery = lambda: log.append("discovery")
        return pub, log

    def test_health_goes_out_before_sampling_and_despite_a_failed_sample(self):
        async def boom():
            raise RuntimeError("proc unreadable")

        pub, log = self._pub(boom)
        with self.assertLogs(mp._LOGGER, "WARNING"):
            asyncio.run(pub._on_health_timer(None))
        self.assertEqual(log, ["health", "manager", "discovery"])

    def test_a_hanging_sample_does_not_hold_health(self):
        async def hang():
            await asyncio.Event().wait()

        pub, log = self._pub(hang)

        async def run():
            task = asyncio.ensure_future(pub._on_health_timer(None))
            await asyncio.sleep(0.05)
            task.cancel()

        asyncio.run(run())
        self.assertEqual(log, ["health"])


class IdentityLeftoversTest(unittest.TestCase):
    """C10."""

    def test_unregistered_entity_of_an_excluded_integration_counts(self):
        pub = _publisher(exclude=["other"])
        pub.rules = mock.Mock()
        pub.rules.for_entity.return_value = {}
        pub.hass.data = {DATA_ENTITY_PLATFORM: {"other": [SimpleNamespace(platform_name="other", entities={"sensor.yaml_only": object()})]}}
        registry = mock.Mock()
        registry.async_get.return_value = None
        with mock.patch("homeassistant.helpers.entity_registry.async_get", return_value=registry):
            self.assertTrue(pub._excluded_now("sensor.yaml_only"))
            self.assertFalse(pub._excluded_now("sensor.unknown"))

    def test_duplicate_default_entity_id_is_warned_and_counted(self):
        pub = _publisher()
        pub._collision_warned = set()
        pub._default_id_warned = set()
        pub.rules = mock.Mock()
        pub.rules.for_entity.return_value = {}
        pub.rules.apply_component = lambda comp, rule: comp
        pub.hass.states.async_all.return_value = [State("camera.front", "idle"), State("sensor.camera_front", "1")]
        pub.hass.config.components = set()
        registry = mock.Mock(entities={})
        registry.async_get.return_value = None
        comp = lambda hass, state, *a: {"platform": "sensor", "default_entity_id": "sensor.camera_front"}
        with mock.patch.object(mp.er, "async_get", return_value=registry), mock.patch.object(mp, "platform_of", return_value="demo"), \
                mock.patch.object(mp.disc, "build_component", side_effect=comp), \
                mock.patch.object(type(pub), "prefix", BASE + "_", create=True), self.assertLogs(mp._LOGGER, "WARNING") as logs:
            groups, counts = pub._group_by_device()
        self.assertEqual(sum(len(c) for _, c in groups.values()), 2)  # both still announced: the main HA names the second _2
        self.assertEqual(counts["default_id_duplicates"], 1)
        self.assertIn("sensor.camera_front", "\n".join(logs.output))


class ReservedIntegrationNamesTest(unittest.TestCase):
    """D3: an integration called "call" had its documents on <base>/call/<domain>/<object_id>, where live
    subscribers get them as service calls."""

    def test_documents_never_land_on_subscribed_or_answer_topics(self):
        pub = _publisher()
        for name in ("call", "cmd", "result", "services", "manager", "health", "status"):
            with self.subTest(name=name):
                topic = pub._topic_for("sensor.x", name)
                self.assertFalse(topic.startswith(f"{BASE}/{name}/"), topic)
                self.assertEqual(topic.split("/")[1:], [f"{name}-integration", "sensor", "x"])
        self.assertEqual(pub._topic_for("sensor.x", "ramses_cc"), f"{BASE}/ramses_cc/sensor/x")

    def test_a_document_of_integration_call_is_not_a_service_call(self):
        pub = _publisher()
        doc = {"entity_id": "light.turn_on", "state": "on", "published_at": "x"}
        pub._on_message(None, None, _message(pub._topic_for("light.turn_on", "call"), json.dumps(doc)))
        self.assertEqual((pub.results, list(pub.history)), ([], []))


if __name__ == "__main__":
    unittest.main()
