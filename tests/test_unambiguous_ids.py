"""Ids read back into "ours + rest": discovery.own_rest for a prefix ending in "-", and the manager device's identifier
mapped back to the discovery id it is announced under."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import parity
from tests import test_camp_publish as camp
from tests.test_r9_mqtt import _call

A = "hass_a"
TOPICS = dict.fromkeys(("status", "health", "manager", "cmd"), "t")


def _hass(entity_ids=("sensor.y",)):
    hass = mock.Mock()
    hass.states.async_entity_ids.return_value = list(entity_ids)
    hass.states.get.side_effect = lambda eid: SimpleNamespace(state="on") if eid in entity_ids else None
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


class _Store:
    """One broker's retained store, reached by the live clients of every container and by the throwaway clients the
    managers build (scans, clears)."""

    def __init__(self):
        self.retained, self.cleared = {}, []

    def retain(self, topic, payload):
        if payload in ("", None):
            self.retained.pop(topic, None)
            self.cleared.append(topic)
        else:
            self.retained[topic] = payload.encode() if isinstance(payload, str) else payload

    def live(self):
        store = self

        class Live:
            def publish(self, topic, payload=None, qos=0, retain=False):
                if retain:
                    store.retain(topic, payload)
                return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)

        return Live()

    def throwaway(self, _suffix, _what, _deadline, on_message=None):
        store = self

        class Client:
            on_subscribe = None

            def subscribe(self, topics):
                filters = [t for t, _qos in topics]
                self.on_subscribe(self, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=1) for _ in topics], None)
                for topic, payload in list(store.retained.items()):
                    if any(mqtt.topic_matches_sub(f, topic) for f in filters):
                        on_message(self, None, SimpleNamespace(topic=topic, payload=payload, retain=True))
                return mqtt.MQTT_ERR_SUCCESS, 1

            def publish(self, topic, payload=None, qos=0, retain=False):
                store.retain(topic, payload)
                return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS, is_published=lambda: True)

            is_connected = staticmethod(lambda: True)
            disconnect = loop_stop = staticmethod(lambda: None)
            socket = staticmethod(lambda: None)

        return Client()


async def _now(f, *args):
    return f(*args)


class _Container:
    """A container on the shared broker, announcing ``entity_id`` on the device-less device of the integration demo
    (discovery id <prefix>demo_nodevice) and its manager device, both retained."""

    def __init__(self, store, base, prefix, entity_id):
        self.base, self.prefix, self.entity_id = base, prefix, entity_id
        pub = camp._publisher(discovery_enabled=True, manager_discovery=True)
        pub._live_base, pub._live_prefix, pub._key_provider = base, prefix, (lambda: base)
        pub._client = store.live()
        pub.hass.async_add_executor_job = _now
        self.did = f"{prefix}demo_nodevice"
        block = {"identifiers": [self.did], "name": "demo (no device)"}
        comp = {"platform": "sensor", "unique_id": prefix + entity_id, "default_entity_id": entity_id, "name": entity_id}
        pub._group_by_device = lambda: ({self.did: (block, {entity_id: comp})}, {})
        pub._manager_discovery = lambda: disc.manager_device(base, prefix, TOPICS, "demo", "1", True)
        for did, (blk, comps) in pub._announced_groups()[0].items():
            store.retain(pub._discovery_topic(did), json.dumps({"device": blk, "origin": disc.origin(prefix),
                                                                "components": {mp._comp_key(e): c for e, c in comps.items()}}))
        self.pub = pub

    def parent_rows(self):
        """How the main Home Assistant registered what this container announced."""
        dev = f"dev_{self.base}"
        return ([{"entity_id": self.entity_id, "unique_id": self.prefix + self.entity_id, "platform": "mqtt", "device_id": dev}],
                [{"id": dev, "identifiers": [["mqtt", self.did]], "name": "demo (no device)"}])


class DestructiveDiscoveryGateTest(unittest.TestCase):
    """Containers hass_a and hass_a_binary on one broker and one main Home Assistant: "Remove orphans" on either never
    empties the other's discovery config.  With both on hass_<domain>_ ids, hass_a's parity takes hass_a_binary's
    sensor.x (hass_a_ + binary_sensor.x) and its device (hass_a_ + binary_demo_nodevice) for its own: the empty
    config it sent deleted those entities on the main Home Assistant."""

    PAIRS = {"legacy + legacy": ("hass_a_", "hass_a_binary_"), "legacy + unambiguous": ("hass_a_", "hass_a_binary-"),
             "unambiguous + legacy": ("hass_a-", "hass_a_binary_")}

    def setUp(self):
        patcher = mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda _self, *a: self.store.throwaway(*a))
        patcher.start()
        self.addCleanup(patcher.stop)
        mock.patch.object(mp.MqttPublisher, "_collect_quiet", staticmethod(lambda *a, **k: None)).start()
        self.addCleanup(mock.patch.stopall)

    def containers(self, prefix_a, prefix_b):
        self.store = _Store()
        a, b = _Container(self.store, A, prefix_a, "sensor.y"), _Container(self.store, "hass_a_binary", prefix_b, "sensor.x")
        ents_a, devs_a = a.parent_rows()
        ents_b, devs_b = b.parent_rows()
        client = mock.Mock(url="http://parent")
        client.commands = mock.AsyncMock(return_value=[ents_a + ents_b, devs_a + devs_b, [], {"components": ["mqtt"], "version": "2026.9.3"}])
        return a, b, client

    def remove_orphans(self, container, client, entity_ids):
        view = parity.ParityActionView(_hass([container.entity_id]), mock.Mock(), container.pub)
        with mock.patch.object(parity, "_parent_client", return_value=client):
            return _call(view, "remove_orphans", {"entity_ids": entity_ids})[0]

    def test_neither_container_empties_the_others_config(self):
        for name, (prefix_a, prefix_b) in self.PAIRS.items():
            with self.subTest(name):
                a, b, client = self.containers(prefix_a, prefix_b)
                before = dict(self.store.retained)
                res_a = self.remove_orphans(a, client, ["sensor.x"])
                res_b = self.remove_orphans(b, client, ["sensor.y"])
                self.assertEqual((res_a.get("removed", []), res_b.get("removed", [])), ([], []))
                self.assertEqual((self.store.retained, self.store.cleared), (before, []))
                # and the publisher refuses the other's device whatever asks it to
                for one, other in ((a, b), (b, a)):
                    retained = asyncio.run(one.pub.async_retained_ours([other.did]))
                    self.assertEqual(retained, {other.did: False})
                    self.assertFalse(one.pub.remove_discovered_component(other.did, other.entity_id, "sensor", retained_ours=set()))
                self.assertEqual(self.store.retained, before)

    def test_what_the_regex_cannot_tell_apart_the_origin_does(self):
        """Both on hass_<domain>_ ids: parity still takes sensor.x for an orphan of hass_a, on hass_a_binary's device;
        the removal is refused and reported."""
        a, b, client = self.containers(*self.PAIRS["legacy + legacy"])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            res = asyncio.run(parity.compute_parity(_hass(["sensor.y"]), mock.Mock(), a.pub))
        self.assertEqual([(o["parent_entity_id"], o["discovery_id"]) for o in res["orphans"]], [("sensor.x", b.did)])
        res = self.remove_orphans(a, client, ["sensor.x"])
        self.assertEqual((res["removed"], res["skipped"], res["not_ours"]), ([], ["sensor.x"], ["sensor.x"]))
        self.assertIn("another container", res["note"])
        self.assertIn(a.pub._discovery_topic(b.did), self.store.retained)

    def test_a_device_of_ours_no_longer_announced_is_still_cleared(self):
        for name, (prefix_a, prefix_b) in self.PAIRS.items():
            with self.subTest(name):
                a, _b, client = self.containers(prefix_a, prefix_b)
                a.pub._group_by_device = lambda: ({}, {})  # its only entity went, and with it the device
                view = parity.ParityActionView(_hass([]), mock.Mock(), a.pub)
                with mock.patch.object(parity, "_parent_client", return_value=client):
                    res = _call(view, "remove_orphans", {"entity_ids": ["sensor.y"]})[0]
                self.assertEqual(res["removed"], ["sensor.y"])
                self.assertEqual(self.store.cleared, [a.pub._discovery_topic(a.did)])

    def test_unreadable_is_not_ours(self):
        a, b, _client = self.containers(*self.PAIRS["legacy + legacy"])
        a.pub._group_by_device = lambda: ({}, {})
        with mock.patch.object(mp.MqttPublisher, "_retained_scan", side_effect=RuntimeError("the broker went away")):
            self.assertEqual(asyncio.run(a.pub.async_retained_ours([a.did, None])), {a.did: False})
        self.assertFalse(a.pub.remove_discovered_component(a.did, "sensor.y", "sensor"))  # nothing verified: refused
        self.assertEqual(self.store.cleared, [])


if __name__ == "__main__":
    unittest.main()
