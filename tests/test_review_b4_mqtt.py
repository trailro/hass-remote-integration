"""Review of b4cd1a1, MQTT side.  S3-1: the value of a text.set_value call for a text entity in password mode was kept
in clear in the command history when the call was refused before it was masked (an oversized _id, a denied domain,
a payload that does not parse), and a number was never masked at all.  S3-2: an entity moved to another device while
the container was down stayed in its old device's retained config."""

import json
import unittest
from unittest import mock

from homeassistant.core import State

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
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


if __name__ == "__main__":
    unittest.main()
