"""Per-instance MQTT identity: HRI_INSTANCE gives hass_<domain>_<instance>, so two containers (two HRI Manager instances)
of the same integration can share one broker and one main Home Assistant.  What a volume already published under stays:
an instance created before this (hass_hri_probe, its entities on the main HA) keeps its base topic, client id and
discovery unique ids when HRI_INSTANCE appears.  An invalid HRI_INSTANCE is refused loudly."""

import contextlib
import json
import os
import random
import re
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import installer as inst_mod
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager.installer import Installer, MqttIdentity, configured_instance
from tests.test_camp_preflight import _installer as _preflight_installer
from tests.test_camp_preflight import _run as _preflight_run
from tests.test_r13_mqtt import _Broker, _Case

# HRI Manager's instance names (hri_manager/hrimgr/names.py NAME_RE); the slug local_hri_<name> becomes HRI_INSTANCE
MANAGER_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,19}")
BROKER = {"host": "127.0.0.1", "port": 1883, "tls": False, "username": ""}
# what a volume that published hri_probe with 0.25.x recorded: no domain, no instance
LEGACY = {"base": "hass_hri_probe", "prefix": "homeassistant", "broker": BROKER}


def _env(value):
    env = {k: v for k, v in os.environ.items() if k != "HRI_INSTANCE"}
    if value is not None:
        env["HRI_INSTANCE"] = value
    return mock.patch.dict(os.environ, env, clear=True)


class _Dir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "mqtt_identity.json")
        self.domain = "hri_probe"

    def identity(self, env=None, record=None):
        if record is not None:
            mp.write_json(self.path, record)
        with _env(env):
            ident = MqttIdentity(self.path, lambda: self.domain)
        ident.load()
        return ident


class InstanceNameTest(unittest.TestCase):
    def test_unset_and_empty_are_no_instance(self):
        for value in (None, ""):
            with _env(value):
                self.assertEqual(configured_instance(), (None, None))

    def test_every_manager_name_is_accepted(self):
        rnd = random.Random(7)
        names = ["a", "garage", "garage_", "g__2", "a" * 20, "z9_"]
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_"
        names += [rnd.choice(alphabet[:26]) + "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 19))) for _ in range(500)]
        for name in names:
            self.assertTrue(MANAGER_NAME_RE.fullmatch(name), name)
            with _env(name):
                self.assertEqual(configured_instance(), (name, None), name)

    def test_a_docker_users_own_name(self):
        for name in ("1st", "b" * 32, "home_2"):
            with _env(name):
                self.assertEqual(configured_instance(), (name, None), name)

    def test_refused(self):
        for value in ("_garage", "Garage", "gar-age", "gar age", " garage", "garage\n", "c" * 33, "garáge", "a/b", "a+b", "#", " "):
            with _env(value):
                instance, problem = configured_instance()
            self.assertIsNone(instance, value)
            self.assertIn("HRI_INSTANCE", problem)
            self.assertIn("MQTT stays disconnected", problem)


class IdentityRuleTest(_Dir):
    def test_unset_is_unchanged(self):
        ident = self.identity()
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe")
        self.assertEqual(ident.source("hri_probe"), "default")
        self.assertIsNone(ident.key(None))
        self.assertIsNone(ident.source(None))

    def test_a_fresh_volume_takes_the_instance(self):
        ident = self.identity("garage")
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe_garage")
        self.assertEqual(ident.source("hri_probe"), "instance")
        self.assertIsNone(ident.describe()["identity_move_to"])

    def test_existing_manager_instance_keeps_its_identity(self):
        """A manager instance created before this release, updated: the entrypoint now exports HRI_INSTANCE=garage."""
        ident = self.identity("garage", LEGACY)
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe")
        self.assertEqual(ident.describe(), {"identity_source": "remembered", "identity_instance": "garage", "identity_problem": None,
                                            "identity_move_to": "hass_hri_probe_garage"})

    def test_a_legacy_record_of_another_integration_holds_nothing(self):
        ident = self.identity("garage", {**LEGACY, "base": "hass_other"})
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe_garage")

    def test_the_remembered_instance_stays_whatever_hri_instance_says_now(self):
        record = {**LEGACY, "base": "hass_hri_probe_old", "domain": "hri_probe"}
        for env, move_to in (("new", "hass_hri_probe_new"), (None, "hass_hri_probe"), ("old", None)):
            ident = self.identity(env, record)
            self.assertEqual(ident.key("hri_probe"), "hass_hri_probe_old", env)
            self.assertEqual(ident.describe()["identity_move_to"], move_to, env)

    def test_a_record_names_its_integration(self):
        """hass_x_foo recorded for the integration x_foo is not the instance foo of x."""
        self.domain = "x"
        ident = self.identity("bar", {**LEGACY, "base": "hass_x_foo", "domain": "x_foo"})
        self.assertEqual(ident.key("x"), "hass_x_bar")
        self.assertEqual(ident.key("x_foo"), "hass_x_foo")

    def test_a_released_record_holds_nothing(self):
        ident = self.identity("garage", {**LEGACY, "domain": "hri_probe", "released": True})
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe_garage")

    def test_an_invalid_instance_is_never_used(self):
        ident = self.identity("Garage")
        self.assertIsNone(ident.key("hri_probe"))
        self.assertEqual(ident.source("hri_probe"), "invalid")
        self.assertIn("'Garage'", ident.describe()["identity_problem"])
        # what was published stays that identity (an uninstall still clears it), and the publisher refuses to connect
        ident = self.identity("Garage", LEGACY)
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe")
        self.assertIsNone(ident.describe()["identity_move_to"])

    def test_a_damaged_record_is_no_record(self):
        for record in ([], {"base": 3}, {"base": "hass_hri_probe_Bad", "domain": "hri_probe"}):
            ident = self.identity("garage", record)
            self.assertEqual(ident.key("hri_probe"), "hass_hri_probe_garage", record)


class InstallerIdentityTest(unittest.TestCase):
    def installer(self, env, record=None):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        os.makedirs(os.path.join(cfg, "integration_manager"))
        if record is not None:
            mp.write_json(os.path.join(cfg, "integration_manager", "mqtt_identity.json"), record)

        async def executor(fn, *args):
            return fn(*args)

        with _env(env), mock.patch.object(inst_mod.events, "emit") as emit, (self.assertLogs(inst_mod._LOGGER, "ERROR") if env == "Bad" else contextlib.nullcontext()):
            inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components=set()), async_add_executor_job=executor))
        inst.state.domain = "hri_probe"
        return inst, emit

    def test_an_invalid_instance_is_logged_and_on_the_timeline(self):
        inst, emit = self.installer("Bad")
        self.assertIsNone(inst.instance_key)
        emit.assert_called_once()
        self.assertIn("HRI_INSTANCE='Bad'", emit.call_args.args[1])

    def test_the_identity_of_an_uninstall_is_the_one_published(self):
        inst, _emit = self.installer("garage", LEGACY)
        self.assertEqual(inst.instance_key, "hass_hri_probe")
        inst.state.domain = None  # the uninstall stops it first
        self.assertEqual(inst.identity_for("hri_probe"), "hass_hri_probe")
        self.assertEqual(inst.identity_for("other"), "hass_other_garage")


class PreflightNoticeTest(unittest.TestCase):
    def test_an_invalid_instance_is_a_preflight_warning(self):
        inst = _preflight_installer(self, {"__init__.py": ""}, {"domain": "demo", "version": "2.0", "config_flow": True, "requirements": []})
        with _env("-x"):
            inst.mqtt_identity = MqttIdentity(os.path.join(inst.state_dir, "mqtt_identity.json"), lambda: "demo")
        report = _preflight_run(inst)
        self.assertTrue(report["ok"])  # MQTT is not the integration's fault: a warning, not a blocker
        self.assertIn(inst.mqtt_identity.problem, report["warnings"])


def _publisher(identity, domain):
    pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig(enabled=True)
    pub._identity = identity
    pub._key_provider = lambda: identity.key(domain)
    pub._live_base = pub._live_prefix = None
    return pub


def _names(pub, entity_ids):
    """Everything the publisher derives from its identity, by kind."""
    topics = {pub._status_topic(), pub._health_topic(), pub._manager_topic(), pub._cmd_base(), pub._call_base(), pub._manager_cmd_base()}
    topics |= {pub._topic_for(eid, "demo") for eid in entity_ids}
    registry = SimpleNamespace(async_get=lambda _eid: None)
    with mock.patch.object(disc.er, "async_get", return_value=registry):
        comps = [disc.build_component(None, State(eid, "on"), pub._topic_for(eid, "demo"), pub._cmd_base(), pub.prefix) for eid in entity_ids]
    disc_id, block = disc.device_block(None, None, "demo", pub.prefix)
    mgr_id, mgr_block, mgr_comps = disc.manager_device(
        pub.base_topic, pub.prefix, {"status": pub._status_topic(), "health": pub._health_topic(), "manager": pub._manager_topic(),
                                     "cmd": pub._manager_cmd_base()}, "demo", "0.26.0", True)
    unique_ids = {c["unique_id"] for c in comps} | {c["unique_id"] for c in mgr_comps.values()}
    topics |= {pub._discovery_topic(disc_id), pub._discovery_topic(mgr_id)}
    for c in comps + list(mgr_comps.values()):
        topics |= {v for k, v in c.items() if k.endswith("_topic") and isinstance(v, str)}
        topics |= {a["topic"] for a in c.get("availability", [])}
    return {"topics": topics, "client_id": {pub.client_id}, "unique_ids": unique_ids,
            "devices": set(block["identifiers"]) | set(mgr_block["identifiers"]) | {mgr_block["name"]},
            "manager_entity_ids": set(mgr_comps)}


class TwoInstancesTest(_Dir):
    def test_two_instances_of_one_integration_share_nothing(self):
        """The default container and a manager instance garage, both running demo, on one broker and one main HA."""
        self.domain = "demo"
        entity_ids = ["switch.garage_door", "sensor.power", "light.hall", "button.restart", "sensor.garage_temp"]
        a = _publisher(self.identity(None), "demo")
        other = os.path.join(self.dir, "b")
        os.makedirs(other)
        with _env("garage"):
            b = _publisher(MqttIdentity(os.path.join(other, "mqtt_identity.json"), lambda: "demo"), "demo")
        self.assertEqual((a.base_topic, b.base_topic), ("hass_demo", "hass_demo_garage"))
        names_a, names_b = _names(a, entity_ids), _names(b, entity_ids)
        for kind in names_a:
            self.assertTrue(names_a[kind], kind)
            self.assertFalse(names_a[kind] & names_b[kind], kind)
        # no topic of one is under the other's base topic, nor matches the other's subscriptions
        for t in names_b["topics"]:
            self.assertFalse(t.startswith("hass_demo/"), t)
        for t in names_a["topics"]:
            self.assertFalse(t.startswith("hass_demo_garage/"), t)
        # and the retained-data ownership test of one never claims the other's discovery config
        config_b = json.dumps({"origin": disc.origin(b.prefix)}).encode()
        self.assertFalse(a._is_ours("homeassistant/device/hass_demo_garage_demo_nodevice/config", config_b, a.base_topic))


class PublisherIdentityTest(_Case):
    """The publisher with the real identity: what it records, what it connects as, and the Move."""

    def identity(self, env, domain="hri_probe"):
        with _env(env):
            ident = MqttIdentity(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), lambda: domain)
        ident.load()
        return ident

    def record(self):
        with open(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def pub_with(self, ident, domain="hri_probe", **config):
        pub = self.publisher(**config)
        pub._identity = ident
        pub._key_provider = lambda: ident.key(domain)
        return pub

    async def test_existing_manager_instance_keeps_names_through_the_update(self):
        """hass_hri_probe published by 0.25.x; the update brings HRI_INSTANCE=garage: same base topic, client id,
        manager device and discovery unique ids, nothing swept, and the record now names its integration."""
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), {**LEGACY, "broker": {**BROKER, "port": self.port}})
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        self.assertEqual((pub.wanted_base_topic, pub.base_topic, pub.client_id, pub.prefix),
                         ("hass_hri_probe", "hass_hri_probe", "hass_hri_probe", "hass_hri_probe_"))
        before = disc.manager_device("hass_hri_probe", "hass_hri_probe_", dict.fromkeys(("status", "health", "manager", "cmd"), "t"), "hri_probe", "", True)
        self.assertEqual(disc.manager_device(pub.base_topic, pub.prefix, dict.fromkeys(("status", "health", "manager", "cmd"), "t"), "hri_probe", "", True),
                         before)
        broker = _Broker({})
        with mock.patch.object(mp.MqttPublisher, "_throwaway_client", lambda self, *a: broker.client(*a)):
            self.assertTrue(await pub.hass.async_add_executor_job(pub._sweep_old_identity, pub.wanted_base_topic))
        self.assertEqual((broker.scans, broker.cleared), ([], []))
        self.assertEqual({k: v for k, v in self.record().items() if k != "broker"},
                         {"base": "hass_hri_probe", "prefix": "homeassistant", "domain": "hri_probe"})
        # the next boot reads that record: still the same names, with or without HRI_INSTANCE
        for env in ("garage", None, "other"):
            self.assertEqual(self.identity(env).key("hri_probe"), "hass_hri_probe", env)

    async def test_a_fresh_volume_records_the_instance_and_keeps_it(self):
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        self.assertEqual(pub.wanted_base_topic, "hass_hri_probe_garage")
        await pub.hass.async_add_executor_job(pub._remember_identity, pub.wanted_base_topic, "homeassistant")
        self.assertEqual(self.record()["domain"], "hri_probe")
        self.assertEqual(ident.source("hri_probe"), "remembered")
        self.assertEqual(self.identity(None).key("hri_probe"), "hass_hri_probe_garage")  # HRI_INSTANCE removed later: kept

    async def test_an_invalid_instance_never_connects(self):
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), LEGACY)
        pub = self.pub_with(self.identity("-bad"), force_base_topic=True)
        pub._client, pub._connected, pub.stats = None, False, {}
        with mock.patch.object(mp.mqtt, "Client") as client, mock.patch.object(mp.MqttPublisher, "_sweep_old_identity") as sweep:
            await pub.hass.async_add_executor_job(pub._connect)
        client.assert_not_called()
        sweep.assert_not_called()
        self.assertEqual(pub.stats["connect_error"], f"not connecting: {pub._identity.problem}")
        self.assertIsNone(pub._live_base)

    def moving(self, pub):
        pub._connected, pub._live_base, pub._live_prefix = True, "hass_hri_probe", "hass_hri_probe_"
        pub._moving, pub._pending_clears, pub._compat_warned, pub._services_published = False, set(), set(), set()
        pub._republish_interval = pub.config.republish_interval_s
        pub._drop_newly_excluded = lambda new: None
        cleared = []

        def clear(base, prefix, docs=True):
            cleared.append((base, prefix, docs))
            return 4

        mock.patch.object(pub, "_load", return_value=pub.config).start()
        mock.patch.object(pub, "_clear_retained_under", side_effect=clear).start()
        mock.patch.object(pub, "_disconnect").start()
        mock.patch.object(pub, "_connect").start()
        mock.patch.object(pub, "publish_health").start()
        return cleared

    async def test_move_clears_the_old_names_and_records_the_new(self):
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), {**LEGACY, "broker": {**BROKER, "port": self.port}})
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        cleared = self.moving(pub)
        self.assertEqual(await pub.async_move_identity("hass_hri_probe_other"),
                         {"ok": False, "error": "the identity to move to is hass_hri_probe_garage now, not hass_hri_probe_other: reload the page"})
        pub._connected = False
        res = await pub.async_move_identity("hass_hri_probe_garage")
        self.assertFalse(res["ok"])
        self.assertIn("not connected", res["error"])
        self.assertEqual((cleared, ident.key("hri_probe")), ([], "hass_hri_probe"))  # a refusal changes nothing
        pub._connected = True
        res = await pub.async_move_identity("hass_hri_probe_garage")
        self.assertEqual(res, {"ok": True, "from": "hass_hri_probe", "to": "hass_hri_probe_garage"})
        self.assertEqual(cleared, [("hass_hri_probe", "homeassistant", True)])  # documents and discovery configs of the old names
        self.assertEqual({k: v for k, v in self.record().items() if k != "broker"},
                         {"base": "hass_hri_probe_garage", "prefix": "homeassistant", "domain": "hri_probe"})
        self.assertEqual(self.identity(None).key("hri_probe"), "hass_hri_probe_garage")
        self.assertEqual(await pub.async_move_identity("hass_hri_probe_garage"),
                         {"ok": False, "error": "nothing to move: the identity is the one the rule gives"})

    async def test_a_failed_move_sweep_is_retried_from_the_record(self):
        """The broker did not take the clear: the record keeps the old names (released), so the next connect sweeps them."""
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), {**LEGACY, "broker": {**BROKER, "port": self.port}})
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        self.moving(pub)
        pub._clear_retained_under.side_effect = lambda *a, **k: None
        self.assertTrue((await pub.async_move_identity("hass_hri_probe_garage"))["ok"])
        self.assertEqual((self.record()["base"], self.record()["released"]), ("hass_hri_probe", True))
        self.assertEqual(self.identity("garage").key("hri_probe"), "hass_hri_probe_garage")

    async def test_status_says_where_the_base_topic_comes_from(self):
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        self.assertEqual(pub.public_config()["identity_source"], "instance")
        self.assertEqual(pub.public_config()["base_topic"], "hass_hri_probe_garage")


if __name__ == "__main__":
    unittest.main()
