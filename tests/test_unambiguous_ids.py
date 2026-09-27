"""Unique ids, device identifiers and discovery ids: the id format of each volume, ids read back into "ours + rest"
(discovery.own_rest for a prefix ending in "-", the manager device's identifier mapped back to its discovery id), and
no container clearing another's discovery config.

The plain identity hass_<domain> put a "_" between itself and the rest (hass_a_ + sensor.x), and a "_" is also inside
domains: hass_a_ + binary_sensor.x is hass_a_binary_ + sensor.x.  A volume that never published its plain identity
uses hass_<domain>- (id_format 2); one that did keeps hass_<domain>_ for good, and nothing it announced changes.
"""

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import parity, views
from custom_components.integration_manager.installer import MqttIdentity
from tests import test_camp_publish as camp
from tests.test_mqtt_instance_identity import BROKER, LEGACY, _env, _names
from tests.test_r9_mqtt import _call
from tests.test_r13_mqtt import _Case

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
                    self.assertEqual(retained, {other.did: "not_ours"})
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
            self.assertEqual(asyncio.run(a.pub.async_retained_ours([a.did, None])), {a.did: "unreadable"})
        self.assertFalse(a.pub.remove_discovered_component(a.did, "sensor.y", "sensor"))  # nothing verified: refused
        self.assertEqual(self.store.cleared, [])

    def test_each_reason_an_orphan_is_left_is_named(self):
        """Another origin, no config on the broker, or none readable: each is refused, and said as it is."""
        a, b, client = self.containers(*self.PAIRS["legacy + legacy"])
        del self.store.retained[a.pub._discovery_topic(b.did)]  # b's device config went from the broker
        res = self.remove_orphans(a, client, ["sensor.x"])
        self.assertEqual((res["removed"], res["skipped"], res["not_on_broker"]), ([], ["sensor.x"], ["sensor.x"]))
        self.assertNotIn("not_ours", res)
        self.assertIn("no retained config on the broker", res["note"])
        self.assertNotIn("another container", res["note"])
        with mock.patch.object(mp.MqttPublisher, "_collect_quiet", staticmethod(lambda *a, **k: True)):  # cut short
            res = self.remove_orphans(a, client, ["sensor.x"])
        self.assertEqual((res["removed"], res["unreadable"]), ([], ["sensor.x"]))
        self.assertIn("could not be read", res["note"])
        self.assertEqual(self.store.cleared, [])



def _config(base, prefix, entity_id="sensor.power", root="homeassistant"):
    """What a container retains for a device-less device of the integration demo holding ``entity_id``."""
    did = f"{prefix}demo_nodevice"
    comp = {"platform": "sensor", "unique_id": prefix + entity_id, "default_entity_id": entity_id}
    return (f"{root}/device/{did}/config",
            json.dumps({"device": {"identifiers": [did]}, "origin": disc.origin(prefix), "components": {"sensor_power": comp}}).encode())


class IdFormatTest(_Case):
    """Which ids a volume announces under, decided once per identity and recorded in mqtt_identity.json."""

    def setUp(self):
        super().setUp()
        self.store = _Store()
        mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda _self, *a: self.store.throwaway(*a)).start()
        self.scans = []
        real = mp.MqttPublisher._retained_scan
        mock.patch.object(mp.MqttPublisher, "_retained_scan",
                          lambda pub, suffix, topics, *a, **k: self.scans.append(suffix) or real(pub, suffix, topics, *a, **k)).start()
        self.path = os.path.join(self.dir, "integration_manager", "mqtt_identity.json")

    def identity(self, env=None, domain="demo"):
        with _env(env):
            ident = MqttIdentity(self.path, lambda: domain)
        ident.load()
        return ident

    def pub_with(self, ident, domain="demo", **config):
        pub = self.publisher(**config)
        pub._identity, pub._key_provider = ident, (lambda: ident.key(domain))
        return pub

    def record(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    async def connect_names(self, pub):
        """What _connect does with the identity before the client: decide, sweep and record, the live names."""
        base = pub.wanted_base_topic
        pub._set_ids_undecided(base, await pub.hass.async_add_executor_job(pub._decide_id_format, base))
        self.assertTrue(await pub.hass.async_add_executor_job(pub._sweep_old_identity, base))
        pub._live_base, pub._live_prefix = base, pub._prefix_for(base)

    async def test_a_fresh_volume_takes_unambiguous_ids(self):
        # another container's hass_demo_binary_ configs start with hass_demo_ too: not ours, they decide nothing
        self.store.retained.update([_config("hass_demo_binary", "hass_demo_binary_"), _config("hass_demo-garage", "hass_demo-garage-")])
        pub = self.pub_with(self.identity())
        await self.connect_names(pub)
        self.assertEqual((pub.prefix, pub._ids_undecided, self.scans), ("hass_demo-", "", ["ids"]))
        self.assertEqual((self.record()["base"], self.record()["id_format"]), ("hass_demo", disc.ID_FORMAT))
        # recorded: the next start reads it, and scans nothing
        self.scans.clear()
        pub = self.pub_with(self.identity())
        await self.connect_names(pub)
        self.assertEqual((pub.prefix, self.scans), ("hass_demo-", []))

    async def test_a_volume_that_published_keeps_its_ids_byte_identical(self):
        """0.25.x's record (no domain) and 0.26.0's (domain, pinned): hass_<domain>_ ids, one scan (for hass_demo- ids
        a rollback would have left) and none once recorded, and every name 0.26.0 announced is the same string."""
        broker = {**BROKER, "port": self.port}
        records = {"0.25.x": {**LEGACY, "base": "hass_demo", "broker": broker},
                   "0.26.0": {"base": "hass_demo", "prefix": "homeassistant", "broker": broker, "domain": "demo", "pinned": True}}
        entity_ids = ["sensor.power", "binary_sensor.door", "switch.pump"]
        for version, record in records.items():
            with self.subTest(version):
                mp.write_json(self.path, record)
                self.scans.clear()
                pub = self.pub_with(self.identity())
                await self.connect_names(pub)
                self.assertEqual((pub.prefix, self.scans), ("hass_demo_", ["ids"]))
                self.assertEqual(self.record(), {**record, "domain": "demo", "pinned": True, "id_format": disc.LEGACY_ID_FORMAT})
                # 0.26.0's names: the identity prefix was the base and a "_"
                old_pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
                old_pub.config, old_pub._key_provider = pub.config, (lambda: "hass_demo")
                old_pub._live_base, old_pub._live_prefix = "hass_demo", "hass_demo_"
                names = _names(pub, entity_ids)
                self.assertEqual(names, _names(old_pub, entity_ids))
                self.assertIn("hass_demo_sensor.power", names["unique_ids"])
                self.assertIn("hass_demo_demo_nodevice", names["discovery_ids"])
                self.assertIn("hass_demo_manager", names["devices"])
                # and the next start reads the record it wrote the same way, without a scan
                self.scans.clear()
                pub = self.pub_with(self.identity())
                await self.connect_names(pub)
                self.assertEqual((pub.prefix, self.scans), ("hass_demo_", []))

    async def test_a_missing_record_follows_what_is_retained(self):
        """A volume whose record was deleted (it was damaged): its own retained configs say which ids it announced."""
        cases = {"hass_demo_": disc.LEGACY_ID_FORMAT, "hass_demo-": disc.ID_FORMAT}
        for prefix, expected in cases.items():
            with self.subTest(prefix):
                if os.path.exists(self.path):
                    os.remove(self.path)
                self.store.retained = dict([_config("hass_demo", prefix), _config("hass_demo_binary", "hass_demo_binary_")])
                pub = self.pub_with(self.identity())
                await self.connect_names(pub)
                self.assertEqual((pub.prefix, self.record()["id_format"]), (prefix, expected))

    async def test_the_manager_device_alone_decides_too(self):
        """Discovery off, manager_discovery on: the manager device's config is all that is retained."""
        mid, block, comps = disc.manager_device("hass_demo", "hass_demo_", TOPICS, "demo", "", False)
        self.store.retained[f"homeassistant/device/{mid}/config"] = json.dumps(
            {"device": block, "origin": disc.origin("hass_demo_"), "components": {mp._comp_key(e): c for e, c in comps.items()}}).encode()
        pub = self.pub_with(self.identity())
        await self.connect_names(pub)
        self.assertEqual(pub.prefix, "hass_demo_")

    async def test_an_incomplete_scan_announces_nothing_until_one_decides(self):
        self.store.retained.update([_config("hass_demo", "hass_demo_"), _config("hass_other", "hass_other_")])
        pub = self.pub_with(self.identity(), discovery_enabled=True)
        with mock.patch.object(mp, "RETAINED_SCAN_MAX_BYTES", 10):
            await self.connect_names(pub)
        self.assertIn("discovery waits", pub._ids_undecided)
        self.assertEqual(self.record()["id_format"], disc.ID_FORMAT_UNDECIDED)  # the names are recorded, the format is not
        self.assertIn("discovery waits", pub._identity.describe()["identity_warning"])  # status and preflight say why
        pub._client, pub._connected, pub._moving = self.store.live(), True, False
        pub._broker_max_packet, pub._oversized_warned = 0, set()
        before = dict(self.store.retained)
        self.assertFalse(pub._publish("homeassistant/device/hass_demo-demo_nodevice/config", "{}", qos=1))
        self.assertFalse(pub._publish("homeassistant/device/hass_demo_demo_nodevice/config", None, qos=1))
        self.assertTrue(pub._publish("hass_demo/status", "online", qos=1))  # the documents flow
        pub._group_by_device = mock.Mock(side_effect=AssertionError("nothing is grouped for discovery"))
        pub._publish_discovery_all()
        self.assertEqual({t: p for t, p in self.store.retained.items() if t.startswith("homeassistant/")},
                         {t: p for t, p in before.items() if t.startswith("homeassistant/")})
        # a restart reads the record: still undecided, so it scans again
        self.assertIsNone(self.identity().id_format("hass_demo"))
        # the next full republish reads them in full: decided, recorded, announced from then on
        pub._ids_tried_at -= mp.IDS_RETRY_MIN_S
        await pub._async_decide_id_format()
        self.assertEqual((pub._ids_undecided, pub.prefix, self.record()["id_format"]), ("", "hass_demo_", disc.LEGACY_ID_FORMAT))
        self.assertIsNone(pub._identity.describe()["identity_warning"])

    def throwaway(self, change):
        """The store's throwaway client, changed by ``change`` before the scan uses it."""
        def make(_self, *a):
            c = self.store.throwaway(*a)
            change(c)
            return c
        return mock.patch.object(mp.MqttPublisher, "_throwaway_client", make)

    @staticmethod
    def silent(code=0, drop=False):
        """A subscription the broker answers with ``code`` and then sends nothing on (``drop``: the connection drops
        after the SUBACK, and paho connects again: a client with no subscription)."""
        def change(c):
            def subscribe(topics):
                c.on_subscribe(c, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=code) for _ in topics], None)
                if drop:
                    c.on_disconnect(c, None, None, ReasonCode(PacketTypes.DISCONNECT, identifier=0x80), None)
                return mqtt.MQTT_ERR_SUCCESS, 1
            c.subscribe = subscribe
        return change

    async def test_a_scan_that_may_be_partial_decides_nothing(self):
        """A legacy volume without a record: its hass_demo_ configs are retained, but the read that would find them
        stops on its time limit while they still arrive, loses its connection (nothing arrives, which looks quiet),
        or is refused.  Deciding "-" there would announce every entity again as a duplicate: it stays undecided."""
        def lost(c):
            self.silent()(c)
            c.is_connected = lambda: False

        cases = {"its time limit": (mock.patch.object(mp.MqttPublisher, "_collect_quiet", staticmethod(lambda *a, **k: True)), "still arriving"),
                 "a lost connection": (self.throwaway(lost), "connection of the scan dropped"),
                 "a connection lost and back": (self.throwaway(self.silent(drop=True)), "connection of the scan dropped"),
                 "a refused subscription": (self.throwaway(self.silent(code=0x87)), "refused the subscription")}
        for name, (patch, reason) in cases.items():
            with self.subTest(name):
                if os.path.exists(self.path):
                    os.remove(self.path)
                self.store.retained = dict([_config("hass_demo", "hass_demo_")])
                pub = self.pub_with(self.identity())
                with patch:
                    await self.connect_names(pub)
                self.assertIn(reason, pub._ids_undecided)
                self.assertIsNone(pub._identity.id_format("hass_demo"))
                self.assertEqual((pub.prefix, self.record()["id_format"]), ("hass_demo-", disc.ID_FORMAT_UNDECIDED))
                # a full read decides as before
                pub._client, pub._connected, pub._moving = self.store.live(), True, False
                pub._ids_tried_at -= mp.IDS_RETRY_MIN_S
                await pub._async_decide_id_format()
                self.assertEqual((pub._ids_undecided, pub.prefix, self.record()["id_format"]), ("", "hass_demo_", disc.LEGACY_ID_FORMAT))

    async def test_only_the_strict_scan_refuses_a_partial_read(self):
        self.store.retained = dict([_config("hass_demo", "hass_demo_")])
        pub = self.pub_with(self.identity())
        with mock.patch.object(mp.MqttPublisher, "_collect_quiet", staticmethod(lambda *a, **k: True)):
            found = await pub.hass.async_add_executor_job(pub._retained_scan, "cleanup", [("homeassistant/device/+/config", 1)])
        self.assertEqual(list(found), list(self.store.retained))

    async def undecided(self, **config):
        """A volume without a record whose read stopped at its maximum, connected."""
        self.store.retained.update([_config("hass_demo", "hass_demo_"), _config("hass_other", "hass_other_")])
        pub = self.pub_with(self.identity(), **config)
        with mock.patch.object(mp, "RETAINED_SCAN_MAX_BYTES", 10):
            await self.connect_names(pub)
        self.assertTrue(pub._ids_undecided)
        pub._client, pub._connected, pub._moving = self.store.live(), True, False
        pub._broker_max_packet, pub._oversized_warned = 0, set()
        return pub

    async def test_the_connection_read_is_not_repeated_at_once(self):
        """The full republish that follows the connection does not read up to 64 MB again seconds later."""
        pub = await self.undecided()
        self.scans.clear()
        await pub._async_decide_id_format()
        self.assertEqual((self.scans, bool(pub._ids_undecided)), ([], True))
        pub._ids_tried_at -= mp.IDS_RETRY_MIN_S
        await pub._async_decide_id_format()
        self.assertEqual((self.scans, pub._ids_undecided), (["ids"], ""))

    async def test_an_undecided_format_keeps_the_orphan_sweep_due(self):
        """The timer's sweep while the format is undecided could clear nothing (no discovery config goes out): it stays
        due, with the configs of earlier processes still to be read, for the full republish that decides the format."""
        pub = await self.undecided(discovery_enabled=True)
        pub._orphan_sweep_due, pub._boot_components = True, None
        with mock.patch.object(mp.MqttPublisher, "_async_sweep_orphans") as sweep:
            await pub._async_orphan_sweep_if_due()
            sweep.assert_not_called()
            self.assertEqual((pub._orphan_sweep_due, pub._boot_components), (True, None))
            pub._ids_tried_at -= mp.IDS_RETRY_MIN_S
            await pub._async_decide_id_format()
            await pub._async_orphan_sweep_if_due()
            sweep.assert_awaited_once()

    async def connected(self, id_format, **config):
        """hass_demo connected, its record holding ``id_format`` (None: 0.26.0's, without one), announcing sensor.power
        on its device-less device."""
        record = {"base": "hass_demo", "prefix": "homeassistant", "broker": {**BROKER, "port": self.port}, "domain": "demo", "pinned": True}
        mp.write_json(self.path, record if id_format is None else {**record, "id_format": id_format})
        pub = self.pub_with(self.identity(), discovery_enabled=True, **config)
        await self.connect_names(pub)
        pub._client, pub._connected, pub._moving = self.store.live(), True, False
        pub._broker_max_packet, pub._oversized_warned = 0, set()
        did = f"{pub.prefix}demo_nodevice"
        comp = {"platform": "sensor", "unique_id": pub.prefix + "sensor.power", "default_entity_id": "sensor.power"}
        pub._group_by_device = lambda: ({did: ({"identifiers": [did]}, {"sensor.power": comp})}, {})
        pub._entity_gone = pub._excluded_now = lambda _eid: False
        return pub

    def both_formats(self, other):
        """What the broker holds after a rollback to 0.26.0 with discovery on and an update again (or a restore, or a
        hand edit): hass_demo's configs in both id formats, the manager device's config (one topic in every format)
        still in ``other``, and the configs of hass_demo_binary and hass_demo-garage, whose ids start like either."""
        mid, block, comps = disc.manager_device("hass_demo", other, TOPICS, "demo", "", False)
        self.store.retained.update([
            _config("hass_demo", "hass_demo_"), _config("hass_demo", "hass_demo-"),
            _config("hass_demo_binary", "hass_demo_binary_"), _config("hass_demo_binary", "hass_demo_binary-"),
            _config("hass_demo-garage", "hass_demo-garage-"),
            (f"homeassistant/device/{mid}/config", json.dumps({"device": block, "origin": disc.origin(other),
                                                               "components": {mp._comp_key(e): c for e, c in comps.items()}}).encode())])
        return _config("hass_demo", other)[0]

    async def test_the_orphan_sweep_clears_our_configs_in_the_other_id_format(self):
        """Both of this identity's entity sets stay on the main HA until the sweep clears the configs in the format not
        announced now; never the manager device's (it is announced again over them), never another container's."""
        for current, other in ((disc.LEGACY_ID_FORMAT, "hass_demo-"), (disc.ID_FORMAT, "hass_demo_")):
            with self.subTest(current=current):
                self.store.retained, self.store.cleared = {}, []
                pub = await self.connected(current)
                leftover = self.both_formats(other)
                before = dict(self.store.retained)
                await pub._async_sweep_orphans()
                self.assertEqual(self.store.cleared, [leftover])
                self.assertEqual(self.store.retained, {t: p for t, p in before.items() if t != leftover})

    @contextlib.contextmanager
    def logged(self, level):
        """assertLogs on the publisher's logger, which _Case silences."""
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs(mp._LOGGER, level) as logs:
                yield logs
        finally:
            logging.disable(logging.CRITICAL)

    async def test_a_rollback_to_0260_and_back_keeps_the_original_entities(self):
        """A volume that started with hass_demo- ids, rolled back to 0.26.0 with discovery on (which announced
        hass_demo_ ids, so the main HA made _2 twins of every entity, and rewrote the record without id_format), then
        updated again: its own hass_demo- configs prove it published them, so it keeps hass_demo- and the sweep clears
        the hass_demo_ configs, the twins; the originals stay."""
        self.both_formats("hass_demo_")
        pub = await self.connected(None)
        self.assertEqual((pub.prefix, self.scans), ("hass_demo-", ["ids"]))
        self.assertEqual((self.record()["id_format"], self.record()["id_format_source"]), (disc.ID_FORMAT, "scan"))
        self.assertEqual(pub._id_format_status("hass_demo")["id_format_source"], "scan")
        before = dict(self.store.retained)
        with self.logged("INFO") as logs:
            await pub._async_sweep_orphans()
        # counted as what they are, not as empty devices
        self.assertEqual([line for line in logs.output if "cleared" in line or "empty devices" in line],
                         ["INFO:custom_components.integration_manager.mqtt_publisher:MQTT: cleared 1 discovery configs "
                          "hass_demo announced in hass_demo_ ids, the id format it does not use"])
        twins = _config("hass_demo", "hass_demo_")[0]
        self.assertEqual(self.store.cleared, [twins])
        self.assertEqual(self.store.retained, {t: p for t, p in before.items() if t != twins})
        self.assertIn(_config("hass_demo", "hass_demo-")[0], self.store.retained)
        # recorded: the next start reads it, and scans nothing
        self.scans.clear()
        pub = self.pub_with(self.identity())
        await self.connect_names(pub)
        self.assertEqual((pub.prefix, self.scans), ("hass_demo-", []))

    async def test_a_rollback_to_0260_and_back_re_creates_the_manager_device(self):
        """0.26.0 announced the manager device under hass_demo_manager on the topic every format shares.  Announced over
        it with hass_demo-manager, the main HA would keep hass_demo_manager as an empty device: the connection's full
        republish empties that config first, pauses as a change on the MQTT page does, then announces it again."""
        self.both_formats("hass_demo_")
        manager = "homeassistant/device/hass_demo_manager/config"
        self.assertIn(b'"hass_demo_manager"', self.store.retained[manager])
        pub = await self.connected(None, manager_discovery=True)
        self.assertEqual(pub.prefix, "hass_demo-")
        pub._manager_discovery = lambda: disc.manager_device("hass_demo", pub.prefix, TOPICS, "demo", "1", True)
        groups = pub._group_by_device()[0]
        pub._group_by_device = lambda: (groups, {"mirrored": 0, "disabled": 0})
        pub.hass.states = mock.Mock(async_all=lambda: [])
        pub.hass.is_running, pub._orphan_sweep_due, pub._boot_components = False, True, None
        pub._identity_sweep_due, pub._resync_excluded, pub._undiscover_due = False, False, False
        pub.stats, pub._pending_clears, pub._manager_announced, pub._boot_removed = {"services_published": 0}, set(), frozenset(), set()
        pub.publish_health = pub.publish_manager = mock.Mock()
        pub._publish_services = mock.AsyncMock()
        timeline, retain = [], self.store.retain

        def logged_retain(topic, payload):
            if topic == manager:
                timeline.append(json.loads(payload)["device"]["identifiers"] if payload else "emptied")
            retain(topic, payload)

        async def pause(seconds):
            timeline.append(("pause", seconds))

        with mock.patch.object(self.store, "retain", logged_retain), mock.patch.object(mp.asyncio, "sleep", pause):
            await pub.async_republish_all()
        self.assertEqual(timeline[:3], ["emptied", ("pause", 2), ["hass_demo-manager"]])
        self.assertEqual(json.loads(self.store.retained[manager])["device"]["identifiers"], ["hass_demo-manager"])
        self.assertNotIn(_config("hass_demo", "hass_demo_")[0], self.store.retained)
        self.assertIn(_config("hass_demo", "hass_demo-")[0], self.store.retained)
        self.assertFalse(pub._ids_switch_due)
        # the next connection finds it in the format in use: nothing is emptied again
        timeline.clear()
        pub = await self.connected(disc.ID_FORMAT, manager_discovery=True)
        self.assertFalse(pub._ids_switch_due)

    async def test_a_record_without_id_format_and_no_own_dash_configs_stays_legacy(self):
        """0.26.0's record, only hass_demo_ configs of this origin (other containers' hass_demo- and hass_demo_binary-
        ones do not count): hass_demo_, recorded as before, byte for byte, and scanned once."""
        self.store.retained.update([_config("hass_demo", "hass_demo_"), _config("hass_demo-garage", "hass_demo-garage-"),
                                    _config("hass_demo_binary", "hass_demo_binary-")])
        pub = await self.connected(None)
        record = {"base": "hass_demo", "prefix": "homeassistant", "broker": {**BROKER, "port": self.port}, "domain": "demo", "pinned": True}
        self.assertEqual((pub.prefix, self.scans), ("hass_demo_", ["ids"]))
        self.assertEqual(self.record(), {**record, "id_format": disc.LEGACY_ID_FORMAT})
        self.assertEqual(pub._id_format_status("hass_demo")["id_format_source"], "recorded")
        await pub._async_sweep_orphans()
        self.assertEqual(self.store.cleared, [])
        self.scans.clear()
        pub = self.pub_with(self.identity())
        await self.connect_names(pub)
        self.assertEqual((pub.prefix, self.scans), ("hass_demo_", []))

    async def test_a_record_without_id_format_stays_legacy_when_the_scan_fails(self):
        """The read that would find hass_demo- configs is incomplete: never undecided for an install that works, it
        keeps hass_demo_ as before, and the log says so."""
        self.both_formats("hass_demo_")
        with mock.patch.object(mp, "RETAINED_SCAN_MAX_BYTES", 10), self.logged("WARNING") as logs:
            pub = await self.connected(None)
        self.assertEqual((pub.prefix, pub._ids_undecided, self.record()["id_format"]), ("hass_demo_", "", disc.LEGACY_ID_FORMAT))
        self.assertNotIn("id_format_source", self.record())
        self.assertTrue(any("hass_demo-" in line and "could not" in line for line in logs.output), logs.output)

    async def test_an_uninstall_ends_the_undecided_state(self):
        """No integration runs any more: there is no identity whose id format waits, and nothing says one does."""
        pub = await self.undecided()
        pub._identity._domain = pub._key_provider = lambda: None  # uninstalled
        pub.stats, pub._stopping, pub._cleanup_pending = {}, False, {}
        await pub.hass.async_add_executor_job(pub._connect)
        self.assertIn("no integration is running", pub.stats["connect_error"])
        self.assertEqual((pub._ids_undecided, pub._identity.undecided), ("", None))

    @staticmethod
    def status(pub):
        pub.hass.states = mock.Mock(async_all=lambda: [], get=lambda _eid: None)
        pub.stats, pub.history, pub._health_last = getattr(pub, "stats", {}), [], {"state": "ok"}
        pub._connected, pub._moving = getattr(pub, "_connected", False), getattr(pub, "_moving", False)
        pub._cleanup_pending, pub._cleanup_pending_lock = getattr(pub, "_cleanup_pending", {}), threading.Lock()
        with mock.patch.object(mp.er, "async_get", return_value=mock.Mock(entities={})), \
                mock.patch.object(pub, "recent_commands", return_value=[]):
            return pub.status()

    async def test_the_status_names_no_prefix_while_undecided(self):
        """hass_demo- is only what nothing is announced under yet: the status does not name it."""
        pub = await self.undecided()
        self.assertIsNone(self.status(pub)["prefix"])
        pub._ids_tried_at -= mp.IDS_RETRY_MIN_S
        await pub._async_decide_id_format()
        self.assertEqual(self.status(pub)["prefix"], "hass_demo_")

    async def test_the_mqtt_page_sets_an_undecided_id_format(self):
        for fmt, prefix in ((disc.LEGACY_ID_FORMAT, "hass_demo_"), (disc.ID_FORMAT, "hass_demo-")):
            with self.subTest(fmt=fmt):
                if os.path.exists(self.path):
                    os.remove(self.path)
                pub = await self.undecided()
                self.assertTrue(pub._id_format_choosable(pub.wanted_base_topic))
                pub.async_republish_all = mock.AsyncMock(return_value=0)
                res = await pub.async_set_id_format(fmt)
                self.assertEqual(res, {"ok": True, "identity": "hass_demo", "id_format": fmt, "prefix": prefix, "recorded": True,
                                       "changed": False})
                self.assertEqual((pub._ids_undecided, pub.prefix, self.record()["id_format"]), ("", prefix, fmt))
                self.assertIsNone(pub._identity.describe()["identity_warning"])
                pub.async_republish_all.assert_awaited_once()  # announced under it at once
                # recorded: what it announced from then on changes only as a confirmed change
                self.assertFalse(pub._id_format_choosable("hass_demo"))
                res = await pub.async_set_id_format(3 - fmt)
                self.assertFalse(res["ok"])
                self.assertIn(f"hass_demo uses id format {fmt} (chosen on the MQTT page)", res["error"])
                self.assertEqual(self.record()["id_format"], fmt)

    async def test_an_automatic_decision_can_be_changed_with_confirm(self):
        """A legacy volume that lost its record, on a broker whose ACL hides the configs: the scan finds nothing and
        decides hass_demo-, and records it.  The MQTT page still changes it, as a confirmed action: recorded, the
        configs this identity announced in the format it leaves are cleared first (only those), then it republishes."""
        pub = self.pub_with(self.identity(), discovery_enabled=True)
        await self.connect_names(pub)
        self.assertEqual((self.record()["id_format"], self.record()["id_format_source"]), (disc.ID_FORMAT, "scan_empty"))
        pub._client, pub._connected, pub._moving = self.store.live(), True, False
        pub._broker_max_packet, pub._oversized_warned = 0, set()
        wrong = self.both_formats("hass_demo-")
        self.store.retained.pop(_config("hass_demo", "hass_demo_")[0])  # what it announced since: hass_demo- only
        status = self.status(pub)
        self.assertEqual({k: status[k] for k in ("id_format", "id_format_source", "id_format_choosable", "id_format_changeable", "prefix")},
                         {"id_format": disc.ID_FORMAT, "id_format_source": "scan_empty", "id_format_choosable": False,
                          "id_format_changeable": True, "prefix": "hass_demo-"})
        pub.async_republish_all = mock.AsyncMock(return_value=0)
        before = dict(self.store.retained)
        res = await pub.async_set_id_format(disc.LEGACY_ID_FORMAT)  # not confirmed: nothing changes
        self.assertFalse(res["ok"])
        self.assertIn("hass_demo uses id format 2 (decided by a broker scan that found none of its configs)", res["error"])
        self.assertIn("confirm", res["error"])
        self.assertEqual((self.record()["id_format"], self.store.retained, pub.prefix), (disc.ID_FORMAT, before, "hass_demo-"))
        pub.async_republish_all.assert_not_called()
        res = await pub.async_set_id_format(disc.LEGACY_ID_FORMAT, confirm=True)
        self.assertEqual(res, {"ok": True, "identity": "hass_demo", "id_format": disc.LEGACY_ID_FORMAT, "prefix": "hass_demo_",
                               "recorded": True, "changed": True})
        self.assertEqual((self.record()["id_format"], self.record()["id_format_source"]), (disc.LEGACY_ID_FORMAT, "chosen"))
        manager = "homeassistant/device/hass_demo_manager/config"
        self.assertEqual(sorted(self.store.cleared), sorted([wrong, manager]))  # announced again at once under hass_demo_
        pub.async_republish_all.assert_awaited_once()
        self.assertFalse(pub._ids_switch_due)
        status = self.status(pub)
        self.assertEqual((status["id_format"], status["id_format_source"], status["prefix"]), (disc.LEGACY_ID_FORMAT, "chosen", "hass_demo_"))
        # the same format again: nothing to confirm, nothing cleared
        self.store.cleared = []
        res = await pub.async_set_id_format(disc.LEGACY_ID_FORMAT)
        self.assertEqual((res["ok"], res["changed"], self.store.cleared), (True, False, []))

    async def test_a_change_while_disconnected_clears_at_the_connection(self):
        pub = await self.connected(disc.LEGACY_ID_FORMAT)
        pub._connected = False
        self.both_formats("hass_demo_")
        res = await pub.async_set_id_format(disc.ID_FORMAT, confirm=True)
        self.assertEqual((res["ok"], res["recorded"], self.record()["id_format"], self.store.cleared), (True, True, disc.ID_FORMAT, []))
        self.assertTrue(pub._ids_switch_due)
        pub._connected = True
        await pub._async_clear_other_id_format()
        self.assertEqual(sorted(self.store.cleared), ["homeassistant/device/hass_demo_demo_nodevice/config",
                                                      "homeassistant/device/hass_demo_manager/config"])
        self.assertFalse(pub._ids_switch_due)

    async def test_the_status_says_where_the_id_format_comes_from(self):
        def fields(pub):
            status = self.status(pub)
            return tuple(status[k] for k in ("id_format", "id_format_source", "id_format_choosable", "id_format_changeable"))

        # before the first connection nothing is offered: no one-click choice on a healthy volume
        pub = self.pub_with(self.identity())
        self.assertEqual(fields(pub), (None, None, False, False))
        await self.connect_names(pub)
        self.assertEqual(fields(pub), (disc.ID_FORMAT, "scan_empty", False, True))
        os.remove(self.path)
        self.store.retained.update([_config("hass_demo", "hass_demo_")])
        pub = self.pub_with(self.identity())
        await self.connect_names(pub)
        self.assertEqual(fields(pub), (disc.LEGACY_ID_FORMAT, "scan", False, True))
        mp.write_json(self.path, {"base": "hass_demo", "prefix": "homeassistant", "broker": {**BROKER, "port": self.port},
                                  "domain": "demo", "pinned": True})  # 0.26.0's
        self.assertEqual(fields(self.pub_with(self.identity())), (disc.LEGACY_ID_FORMAT, "recorded", False, True))
        os.remove(self.path)
        pub = await self.undecided()
        self.assertEqual(fields(pub), (None, None, True, False))
        pub = self.pub_with(self.identity("garage"))
        await self.connect_names(pub)
        self.assertEqual(fields(pub), (disc.ID_FORMAT, "instance", False, False))

    async def test_the_choice_reads_the_record_again(self):
        pub = await self.undecided()
        mp.write_json(self.path, {**self.record(), "id_format": disc.LEGACY_ID_FORMAT})  # corrected by hand while running
        pub.async_republish_all = mock.AsyncMock()
        res = await pub.async_set_id_format(disc.ID_FORMAT)
        self.assertFalse(res["ok"])
        self.assertIn("uses id format 1 (recorded)", res["error"])
        self.assertEqual(self.record()["id_format"], disc.LEGACY_ID_FORMAT)
        pub.async_republish_all.assert_not_called()

    async def test_a_choice_while_disconnected_applies_at_the_connection(self):
        pub = self.pub_with(self.identity())
        res = await pub.async_set_id_format(disc.LEGACY_ID_FORMAT)
        self.assertEqual((res["ok"], res["recorded"], res["prefix"]), (True, False, "hass_demo_"))
        self.assertFalse(os.path.exists(self.path))
        await self.connect_names(pub)
        self.assertEqual((self.scans, pub.prefix, self.record()["id_format"]), ([], "hass_demo_", disc.LEGACY_ID_FORMAT))

    async def test_an_instance_has_no_id_format_to_choose(self):
        pub = self.pub_with(self.identity("garage"))
        await self.connect_names(pub)
        self.assertFalse(pub._id_format_choosable(pub.wanted_base_topic))
        res = await pub.async_set_id_format(disc.LEGACY_ID_FORMAT)
        self.assertFalse(res["ok"])
        self.assertIn("instance identity", res["error"])
        self.assertEqual(pub.prefix, "hass_demo-garage-")

    async def test_parity_and_cutover_say_undecided_and_count_nothing(self):
        pub = await self.undecided()
        with self.assertRaises(parity.IdsUndecided):
            await parity.compute_parity(_hass(), mock.Mock(), pub)
        view = parity.ParityView(_hass(), mock.Mock(), pub)
        with mock.patch.object(view, "json", side_effect=lambda d, **k: d):
            res = await view.get(SimpleNamespace(headers={"X-Requested-With": "fetch"}, query={}))
        self.assertEqual((res["ok"], res["ids_undecided"]), (False, True))
        self.assertIn("id format undecided: nothing is compared until it is decided", res["error"])
        self.assertNotIn("summary", res)
        installer = SimpleNamespace(running="demo", running_tag="1.0", settings=SimpleNamespace(data={}), smoke={},
                                    state=SimpleNamespace(pending_smoke=None))
        pub.build_health, pub.stats = (lambda: {"state": "ok"}), {"connected": True}
        self.assertIn("discovery waits", parity.CutoverView(_hass(), installer, pub)._status()["ids_undecided"])
        pub._ids_undecided = ""
        self.assertEqual(parity.CutoverView(_hass(), installer, pub)._status()["ids_undecided"], "")

    async def test_the_connect_decides_before_it_records(self):
        for complete in (True, False):
            with self.subTest(complete=complete):
                if os.path.exists(self.path):
                    os.remove(self.path)
                self.store.retained = dict([_config("hass_demo", "hass_demo_"), _config("hass_other", "hass_other_")])
                pub = self.pub_with(self.identity(), force_base_topic=True)
                pub._client, pub._connected, pub.stats, pub._stopping, pub._cleanup_pending = None, False, {}, False, {}
                with mock.patch.object(mp.MqttPublisher, "_new_client"), \
                        mock.patch.object(mp, "RETAINED_SCAN_MAX_BYTES", mp.RETAINED_SCAN_MAX_BYTES if complete else 10):
                    await pub.hass.async_add_executor_job(pub._connect)
                self.assertEqual(pub.stats["connect_error"], "")
                self.assertEqual((pub._live_prefix, bool(pub._ids_undecided)), ("hass_demo_", False) if complete else ("hass_demo-", True))
                self.assertEqual(self.record()["id_format"], disc.LEGACY_ID_FORMAT if complete else disc.ID_FORMAT_UNDECIDED)

    async def test_an_instance_never_scans(self):
        pub = self.pub_with(self.identity("garage"))
        await self.connect_names(pub)
        self.assertEqual((pub.prefix, self.scans, self.record()["id_format"]), ("hass_demo-garage-", [], disc.ID_FORMAT))

    async def test_a_move_to_the_plain_identity_asks_the_broker(self):
        """The record holds the instance: what hass_demo left retained (a Move that did not clear it) decides."""
        mp.write_json(self.path, {"base": "hass_demo-garage", "prefix": "homeassistant", "broker": {**BROKER, "port": self.port},
                                  "domain": "demo", "pinned": True, "id_format": disc.ID_FORMAT})
        ident = self.identity()
        self.assertIsNone(ident.id_format("hass_demo"))
        self.store.retained.update([_config("hass_demo", "hass_demo_")])
        pub = self.pub_with(ident)
        self.assertEqual(await pub.hass.async_add_executor_job(pub._decide_id_format, "hass_demo"), "")
        self.assertEqual(pub._prefix_for("hass_demo"), "hass_demo_")

    async def test_a_move_back_reads_the_broker_again(self):
        """What the scan gave hass_demo does not outlive a Move away: a Move back reads what is retained by then."""
        ident = self.identity()
        pub = self.pub_with(ident)
        await self.connect_names(pub)  # nothing retained: hass_demo-
        self.assertEqual(ident.id_format("hass_demo"), disc.ID_FORMAT)
        ident.release()  # a Move, as async_move_identity does it: released, then the new identity recorded
        ident.write({"base": "hass_demo-garage", "prefix": "homeassistant", "broker": {**BROKER, "port": self.port},
                     "domain": "demo", "pinned": True, "id_format": disc.ID_FORMAT})
        self.assertIsNone(ident.id_format("hass_demo"))
        self.store.retained.update([_config("hass_demo", "hass_demo_")])  # e.g. restored onto this broker meanwhile
        self.scans.clear()
        self.assertEqual(await pub.hass.async_add_executor_job(pub._decide_id_format, "hass_demo"), "")
        self.assertEqual((self.scans, pub._prefix_for("hass_demo")), (["ids"], "hass_demo_"))

    async def test_an_id_format_no_version_wrote_is_a_damaged_record(self):
        for value in ("2", 3, -1, None, True, 2.0):
            with self.subTest(value=value):
                mp.write_json(self.path, {"base": "hass_demo", "prefix": "homeassistant", "domain": "demo", "id_format": value})
                with mock.patch("custom_components.integration_manager.installer.events.emit"):
                    ident = self.identity()
                self.assertIn("id_format", ident.record_problem)
                # removing it keeps the ids the broker still shows: it does not re-create every entity
                self.assertIn("remove it: the identity is then chosen as for a volume that never published", ident.record_problem)
                self.assertIn("hass_<domain> keeps the hass_<domain>_ ids its retained discovery configs still hold", ident.record_problem)
                self.assertIsNone(ident.key("demo"))


class CollectQuietTest(unittest.TestCase):
    def test_it_says_when_its_time_limit_ended_a_burst(self):
        found, stop = {}, threading.Event()

        def arrive():
            while not stop.is_set():
                found[f"t{len(found)}"] = b"x"
                time.sleep(0.05)

        feeder = threading.Thread(target=arrive)
        feeder.start()
        try:
            self.assertIs(mp.MqttPublisher._collect_quiet(None, found, min_s=0.2, quiet_s=0.5, max_s=1.0), True)
        finally:
            stop.set()
            feeder.join()
        self.assertIs(mp.MqttPublisher._collect_quiet(None, found, min_s=0.2, quiet_s=0.3, max_s=5.0), False)


class IdFormatViewTest(unittest.IsolatedAsyncioTestCase):
    def post(self, body, content_type="application/json"):
        async def payload():
            if isinstance(body, Exception):
                raise body
            return body

        publisher = SimpleNamespace(async_set_id_format=mock.AsyncMock(return_value={"ok": True}))
        return views.MqttActionView(publisher).post(SimpleNamespace(content_type=content_type, json=payload), "id_format"), publisher

    async def test_a_bad_request_is_a_400(self):
        for body, content_type in (([], "application/json"), (ValueError("Expecting value"), "application/json"), ({}, "application/json"),
                                   ({"format": 0}, "application/json"), ({"format": 3}, "application/json"),
                                   ({"format": "1"}, "application/json"), ({"format": True}, "application/json"),
                                   ({"format": 1.0}, "application/json"), ({"format": 1}, "text/plain"),
                                   ({"format": 1, "confirm": "yes"}, "application/json"), ({"format": 1, "confirm": 1}, "application/json")):
            with self.subTest(body=body, content_type=content_type):
                call, publisher = self.post(body, content_type)
                self.assertEqual((await call).status, 400)
                publisher.async_set_id_format.assert_not_called()

    async def test_the_choice_reaches_the_publisher(self):
        for fmt in (disc.LEGACY_ID_FORMAT, disc.ID_FORMAT):
            for body, confirm in (({"format": fmt}, False), ({"format": fmt, "confirm": True}, True)):
                call, publisher = self.post(body)
                self.assertEqual(json.loads((await call).body), {"ok": True})
                publisher.async_set_id_format.assert_awaited_once_with(fmt, confirm=confirm)


class DisjointIdsTest(unittest.TestCase):
    """Containers a and a_binary share no unique id, device identifier or discovery id, and neither takes the
    other's for its own, in any pair of formats; with hass_<domain>_ ids on both, what the prefix rule cannot tell
    apart the origin gate does (DestructiveDiscoveryGateTest)."""

    def pub(self, base, prefix):
        pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(enabled=True)
        pub._key_provider = lambda: base
        pub._live_base, pub._live_prefix = base, prefix
        return pub

    def test_a_and_a_binary(self):
        for prefix_a, prefix_b in (("hass_a-", "hass_a_binary-"), ("hass_a_", "hass_a_binary-"), ("hass_a-", "hass_a_binary_")):
            with self.subTest(a=prefix_a, b=prefix_b):
                a, b = self.pub(A, prefix_a), self.pub("hass_a_binary", prefix_b)
                names_a = _names(a, ["binary_sensor.x", "sensor.power"], "binary_demo")
                names_b = _names(b, ["sensor.x", "sensor.power"], "demo")
                for kind in names_a:
                    self.assertFalse(names_a[kind] & names_b[kind], kind)
                # parity's rules for a hass_<domain>_ prefix take no rest with a "-" either: own_rest is the most they take
                for one, other in ((a, names_b), (b, names_a)):
                    for kind in ("unique_ids", "devices", "discovery_ids"):
                        ids = [x for x in other[kind] if not (kind == "discovery_ids" and x.endswith("_manager"))]
                        self.assertEqual([x for x in ids if disc.own_rest(one.prefix, x) is not None], [], (one.prefix, kind))

    def test_the_legacy_pair_is_what_the_gate_is_for(self):
        """hass_a_ + binary_sensor.x is hass_a_binary_ + sensor.x: the same unique id."""
        a, b = self.pub(A, "hass_a_"), self.pub("hass_a_binary", "hass_a_binary_")
        self.assertTrue(_names(a, ["binary_sensor.x"], "binary_demo")["unique_ids"] & _names(b, ["sensor.x"], "demo")["unique_ids"])

    def test_identity_prefix(self):
        self.assertEqual([disc.identity_prefix(A), disc.identity_prefix(A, disc.LEGACY_ID_FORMAT), disc.identity_prefix(A, disc.ID_FORMAT)],
                         ["hass_a-", "hass_a_", "hass_a-"])
        for fmt in (disc.LEGACY_ID_FORMAT, disc.ID_FORMAT):  # an instance whatever the format says
            self.assertEqual(disc.identity_prefix("hass_a-garage", fmt), "hass_a-garage-")
        self.assertEqual(disc.origin("hass_a-"), disc.origin("hass_a_"))  # the origin names the identity either way


if __name__ == "__main__":
    unittest.main()
