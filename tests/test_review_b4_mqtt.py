"""Review of b4cd1a1, MQTT side.  S3-1: the value of a text.set_value call for a text entity in password mode was kept
in clear in the command history when the call was refused before it was masked (an oversized _id, a denied domain,
a payload that does not parse), and a number was never masked at all.  S3-2: an entity moved to another device while
the container was down stayed in its old device's retained config.  S3-5: a version switch cleared the retained
documents of disabled entities, whose discovery components stay and read them.  End-to-end run: saving the MQTT
settings opened mqtt.json on the event loop, and the command history showed a masked value in a data field's text as
token=\"***\": the quotes it added ended the JSON string around it."""

import asyncio
import builtins
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
from tests import test_camp_publish as camp
from tests.test_e2e_pub_discovery import StartWindowCase, _entry
from tests.test_r14_mqtt import _password_publisher

SECRET = "hunter2"


class PasswordValueMaskedOnEveryRefusalTest(unittest.TestCase):
    def setUp(self):
        self.pub = _password_publisher()

    def _history(self):
        return json.dumps(list(self.pub.history), default=str)

    def _call(self, payload, topic="text/set_value"):
        with mock.patch.object(mp._LOGGER, "warning"):
            self.pub._on_call(topic, payload if isinstance(payload, str) else json.dumps(payload))

    def test_an_oversized_id(self):
        self._call({"entity_id": "text.pw", "value": SECRET, "_id": "x" * (mp.CALL_ID_MAX_BYTES + 10)})
        self.assertEqual(self.pub.history[-1]["state"], "rejected")
        self.assertNotIn(SECRET, self._history())
        self.assertIn("***", self.pub.history[-1]["data"])

    def test_a_domain_excluded(self):
        self.pub.config.exclude_integrations = ["text"]
        self._call({"entity_id": "text.pw", "value": SECRET})
        self.assertEqual(self.pub.history[-1]["state"], "rejected")
        self.assertNotIn(SECRET, self._history())

    def test_a_payload_that_does_not_parse(self):
        self._call('{"entity_id": "text.pw", "value": "' + SECRET + '", "x": NaN}')
        self.assertEqual(self.pub.history[-1]["state"], "rejected")
        self.assertNotIn(SECRET, self._history())

    def test_a_number(self):
        self._call({"entity_id": "text.pw", "value": 480913, "_id": "x" * (mp.CALL_ID_MAX_BYTES + 10)})
        self.assertNotIn("480913", self._history())

    def test_a_number_is_a_secret_to_mask_in_the_service_error(self):
        self.assertEqual(mp.password_value(self.pub.hass, "text", "set_value", {"entity_id": "text.pw", "value": 480913},
                                           lambda: []), "480913")

    def test_a_plain_text_entity_stays_readable(self):
        self._call({"entity_id": "text.plain", "value": "visible", "_id": "x" * (mp.CALL_ID_MAX_BYTES + 10)})
        self.assertIn("visible", self._history())


class MovedWhileDownTest(StartWindowCase):
    """S3-2: sensor.a sat under another device when the previous process announced it; now it is on the demo device.
    The old config kept owning its unique_id, so the consumer ignored it in the demo device's config."""

    OLD = "old"

    def _retained(self, did, *eids):
        comps = {}
        for eid in eids:
            comps[mp._comp_key(eid)] = {"platform": "sensor", "unique_id": f"{self.pub.prefix}{eid}", "state_topic": "x"}
        return json.dumps({"device": {"identifiers": [did]}, "origin": disc.origin(self.pub.prefix), "components": comps}).encode()

    async def _sweep(self, found):
        self.pub._orphan_sweep_due = False  # as _async_orphan_sweep_if_due does before sweeping
        self.pub.hass.async_add_executor_job = mock.AsyncMock(return_value=found)
        self.published[:] = []
        await self.pub._async_sweep_orphans()

    def _last(self, did):
        topic = self.pub._discovery_topic(did)
        return [p for t, p in self.published if t == topic]

    def _demo_retained(self):
        return {self.pub._discovery_topic(self.did): self.raw_config().encode()}

    async def test_the_old_device_is_gone(self):
        old = f"{self.pub.prefix}{self.OLD}"
        await self._sweep({self.pub._discovery_topic(old): self._retained(old, "sensor.a"), **self._demo_retained()})
        self.assertEqual(self._last(old), [None])
        self.assertNotIn(None, self._last(self.did))  # the live config is never cleared
        await self.run_debounced()  # the device it moved to is announced again once the old config dropped it
        self.assertIn("unique_id", self.config()["components"]["sensor_a"])

    async def test_the_old_device_still_has_other_entities(self):
        self.registry.entities["sensor.c"] = _entry("sensor.c")
        self.registry.entities["sensor.c"].platform = "other"
        self.states["sensor.c"] = State("sensor.c", "5", {"unit_of_measurement": "W"})
        self.pub.hass.config.components = {"demo", "other"}
        await self.pub.async_republish_all()
        other, _ = disc.device_block(self.pub.hass, None, "other", self.pub.prefix)
        await self._sweep({self.pub._discovery_topic(other): self._retained(other, "sensor.c", "sensor.a"), **self._demo_retained()})
        [config] = [json.loads(p) for p in self._last(other)]
        self.assertEqual(config["components"]["sensor_a"], {"platform": "sensor"})
        self.assertIn("unique_id", config["components"]["sensor_c"])
        self.assertNotIn(None, self._last(self.did))

    async def test_the_old_device_only_has_entities_still_setting_up(self):
        self.registry.entities["sensor.d"] = _entry("sensor.d")
        self.registry.entities["sensor.d"].platform = "slow"  # not loaded: not announced by this process, not gone
        old = f"{self.pub.prefix}{self.OLD}"
        await self._sweep({self.pub._discovery_topic(old): self._retained(old, "sensor.d", "sensor.a"), **self._demo_retained()})
        [config] = [json.loads(p) for p in self._last(old)]
        self.assertEqual(config["components"]["sensor_a"], {"platform": "sensor"})
        self.assertIn("unique_id", config["components"]["sensor_d"])

    async def test_an_entity_that_did_not_move_is_left_alone(self):
        await self._sweep(self._demo_retained())
        self.assertEqual(self.published, [])
        self.assertEqual(self.pub.hass.loop.later, [])


class VersionSwitchKeepsDisabledEntitiesTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_what_no_entity_has_any_more_is_cleared(self):
        pub = camp._publisher(enabled=True, host="broker", force_base_topic=True, discovery_enabled=True)
        pub._topics = {"sensor.live": f"{camp.BASE}/demo/sensor/live"}
        pub.config.exclude_integrations = []
        pub.rules.rules = {"sensor.hidden": {"exclude": True}}
        registry = SimpleNamespace(entities={eid: SimpleNamespace(entity_id=eid, platform="demo")
                                             for eid in ("sensor.live", "sensor.off", "sensor.hidden")})
        pub.hass.states.get = lambda eid: object() if eid == "sensor.live" else None
        doc = json.dumps({"published_at": "now", "integration": "demo"}).encode()
        retained = {f"{camp.BASE}/demo/sensor/{name}": doc for name in ("live", "off", "hidden", "gone")}
        pub._retained_scan = lambda *a, **k: retained
        cleared = []
        pub._clear_topics = lambda suffix, topics: cleared.extend(topics)
        pub.async_republish_all = mock.AsyncMock(return_value=1)
        pub.hass.async_add_executor_job = mock.AsyncMock(side_effect=lambda f, *a: f(*a))
        with mock.patch.object(er, "async_get", return_value=registry):
            self.assertEqual(await pub.async_clear_stale_docs(), 2)
        self.assertEqual(sorted(cleared), [f"{camp.BASE}/demo/sensor/gone", f"{camp.BASE}/demo/sensor/hidden"])


class SaveReadsTheFileOffTheLoopTest(unittest.TestCase):
    def test_mqtt_json_is_not_opened_on_the_loop(self):
        pub = object.__new__(mp.MqttPublisher)
        pub.path = os.path.join(tempfile.mkdtemp(), "mqtt.json")
        with open(pub.path, "w", encoding="utf-8") as fh:
            json.dump({"host": "broker.lan", "port": 1884}, fh)
        pub.config = mp.MqttConfig()
        pub._saved = pub._disk_read = None
        opened_on = []
        real_open = builtins.open

        def spy(path, *a, **k):
            if path == pub.path:
                opened_on.append(threading.current_thread())
            return real_open(path, *a, **k)

        async def main():
            loop = asyncio.get_running_loop()
            pub.hass = mock.Mock()
            pub.hass.async_add_executor_job = lambda f, *a: loop.run_in_executor(None, f, *a)
            with mock.patch.object(mp.writer, "async_write", mock.AsyncMock()), mock.patch("builtins.open", spy):
                first, second = await asyncio.gather(pub.async_save({"qos": 1}), pub.async_save({"host": "other"}))
            return threading.current_thread(), first, second

        loop_thread, first, second = asyncio.run(main())
        self.assertTrue(opened_on)
        self.assertNotIn(loop_thread, opened_on)
        self.assertEqual((first.host, first.port, first.qos), ("broker.lan", 1884, 1))  # the file is still the base
        self.assertEqual((second.host, second.port, second.qos), ("other", 1884, 1))  # and each save sees the one before


class MaskedTextKeepsItsQuotesTest(unittest.TestCase):
    def test_a_data_field_that_quotes_credentials(self):
        pub = camp._publisher()
        message = "see https://x.invalid/a?token=XTOK then Authorization: Bearer YTOKEN1234 password=ZPW"
        rec = pub._remember("call", "hri_probe.fail", {"_id": "mf1", "message": message}, "mf1")
        shown = json.loads(rec["data"])  # still JSON: no quote was added inside the string
        self.assertEqual(shown["message"], "see https://x.invalid/a?token=*** then Authorization: *** password=***")

    def test_a_field_that_needs_no_name_stays_readable(self):
        """The shapes that need no name (a lone Bearer, a URL password) are for a service's error message only."""
        pub = camp._publisher()
        rec = pub._remember("call", "demo.x", {"message": "just Bearer abcdefghijkl here"}, None)
        self.assertEqual(json.loads(rec["data"]), {"message": "just Bearer abcdefghijkl here"})

    def test_quotes_are_kept_as_they_were(self):
        for text, masked in (('{"code": 1234}', '{"code": "***"}'),
                             ("{'token': 'abc'}", "{'token': '***'}"),
                             ("{'token': abc}", "{'token': '***'}"),
                             ('{\\"code\\": 1234}', '{\\"code\\": \\"***\\"}'),
                             ('{\\"code\\": \\"1234\\"}', '{\\"code\\": \\"***\\"}'),
                             ("pin=1234 and more", "pin=*** and more")):
            with self.subTest(text=text):
                self.assertEqual(mp._mask_text(text), masked)


if __name__ == "__main__":
    unittest.main()
