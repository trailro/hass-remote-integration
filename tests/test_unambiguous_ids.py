"""Unique ids, device identifiers and discovery ids: the id format of each volume, ids read back into "ours + rest"
(discovery.own_rest for a prefix ending in "-", the manager device's identifier mapped back to its discovery id), and
no container clearing another's discovery config.

The plain identity hass_<domain> put a "_" between itself and the rest (hass_a_ + sensor.x), and a "_" is also inside
domains: hass_a_ + binary_sensor.x is hass_a_binary_ + sensor.x.  A volume that never published its plain identity
uses hass_<domain>- (id_format 2); one that did keeps hass_<domain>_ for good, and nothing it announced changes.
"""

import asyncio
import json
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
from custom_components.integration_manager import parity
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
        """0.25.x's record (no domain) and 0.26.0's (domain, pinned): hass_<domain>_ ids, no scan, and every name
        0.26.0 announced is the same string."""
        broker = {**BROKER, "port": self.port}
        records = {"0.25.x": {**LEGACY, "base": "hass_demo", "broker": broker},
                   "0.26.0": {"base": "hass_demo", "prefix": "homeassistant", "broker": broker, "domain": "demo", "pinned": True}}
        entity_ids = ["sensor.power", "binary_sensor.door", "switch.pump"]
        for version, record in records.items():
            with self.subTest(version):
                mp.write_json(self.path, record)
                pub = self.pub_with(self.identity())
                await self.connect_names(pub)
                self.assertEqual((pub.prefix, self.scans), ("hass_demo_", []))
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
                # and the next start reads the record it wrote the same way
                self.assertEqual(self.pub_with(self.identity()).prefix, "hass_demo_")

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
