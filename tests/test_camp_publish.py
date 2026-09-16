"""Test campaign, publishing side: the size cap that keeps one oversized document from looping the bridge,
the honest disconnect message, the target check narrowed to what an entity service can reach, the call id and
the shape of the pre-parse refusals, and the service catalog cleared when the integration is stopped."""

import asyncio
import collections
import json
import unittest
from functools import partial
from types import SimpleNamespace
from unittest import mock

from homeassistant.helpers.service import entity_service_call

from custom_components.integration_manager import mqtt_publisher as mp

BASE = "hass_camp"


class FakeClient:
    def __init__(self, rc=0):
        self.published = []
        self.subscribed = []
        self._rc = rc

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self._rc)

    def subscribe(self, topics):
        self.subscribed.append(topics)
        return (0, len(self.subscribed))  # paho: (rc, mid)


def _publisher(**config):
    """A publisher with a connected fake client, built field by field (no HA instance, no broker)."""
    pub = object.__new__(mp.MqttPublisher)
    pub.hass = mock.Mock()
    pub.hass.data = {}
    pub.hass.loop.call_soon_threadsafe = lambda f: f()
    pub.hass.async_create_task = lambda coro: coro.close()  # the republish a connect schedules never runs here
    pub.config = mp.MqttConfig(**config)
    pub.history = collections.deque(maxlen=mp.HISTORY_MAX)
    pub._calls = {}
    pub.stats = {"calls": 0, "last_call": None, "published": 0, "cleared": 0, "unchanged_skipped": 0,
                 "oversized_skipped": 0, "last_oversized": None, "services_published": 0,
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
    pub._live_prefix = BASE + "_"
    pub._key_provider = lambda: BASE
    pub._topics = {}
    pub._services_published = set()
    pub.results = []
    return pub


class PublishSizeCapTest(unittest.TestCase):
    """A document over the maximum packet size is skipped: sending it costs the connection, and paho
    replays the queued QoS 1 message on every reconnect until a new client is built."""

    def test_oversized_document_is_not_sent(self):
        pub = _publisher(qos=1)
        big = "x" * (mp.PUBLISH_MAX_BYTES + 1)
        self.assertFalse(pub._publish(f"{BASE}/light/light/big", big))
        self.assertEqual(pub._client.published, [])
        self.assertEqual(pub.stats["oversized_skipped"], 1)
        self.assertIn(f"{BASE}/light/light/big", pub.stats["last_oversized"])

    def test_a_document_that_fits_still_goes_out(self):
        pub = _publisher(qos=1)
        self.assertTrue(pub._publish(f"{BASE}/light/light/small", "x" * 1000))
        self.assertEqual(len(pub._client.published), 1)
        self.assertEqual(pub.stats["oversized_skipped"], 0)

    def test_the_topic_counts_towards_the_limit(self):
        pub = _publisher()
        topic = f"{BASE}/" + "t" * 500
        self.assertFalse(pub._publish(topic, "x" * (mp.PUBLISH_MAX_BYTES - 400)))

    def test_reported_once_per_topic_per_connection(self):
        pub = _publisher()
        big = "x" * (mp.PUBLISH_MAX_BYTES + 1)
        with self.assertLogs(mp._LOGGER, "ERROR") as logs:
            for _ in range(3):
                pub._publish(f"{BASE}/a", big)
            pub._publish(f"{BASE}/b", big)
        self.assertEqual(len(logs.output), 2)
        self.assertIn(f"{BASE}/a", logs.output[0])
        self.assertEqual(pub.stats["oversized_skipped"], 4)

    def test_a_reconnect_reports_it_again(self):
        pub = _publisher()
        big = "x" * (mp.PUBLISH_MAX_BYTES + 1)
        pub._publish(f"{BASE}/a", big)
        pub._on_connect(pub._client, None, {}, 0)
        with self.assertLogs(mp._LOGGER, "ERROR") as logs:
            pub._publish(f"{BASE}/a", big)
        self.assertEqual(len(logs.output), 1)

    def test_an_announced_maximum_wins(self):
        """MQTT 5: the broker states what it accepts."""
        pub = _publisher()
        self.assertEqual(pub._publish_limit(), mp.PUBLISH_MAX_BYTES)
        pub._on_connect(pub._client, None, {}, 0, SimpleNamespace(MaximumPacketSize=4096))
        self.assertEqual(pub._publish_limit(), 4096)
        pub._client.published.clear()
        self.assertFalse(pub._publish(f"{BASE}/light/light/x", "x" * 5000))
        self.assertEqual(pub._client.published, [])

    def test_an_oversized_result_is_answered_without_the_response(self):
        pub = _publisher()
        pub._publish_result("todo", "get_items", {"id": "t1", "service": "todo.get_items", "ok": True,
                                                  "response": "x" * (mp.PUBLISH_MAX_BYTES + 1)})
        self.assertEqual(len(pub._client.published), 1)
        answer = json.loads(pub._client.published[0][1])
        self.assertEqual(answer["id"], "t1")
        self.assertEqual(answer["service"], "todo.get_items")
        self.assertFalse(answer["ok"])
        self.assertIn("maximum", answer["error"])


class DisconnectMessageTest(unittest.TestCase):
    """A broker that drops an established connection is not a TLS problem: saying so sends the operator
    after the wrong setting while the real cause (an oversized packet) keeps looping the bridge."""

    def _disconnect(self, pub):
        pub._on_disconnect(pub._client, None, {}, "Unspecified error")
        return pub.stats["connect_error"]

    def test_a_drop_right_after_connecting_does_not_blame_tls(self):
        pub = _publisher()
        pub._on_connect(pub._client, None, {}, 0)
        message = self._disconnect(pub)
        self.assertNotIn("TLS listener", message)
        self.assertIn("closed the connection", message)

    def test_a_drop_without_a_connection_still_suggests_tls(self):
        pub = _publisher()
        self.assertIn("if the port is a TLS listener", self._disconnect(pub))

    def test_a_long_lived_connection_blames_neither(self):
        pub = _publisher()
        pub._on_connect(pub._client, None, {}, 0)
        pub._connected_at -= mp.DROP_AFTER_CONNECT_S + 1
        message = self._disconnect(pub)
        self.assertNotIn("TLS listener", message)
        self.assertNotIn("closed the connection", message)


class ServiceReachTest(unittest.TestCase):
    """An area, floor or label holds entities of every domain; an entity service reaches only its own.
    Excluding one light must not refuse every area-scoped call of every other domain."""

    def setUp(self):
        self.pub = _publisher()
        self.pub._topics = {"switch.published": "t", "light.published": "t"}
        self.selected = SimpleNamespace(referenced=set(), indirectly_referenced=set())
        patcher = mock.patch("homeassistant.helpers.target.async_extract_referenced_entity_ids",
                             side_effect=lambda *a, **k: self.selected)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _register(self, domain, service, entity_service=True):
        # the registration form Home Assistant uses: partial(entity_service_call, hass, entities, func)
        target = partial(entity_service_call, self.pub.hass, {}, None) if entity_service else (lambda call: None)
        self.pub.hass.services.async_services_for_domain = lambda d: (
            {service: SimpleNamespace(job=SimpleNamespace(target=target))} if d == domain else {})

    def test_an_entity_of_another_domain_in_the_area_does_not_refuse_the_call(self):
        self.selected.indirectly_referenced = {"switch.published", "light.excluded"}
        self._register("switch", "toggle")
        self.assertIsNone(self.pub._call_target_problem({"area_id": "kitchen"}, "switch", "toggle"))

    def test_an_unpublished_entity_of_the_service_domain_still_refuses_it(self):
        self.selected.indirectly_referenced = {"switch.excluded", "light.published"}
        self._register("switch", "toggle")
        self.assertIn("switch.excluded", self.pub._call_target_problem({"area_id": "kitchen"}, "switch", "toggle") or "")

    def test_an_entity_id_the_caller_named_is_still_checked(self):
        self.selected.referenced = {"light.excluded"}
        self._register("switch", "toggle")
        self.assertIn("light.excluded", self.pub._call_target_problem({"entity_id": "light.excluded"}, "switch", "toggle") or "")

    def test_an_entity_id_elsewhere_in_the_payload_is_still_checked(self):
        self.selected.indirectly_referenced = {"switch.published"}
        self._register("switch", "toggle")
        problem = self.pub._call_target_problem({"area_id": "kitchen", "group_members": ["light.excluded"]},
                                                "switch", "toggle")
        self.assertIn("light.excluded", problem or "")

    def test_a_service_that_is_not_an_entity_service_keeps_the_strict_check(self):
        """Its handler may do anything with an area_id, so nothing in the area may be out of bounds."""
        self.selected.indirectly_referenced = {"light.excluded"}
        self._register("hri_probe", "notify", entity_service=False)
        self.assertIn("light.excluded", self.pub._call_target_problem({"area_id": "kitchen"}, "hri_probe", "notify") or "")

    def test_a_platform_entity_service_reaches_the_domains_it_registered(self):
        self.pub._topics["media_player.published"] = "t"
        self.pub.hass.data = {"domain_platform_entities": {("media_player", "hri_probe"): {}}}
        self.selected.indirectly_referenced = {"media_player.published", "light.excluded"}
        self._register("hri_probe", "play")
        self.assertIsNone(self.pub._call_target_problem({"area_id": "kitchen"}, "hri_probe", "play"))

    def test_without_a_service_named_the_check_stays_strict(self):
        self.selected.indirectly_referenced = {"light.excluded"}
        self.assertIn("light.excluded", self.pub._call_target_problem({"area_id": "kitchen"}) or "")


class RefusedCallShapeTest(unittest.TestCase):
    """A refusal a consumer cannot correlate or route is a refusal it cannot process."""

    def setUp(self):
        self.pub = _publisher()
        self.pub._publish_result = lambda domain, service, result: self.pub.results.append((domain, service, result))

    def test_a_denied_domain_keeps_the_call_id(self):
        self.pub._on_call("homeassistant/restart", json.dumps({"_id": "c7"}))
        self.assertEqual(self.pub.results[0][2]["id"], "c7")
        self.assertEqual(self.pub.history[0]["id"], "c7")

    def test_a_bad_topic_keeps_the_call_id(self):
        self.pub._on_call("light/turn_on/extra", json.dumps({"_id": "c9"}))
        self.assertEqual(self.pub.history[0]["id"], "c9")

    def test_a_bad_service_name_keeps_the_call_id(self):
        self.pub._on_call("light/TURN ON", json.dumps({"_id": "c10"}))
        self.assertEqual(self.pub.history[0]["id"], "c10")

    def test_an_empty_payload_is_answered_like_every_other_result(self):
        self.pub._reject_empty_call("light/turn_on")
        self.assertEqual(len(self.pub.results), 1)
        result = self.pub.results[0][2]
        self.assertEqual(result["service"], "light.turn_on")
        self.assertIsNone(result["id"])
        self.assertFalse(result["ok"])


class StoppedCatalogTest(unittest.TestCase):
    """A stopped integration has no services: the retained catalog must not keep advertising them."""

    def _stopped(self):
        pub = _publisher(enabled=False, discovery_enabled=True)
        pub._key_provider = lambda: None  # nothing running any more: the stop identity
        pub._services_published = {"light", "switch"}
        pub._pending_clears = set()
        pub._registry_timer = pub._services_timer = None
        pub._republish_interval = pub.config.republish_interval_s
        pub._load = lambda: pub.config
        pub.build_health = lambda: {"state": "stopped"}
        pub.publish_health = lambda: None
        pub._disconnect = lambda publish_offline=True: None

        async def executor(func, *args):
            return func(*args)

        pub.hass.async_add_executor_job = executor
        asyncio.run(pub._async_reconnect_locked())
        return pub

    def test_the_catalog_is_cleared_on_stop(self):
        pub = self._stopped()
        cleared = {topic for topic, payload, _qos, _retain in pub._client.published if payload == ""}
        self.assertEqual(cleared, {f"{BASE}/services/light", f"{BASE}/services/switch"})

    def test_the_health_document_still_goes_out(self):
        pub = self._stopped()
        self.assertIn(f"{BASE}/health", [topic for topic, *_ in pub._client.published])

    def test_a_later_start_publishes_the_catalog_again(self):
        pub = self._stopped()
        self.assertEqual(pub._services_published, set())
        self.assertIsNone(pub._last_hash.get(f"{BASE}/services/light"))


if __name__ == "__main__":
    unittest.main()
