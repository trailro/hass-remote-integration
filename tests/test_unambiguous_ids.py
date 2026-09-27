"""Ids read back into "ours + rest": discovery.own_rest for a prefix ending in "-", and the manager device's identifier
mapped back to the discovery id it is announced under."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import parity
from tests import test_camp_publish as camp
from tests.test_r9_mqtt import _call

A = "hass_a"
TOPICS = dict.fromkeys(("status", "health", "manager", "cmd"), "t")


def _hass():
    hass = mock.Mock()
    hass.states.async_entity_ids.return_value = ["sensor.y"]
    hass.states.get.return_value = SimpleNamespace(state="on")
    return hass


class OwnRestTest(unittest.TestCase):
    def test_the_rest_of_an_own_id(self):
        prefix = "hass_a-"
        for rest in ("sensor.x", "binary_sensor.x", "demo_nodevice", "manager", "health_online"):
            self.assertEqual(disc.own_rest(prefix, prefix + rest), rest)
        for value in (None, 3, "", prefix, "hass_b-sensor.x", "hass_a_sensor.x", prefix + "garage-sensor.x", prefix + "-"):
            self.assertIsNone(disc.own_rest(prefix, value), value)


class ManagerOrphanTest(unittest.TestCase):
    """The manager device is announced under <base>_manager, not under its identifier <prefix>manager, which is the
    same string only for a prefix ending in "_": parity maps the identifier back."""

    CASES = ((A, "hass_a_"), ("hass_a-garage", "hass_a-garage-"))

    def test_a_manager_orphan_is_removed_from_the_manager_device(self):
        for key, prefix in self.CASES:
            with self.subTest(key=key):
                pub = camp._publisher(discovery_enabled=True, manager_discovery=True)
                pub._live_base, pub._live_prefix, pub._key_provider = key, prefix, (lambda k=key: k)
                pub._group_by_device = lambda: ({}, {})
                pub._manager_discovery = lambda k=key, p=prefix: disc.manager_device(k, p, TOPICS, "demo", "1", False)  # no buttons now
                entities = [{"entity_id": f"button.{key}_restart", "unique_id": f"{prefix}manager_restart", "platform": "mqtt", "device_id": "m"}]
                devices = [{"id": "m", "identifiers": [["mqtt", f"{prefix}manager"]], "name": "manager"}]
                client = mock.Mock(url="http://parent")
                client.commands = mock.AsyncMock(return_value=[entities, devices, [], {"components": ["mqtt"], "version": "2026.9.3"}])
                with mock.patch.object(parity, "_parent_client", return_value=client):
                    res = asyncio.run(parity.compute_parity(_hass(), mock.Mock(), pub))
                    (orphan,) = res["orphans"]
                    self.assertEqual((orphan["discovery_id"], orphan["our_entity_id"]), (f"{key}_manager", "manager_restart"))
                    answer, _ = _call(parity.ParityActionView(_hass(), mock.Mock(), pub), "remove_orphans",
                                      {"entity_ids": [f"button.{key}_restart"]})
                self.assertEqual(answer["removed"], [f"button.{key}_restart"])
                ((topic, payload, _qos, _retain),) = pub._client.published
                self.assertEqual(topic, f"homeassistant/device/{key}_manager/config")
                self.assertEqual(json.loads(payload)["components"][f"button_{key}_restart"], {"platform": "button"})

    def test_an_unambiguous_plain_prefix_does_not_claim_its_instances(self):
        """hass_a- starts every id of the instance hass_a-garage: a "-" in the rest is an instance's."""
        pub = camp._publisher(discovery_enabled=True)
        pub._live_base, pub._live_prefix, pub._key_provider = A, "hass_a-", (lambda: A)
        pub._group_by_device = lambda: ({}, {})
        pub._manager_discovery = lambda: disc.manager_device(A, "hass_a-", TOPICS, "demo", "1", False)
        entities = [{"entity_id": "sensor.x", "unique_id": "hass_a-garage-sensor.x", "platform": "mqtt", "device_id": "d"},
                    {"entity_id": "binary_sensor.a_garage_integration", "unique_id": "hass_a-garage-health_online", "platform": "mqtt",
                     "device_id": "m"}]
        devices = [{"id": "d", "identifiers": [["mqtt", "hass_a-garage-demo_nodevice"]], "name": "demo"},
                   {"id": "m", "identifiers": [["mqtt", "hass_a-garage-manager"]], "name": "manager"}]
        client = mock.Mock(url="http://parent")
        client.commands = mock.AsyncMock(return_value=[entities, devices, [], {"components": ["mqtt"], "version": "2026.9.3"}])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            res = asyncio.run(parity.compute_parity(_hass(), mock.Mock(), pub))
        self.assertEqual((res["parent"], res["orphans"]), (0, []))

if __name__ == "__main__":
    unittest.main()
