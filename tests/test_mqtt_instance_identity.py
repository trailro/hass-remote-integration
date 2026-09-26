"""Per-instance MQTT identity: HRI_INSTANCE gives hass_<domain>-<instance>, so two containers (two HRI Manager instances)
of the same integration can share one broker and one main Home Assistant.  What a volume already published under stays:
an instance created before this (hass_hri_probe, its entities on the main HA) keeps its base topic, client id and
discovery unique ids when HRI_INSTANCE appears.  An invalid HRI_INSTANCE is refused loudly."""

import asyncio
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


def _env(value, unknown=None):
    env = {k: v for k, v in os.environ.items() if k not in ("HRI_INSTANCE", "HRI_INSTANCE_UNKNOWN")}
    if value is not None:
        env["HRI_INSTANCE"] = value
    if unknown is not None:
        env["HRI_INSTANCE_UNKNOWN"] = unknown  # the app's entrypoint, when the Supervisor did not say which app it is
    return mock.patch.dict(os.environ, env, clear=True)


class _Dir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "mqtt_identity.json")
        self.domain = "hri_probe"

    def identity(self, env=None, record=None, unknown=None):
        if record is not None:
            mp.write_json(self.path, record)
        with _env(env, unknown):
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
            self.assertIn("correct or remove it", problem)

    def test_an_app_that_could_not_read_its_slug_has_an_unknown_instance(self):
        with _env(None, "1"):
            instance, problem = configured_instance()
        self.assertIsNone(instance)
        self.assertIn("the app's slug could not be read from the Supervisor", problem)
        self.assertIn("restart the app", problem)
        with _env("garage", "1"):  # set explicitly: known, whatever the Supervisor said
            self.assertEqual(configured_instance(), ("garage", None))
        for unknown in ("", "0"):
            with _env(None, unknown):
                self.assertEqual(configured_instance(), (None, None), unknown)


class IdentityRuleTest(_Dir):
    def test_unset_is_unchanged(self):
        ident = self.identity()
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe")
        self.assertEqual(ident.source("hri_probe"), "default")
        self.assertIsNone(ident.key(None))
        self.assertIsNone(ident.source(None))

    def test_a_fresh_volume_takes_the_instance(self):
        ident = self.identity("garage")
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe-garage")
        self.assertEqual(ident.source("hri_probe"), "instance")
        self.assertIsNone(ident.describe()["identity_move_to"])

    def test_existing_manager_instance_keeps_its_identity(self):
        """A manager instance created before this release, updated: the entrypoint now exports HRI_INSTANCE=garage."""
        ident = self.identity("garage", LEGACY)
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe")
        self.assertEqual(ident.describe(), {"identity_source": "remembered", "identity_instance": "garage", "identity_problem": None,
                                            "identity_warning": None, "identity_move_to": "hass_hri_probe-garage"})

    def test_a_legacy_record_of_another_integration_holds_nothing(self):
        ident = self.identity("garage", {**LEGACY, "base": "hass_other"})
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe-garage")

    def test_the_remembered_instance_stays_whatever_hri_instance_says_now(self):
        record = {**LEGACY, "base": "hass_hri_probe-old", "domain": "hri_probe", "pinned": True}
        for env, move_to in (("new", "hass_hri_probe-new"), (None, "hass_hri_probe"), ("old", None)):
            ident = self.identity(env, record)
            self.assertEqual(ident.key("hri_probe"), "hass_hri_probe-old", env)
            self.assertEqual(ident.describe()["identity_move_to"], move_to, env)

    def test_a_record_names_its_integration(self):
        """hass_x_foo recorded for the integration x_foo is not the instance foo of x."""
        self.domain = "x"
        ident = self.identity("bar", {**LEGACY, "base": "hass_x_foo", "domain": "x_foo", "pinned": True})
        self.assertEqual(ident.key("x"), "hass_x-bar")
        self.assertEqual(ident.key("x_foo"), "hass_x_foo")

    def test_a_record_not_pinned_yet_holds_nothing(self):
        """Recorded at a first connection that did not hold (another client took the client id back at once, the
        broker refused it): the identity still follows HRI_INSTANCE."""
        record = {**LEGACY, "base": "hass_hri_probe", "domain": "hri_probe", "pinned": False}
        for env, key in ((None, "hass_hri_probe"), ("garage", "hass_hri_probe-garage")):
            ident = self.identity(env, record)
            self.assertEqual((ident.key("hri_probe"), ident.source("hri_probe")), (key, "instance" if env else "default"), env)
            self.assertIsNone(ident.describe()["identity_move_to"])
        # pinned only for the base and the integration it records, and once
        ident = self.identity("garage", record)
        self.assertFalse(ident.pin("hass_hri_probe-garage"))
        self.domain = "other"
        self.assertFalse(ident.pin("hass_hri_probe"))
        self.domain = "hri_probe"
        self.assertTrue(ident.pin("hass_hri_probe"))
        self.assertFalse(ident.pin("hass_hri_probe"))
        self.assertEqual(self.identity("garage").key("hri_probe"), "hass_hri_probe")
        self.assertFalse(self.identity("garage", {**record, "released": True}).pin("hass_hri_probe"))
        self.assertFalse(self.identity("garage", LEGACY).pin("hass_hri_probe"))  # 0.25.x: kept already

    def test_a_released_record_holds_nothing(self):
        ident = self.identity("garage", {**LEGACY, "domain": "hri_probe", "released": True})
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe-garage")

    def test_an_invalid_instance_is_never_used(self):
        ident = self.identity("Garage")
        self.assertIsNone(ident.key("hri_probe"))
        self.assertEqual(ident.source("hri_probe"), "invalid")
        self.assertIn("'Garage'", ident.describe()["identity_problem"])
        self.assertIn("MQTT stays disconnected", ident.blocking())
        self.assertIsNone(ident.describe()["identity_warning"])

    def test_an_invalid_instance_blocks_only_what_would_take_it(self):
        """A remembered identity does not need HRI_INSTANCE: it keeps connecting, with the problem as a warning."""
        for env, unknown, text in (("Garage", None, "'Garage'"), (None, "1", "restart the app")):
            ident = self.identity(env, LEGACY, unknown)
            self.assertEqual(ident.key("hri_probe"), "hass_hri_probe", env)
            self.assertEqual(ident.source("hri_probe"), "remembered")
            self.assertIsNone(ident.blocking())
            info = ident.describe()
            self.assertIsNone(info["identity_problem"])
            self.assertIn(text, info["identity_warning"])
            self.assertIn("not used: hri_probe keeps hass_hri_probe", info["identity_warning"])
            self.assertIsNone(info["identity_move_to"])
            # another integration on the same volume would take it: that one has no identity
            self.assertIsNone(ident.key("other"))
            self.assertIn(text, ident.problem_for("other"))
            self.assertIsNone(ident.warning_for("other"))

    def test_an_unknown_instance_pins_nothing_on_a_fresh_volume(self):
        ident = self.identity(None, unknown="1")
        self.assertIsNone(ident.key("hri_probe"))
        self.assertEqual(ident.source("hri_probe"), "invalid")
        self.assertIn("the app's slug could not be read from the Supervisor", ident.blocking())
        self.assertIn("MQTT stays disconnected", ident.blocking())

    def test_only_a_missing_record_is_a_fresh_volume(self):
        ident = self.identity("garage")
        self.assertEqual((ident.record_problem, ident.blocking()), (None, None))
        self.assertEqual(ident.key("hri_probe"), "hass_hri_probe-garage")

    def damaged(self, write):
        write()
        with _env("garage"), mock.patch.object(inst_mod.events, "emit") as emit, self.assertLogs(inst_mod._LOGGER, "ERROR"):
            ident = MqttIdentity(self.path, lambda: self.domain)
            ident.load()
        emit.assert_called_once_with("mqtt", ident.record_problem)
        return ident

    def test_a_damaged_record_moves_nothing(self):
        """A record that cannot be read, or that no version wrote, gives no identity at all: never the fresh volume's."""
        records = ([], {}, {"base": 3}, {"prefix": "homeassistant"}, {"base": "hass_hri_probe-Bad", "domain": "hri_probe"},
                   {"base": "hass_hri_probe", "domain": "Bad"}, {"base": "hass_other", "domain": "hri_probe"},
                   {"base": "hass_hri_probe", "domain": "hri_probe", "released": "yes"},
                   # no domain: only what 0.25.x wrote (its plain name, prefix, broker) is a record of an older version
                   {"base": "hass_hri_probe-garage"}, {**LEGACY, "released": True}, {**LEGACY, "base": "hass_Bad"})
        texts = ("", "{", '{"base": "hass_hri_probe"', "\x00\xff")
        cases = [lambda r=r: mp.write_json(self.path, r) for r in records]
        cases += [lambda t=t: open(self.path, "w", encoding="latin-1").write(t) for t in texts]
        cases.append(lambda: (os.remove(self.path), os.makedirs(self.path)))  # cannot be read at all
        for i, write in enumerate(cases):
            with self.subTest(i):
                if os.path.isdir(self.path):
                    os.rmdir(self.path)
                ident = self.damaged(write)
                self.assertIn("mqtt_identity.json", ident.record_problem)
                self.assertIn("MQTT stays disconnected", ident.record_problem)
                self.assertIsNone(ident.key("hri_probe"))
                self.assertIsNone(ident.key("other"))
                self.assertEqual(ident.source("hri_probe"), "invalid")
                self.assertEqual(ident.describe()["identity_problem"], ident.record_problem)
                self.assertEqual(ident.blocking(), ident.record_problem)
                self.assertIsNone(ident.describe()["identity_move_to"])

    def test_a_corrected_record_is_read_again(self):
        ident = self.damaged(lambda: mp.write_json(self.path, {"base": 3}))
        mp.write_json(self.path, LEGACY)
        ident.load()
        self.assertEqual((ident.record_problem, ident.key("hri_probe")), (None, "hass_hri_probe"))

    def test_a_record_is_written_synced_and_only_when_it_changed(self):
        ident = self.identity("garage", LEGACY)
        record = {**LEGACY, "domain": "hri_probe"}
        with mock.patch.object(inst_mod, "write_json", wraps=inst_mod.write_json) as write:
            ident.write(record)
            ident.write(dict(record))
            ident.release()
            ident.release()
        self.assertEqual([c.args[1] for c in write.call_args_list], [record, {**record, "released": True}])
        self.assertTrue(all(c.kwargs.get("fsync") is True for c in write.call_args_list))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {**record, "released": True})


class InstallerIdentityTest(unittest.TestCase):
    def installer(self, env, record=None, level=None, unknown=None):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        os.makedirs(os.path.join(cfg, "integration_manager"))
        if record is not None:
            mp.write_json(os.path.join(cfg, "integration_manager", "mqtt_identity.json"), record)
            mp.write_json(os.path.join(cfg, "integration_manager", "state.json"), {"domain": "hri_probe", "installed": {"hri_probe": {}}})

        async def executor(fn, *args):
            return fn(*args)

        with _env(env, unknown), mock.patch.object(inst_mod.events, "emit") as emit, \
                (self.assertLogs(inst_mod._LOGGER, level) if level else contextlib.nullcontext()) as logs:
            inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components=set()), async_add_executor_job=executor))
        if level:
            self.assertEqual({r.levelname for r in logs.records if r.getMessage().startswith("MQTT: ")}, {level})
        inst.state.domain = "hri_probe"
        return inst, emit

    def test_an_invalid_instance_is_logged_and_on_the_timeline(self):
        inst, emit = self.installer("Bad", level="ERROR")
        self.assertIsNone(inst.instance_key)
        emit.assert_called_once()
        self.assertIn("HRI_INSTANCE='Bad'", emit.call_args.args[1])

    def test_an_unused_invalid_instance_is_a_warning(self):
        for env, unknown in (("Bad", None), (None, "1")):
            inst, emit = self.installer(env, LEGACY, level="WARNING", unknown=unknown)
            self.assertEqual(inst.instance_key, "hass_hri_probe")
            emit.assert_called_once()
            self.assertIn("not used: hri_probe keeps hass_hri_probe", emit.call_args.args[1])

    def test_the_identity_of_an_uninstall_is_the_one_published(self):
        inst, _emit = self.installer("garage", LEGACY)
        self.assertEqual(inst.instance_key, "hass_hri_probe")
        inst.state.domain = None  # the uninstall stops it first
        self.assertEqual(inst.identity_for("hri_probe"), "hass_hri_probe")
        self.assertEqual(inst.identity_for("other"), "hass_other-garage")


class PreflightNoticeTest(unittest.TestCase):
    def test_an_invalid_instance_is_a_preflight_warning(self):
        inst = _preflight_installer(self, {"__init__.py": ""}, {"domain": "demo", "version": "2.0", "config_flow": True, "requirements": []})
        with _env("-x"):
            inst.mqtt_identity = MqttIdentity(os.path.join(inst.state_dir, "mqtt_identity.json"), lambda: "demo")
        report = _preflight_run(inst)
        self.assertTrue(report["ok"])  # MQTT is not the integration's fault: a warning, not a blocker
        self.assertIn(inst.mqtt_identity.problem_for("demo"), report["warnings"])
        # with an identity remembered for it, the problem does not keep it off MQTT, and is still reported
        mp.write_json(inst.mqtt_identity.path, {"base": "hass_demo", "prefix": "homeassistant"})
        inst.mqtt_identity.load()
        report = _preflight_run(inst)
        self.assertIsNone(inst.mqtt_identity.problem_for("demo"))
        self.assertIn(inst.mqtt_identity.warning_for("demo"), report["warnings"])


def _publisher(identity, domain):
    pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig(enabled=True)
    pub._identity = identity
    pub._key_provider = lambda: identity.key(domain)
    pub._live_base = pub._live_prefix = None
    return pub


def _names(pub, entity_ids, domain="demo"):
    """Everything the publisher derives from its identity, by kind."""
    topics = {pub._status_topic(), pub._health_topic(), pub._manager_topic(), pub._cmd_base(), pub._call_base(), pub._manager_cmd_base()}
    topics |= {pub._topic_for(eid, domain) for eid in entity_ids}
    registry = SimpleNamespace(async_get=lambda _eid: None)
    with mock.patch.object(disc.er, "async_get", return_value=registry):
        comps = [disc.build_component(None, State(eid, "on"), pub._topic_for(eid, domain), pub._cmd_base(), pub.prefix) for eid in entity_ids]
    disc_id, block = disc.device_block(None, None, domain, pub.prefix)
    mgr_id, mgr_block, mgr_comps = disc.manager_device(
        pub.base_topic, pub.prefix, {"status": pub._status_topic(), "health": pub._health_topic(), "manager": pub._manager_topic(),
                                     "cmd": pub._manager_cmd_base()}, domain, "0.26.0", True)
    unique_ids = {c["unique_id"] for c in comps} | {c["unique_id"] for c in mgr_comps.values()}
    topics |= {pub._discovery_topic(disc_id), pub._discovery_topic(mgr_id)}
    for c in comps + list(mgr_comps.values()):
        topics |= {v for k, v in c.items() if k.endswith("_topic") and isinstance(v, str)}
        topics |= {a["topic"] for a in c.get("availability", [])}
    return {"topics": topics, "client_id": {pub.client_id}, "unique_ids": unique_ids, "discovery_ids": {disc_id, mgr_id},
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
        self.assertEqual((a.base_topic, b.base_topic), ("hass_demo", "hass_demo-garage"))
        names_a, names_b = _names(a, entity_ids), _names(b, entity_ids)
        for kind in names_a:
            self.assertTrue(names_a[kind], kind)
            self.assertFalse(names_a[kind] & names_b[kind], kind)
        # no topic of one is under the other's base topic, nor matches the other's subscriptions
        for t in names_b["topics"]:
            self.assertFalse(t.startswith("hass_demo/"), t)
        for t in names_a["topics"]:
            self.assertFalse(t.startswith("hass_demo-garage/"), t)
        # and the retained-data ownership test of one never claims the other's discovery config
        config_b = json.dumps({"origin": disc.origin(b.prefix)}).encode()
        self.assertFalse(a._is_ours("homeassistant/device/hass_demo-garage_demo_nodevice/config", config_b, a.base_topic))


class UnambiguousIdentityTest(_Dir):
    """No identity is another's: not an instance against another integration's plain name, not two instances of one
    integration whose names only differ by what follows a "_", and no unique id of one is another's."""

    def pub(self, domain, instance):
        sub = os.path.join(self.dir, f"{domain}-{instance}")
        os.makedirs(sub)
        with _env(instance):
            return _publisher(MqttIdentity(os.path.join(sub, "mqtt_identity.json"), lambda: domain), domain)

    def assert_share_nothing(self, a, b, entity_ids_a, entity_ids_b, domain_a, domain_b):
        names_a, names_b = _names(a, entity_ids_a, domain_a), _names(b, entity_ids_b, domain_b)
        for kind in names_a:
            self.assertFalse(names_a[kind] & names_b[kind], (kind, names_a[kind] & names_b[kind]))
        # parity takes a unique id or a device identifier that starts with its prefix for its own
        self.assertFalse(a.prefix.startswith(b.prefix) or b.prefix.startswith(a.prefix), (a.prefix, b.prefix))
        for t in names_b["topics"]:
            self.assertFalse(t.startswith(a.base_topic + "/"), t)
        for t in names_a["topics"]:
            self.assertFalse(t.startswith(b.base_topic + "/"), t)

    def test_an_instance_is_not_another_integrations_plain_name(self):
        """Integration hri, instance probe, next to the integration hri_probe with no instance."""
        instance, plain = self.pub("hri", "probe"), self.pub("hri_probe", None)
        self.assertEqual((instance.base_topic, plain.base_topic), ("hass_hri-probe", "hass_hri_probe"))
        self.assert_share_nothing(instance, plain, ["sensor.power"], ["sensor.power"], "hri", "hri_probe")
        # nor does a record of what the integration hri_probe published count for hri's instance probe
        with _env("probe"):
            ident = MqttIdentity(os.path.join(self.dir, "legacy.json"), lambda: "hri")
        mp.write_json(ident.path, LEGACY)
        ident.load()
        self.assertEqual(ident.key("hri"), "hass_hri-probe")

    def test_no_unique_id_of_an_instance_is_the_plain_ones(self):
        """hass_a_ + binary_sensor.foo would be the instance binary's sensor.foo with a "_" between them."""
        plain, instance = self.pub("a", None), self.pub("a", "binary")
        self.assert_share_nothing(plain, instance, ["binary_sensor.foo"], ["sensor.foo"], "a", "a")

    def test_two_instances_whose_names_share_a_start(self):
        garage, garage_binary = self.pub("x", "garage"), self.pub("x", "garage_binary")
        self.assert_share_nothing(garage, garage_binary, ["binary_sensor.foo", "sensor.manager"], ["sensor.foo"], "x", "x")
        config = json.dumps({"origin": disc.origin(garage_binary.prefix)}).encode()
        self.assertFalse(garage._is_ours("homeassistant/device/hass_x-garage_binary_manager/config", config, garage.base_topic))

    def test_the_separator_is_in_no_domain_and_fits_every_name(self):
        from homeassistant.core import valid_domain

        self.assertFalse(valid_domain("a" + disc.INSTANCE_SEP + "b"))
        self.assertIsNone(inst_mod._DOMAIN_RE.match("a" + disc.INSTANCE_SEP + "b"))
        # a discovery node or object id (Home Assistant's mqtt TOPIC_MATCHER), a topic level, a client id of the
        # characters MQTT brokers take
        self.assertRegex(disc.INSTANCE_SEP, r"\A[a-zA-Z0-9_-]\Z")
        self.assertNotIn(disc.INSTANCE_SEP, "+#/$\0")


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
                         {"base": "hass_hri_probe", "prefix": "homeassistant", "domain": "hri_probe", "pinned": True})
        # the next boot reads that record: still the same names, with or without HRI_INSTANCE
        for env in ("garage", None, "other"):
            self.assertEqual(self.identity(env).key("hri_probe"), "hass_hri_probe", env)

    def held(self, pub, lived):
        """The publisher connected ``lived`` seconds ago on a current client."""
        pub._client, pub._connected, pub._moving = object(), True, False
        pub._connected_at, pub._live_base = mp.time.monotonic() - lived, pub.wanted_base_topic
        return pub._client

    async def test_a_fresh_volume_keeps_the_instance_once_its_connection_held(self):
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        pub.hass.async_create_task = asyncio.ensure_future
        self.assertEqual(pub.wanted_base_topic, "hass_hri_probe-garage")
        await pub.hass.async_add_executor_job(pub._remember_identity, pub.wanted_base_topic, "homeassistant")
        self.assertEqual((self.record()["domain"], self.record()["pinned"]), ("hri_probe", False))
        self.assertEqual(ident.source("hri_probe"), "instance")
        self.assertEqual(self.identity(None).key("hri_probe"), "hass_hri_probe")  # not kept yet: HRI_INSTANCE still decides
        # a connection that did not hold, or a client that is not the current one, pins nothing
        client = self.held(pub, mp.DROP_AFTER_CONNECT_S - 2)
        pub._connection_held(client)
        pub._connection_held(object())
        pub._connected = False
        pub._connection_held(client)
        await asyncio.sleep(0.05)
        self.assertFalse(self.record()["pinned"])
        client = self.held(pub, mp.DROP_AFTER_CONNECT_S + 1)
        pub._connection_held(client)
        for _ in range(100):
            if self.record()["pinned"]:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.record()["pinned"])
        self.assertEqual(ident.source("hri_probe"), "remembered")
        self.assertEqual(self.identity(None).key("hri_probe"), "hass_hri_probe-garage")  # HRI_INSTANCE removed later: kept
        self.emit.assert_any_call("mqtt", "identity hass_hri_probe-garage kept from now on: its first connection held")

    async def test_the_pin_waits_for_a_move_in_progress(self):
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        await pub.hass.async_add_executor_job(pub._remember_identity, pub.wanted_base_topic, "homeassistant")
        client = self.held(pub, mp.DROP_AFTER_CONNECT_S + 1)
        async with pub._conn_lock:
            task = asyncio.ensure_future(pub._async_pin(client))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            pub._client = object()  # the reconnect under the lock replaced the client
        await task
        self.assertFalse(self.record()["pinned"])

    async def test_a_damaged_record_never_connects_until_corrected(self):
        path = os.path.join(self.dir, "integration_manager", "mqtt_identity.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"base": "hass_hri_pro')
        with mock.patch.object(inst_mod.events, "emit"):
            ident = self.identity("garage")
        pub = self.pub_with(ident, force_base_topic=True)
        pub._client, pub._connected, pub.stats = None, False, {}
        with mock.patch.object(mp.mqtt, "Client") as client, mock.patch.object(mp.MqttPublisher, "_sweep_old_identity") as sweep:
            await pub.hass.async_add_executor_job(pub._connect)
        client.assert_not_called()
        sweep.assert_not_called()
        self.assertEqual(pub.stats["connect_error"], f"not connecting: {ident.record_problem}")
        self.assertEqual(ident.describe()["identity_problem"], ident.record_problem)  # what status() shows
        self.assertIsNone(pub.wanted_base_topic)
        # corrected: the next reconnect (a save of the settings, Reconnect) reads it again and connects under it
        mp.write_json(path, {**LEGACY, "broker": {**BROKER, "port": self.port}})
        self.moving(pub)
        pub._connected, pub._live_base, pub._live_prefix = False, None, None
        await pub._async_reconnect_locked()
        self.assertIsNone(ident.record_problem)
        pub._connect.assert_called_once()
        self.assertEqual(pub.wanted_base_topic, "hass_hri_probe")

    async def test_the_record_is_rewritten_only_when_it_changed(self):
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), {**LEGACY, "broker": {**BROKER, "port": self.port}})
        pub = self.pub_with(self.identity("garage"))
        with mock.patch.object(inst_mod, "write_json", wraps=inst_mod.write_json) as write:
            for _ in range(3):
                await pub.hass.async_add_executor_job(pub._remember_identity, pub.wanted_base_topic, "homeassistant")
        self.assertEqual(write.call_count, 1)
        self.assertIs(write.call_args.kwargs["fsync"], True)

    async def test_an_invalid_instance_never_connects(self):
        """A fresh volume: the identity it would take is unknown."""
        pub = self.pub_with(self.identity("-bad"), force_base_topic=True)
        pub._client, pub._connected, pub.stats = None, False, {}
        with mock.patch.object(mp.mqtt, "Client") as client, mock.patch.object(mp.MqttPublisher, "_sweep_old_identity") as sweep:
            await pub.hass.async_add_executor_job(pub._connect)
        client.assert_not_called()
        sweep.assert_not_called()
        self.assertEqual(pub.stats["connect_error"], f"not connecting: {pub._identity.blocking()}")
        self.assertIsNone(pub._live_base)

    async def test_an_invalid_instance_does_not_stop_a_remembered_identity(self):
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), LEGACY)
        pub = self.pub_with(self.identity("-bad"), force_base_topic=True)
        pub._client, pub._connected, pub.stats, pub._stopping = None, False, {}, False
        pub._cleanup_pending = {}
        with mock.patch.object(mp.MqttPublisher, "_new_client") as new_client, \
                mock.patch.object(mp.MqttPublisher, "_sweep_old_identity", return_value=True) as sweep:
            await pub.hass.async_add_executor_job(pub._connect)
        new_client.assert_called_once_with("hass_hri_probe")
        sweep.assert_called_once_with("hass_hri_probe")
        self.assertEqual(pub.stats["connect_error"], "")
        self.assertIn("not used", pub._identity.describe()["identity_warning"])

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
        self.assertEqual(await pub.async_move_identity("hass_hri_probe-other"),
                         {"ok": False, "error": "the identity to move to is hass_hri_probe-garage now, not hass_hri_probe-other: reload the page"})
        pub._connected = False
        res = await pub.async_move_identity("hass_hri_probe-garage")
        self.assertFalse(res["ok"])
        self.assertIn("not connected", res["error"])
        self.assertEqual((cleared, ident.key("hri_probe")), ([], "hass_hri_probe"))  # a refusal changes nothing
        pub._connected = True
        res = await pub.async_move_identity("hass_hri_probe-garage")
        self.assertEqual(res, {"ok": True, "from": "hass_hri_probe", "to": "hass_hri_probe-garage"})
        self.assertEqual(cleared, [("hass_hri_probe", "homeassistant", True)])  # documents and discovery configs of the old names
        self.assertEqual({k: v for k, v in self.record().items() if k != "broker"},
                         {"base": "hass_hri_probe-garage", "prefix": "homeassistant", "domain": "hri_probe", "pinned": False})
        self.assertEqual(self.identity("garage").key("hri_probe"), "hass_hri_probe-garage")
        self.assertTrue(ident.pin("hass_hri_probe-garage"))  # its first connection held
        self.assertEqual(self.identity(None).key("hri_probe"), "hass_hri_probe-garage")
        self.assertEqual(await pub.async_move_identity("hass_hri_probe-garage"),
                         {"ok": False, "error": "nothing to move: the identity is the one the rule gives"})

    async def test_a_failed_move_sweep_is_retried_from_the_record(self):
        """The broker did not take the clear: the record keeps the old names (released), so the next connect sweeps them."""
        mp.write_json(os.path.join(self.dir, "integration_manager", "mqtt_identity.json"), {**LEGACY, "broker": {**BROKER, "port": self.port}})
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        self.moving(pub)
        pub._clear_retained_under.side_effect = lambda *a, **k: None
        self.assertTrue((await pub.async_move_identity("hass_hri_probe-garage"))["ok"])
        self.assertEqual((self.record()["base"], self.record()["released"]), ("hass_hri_probe", True))
        self.assertEqual(self.identity("garage").key("hri_probe"), "hass_hri_probe-garage")

    async def test_status_says_where_the_base_topic_comes_from(self):
        ident = self.identity("garage")
        pub = self.pub_with(ident)
        self.assertEqual(pub.public_config()["identity_source"], "instance")
        self.assertEqual(pub.public_config()["base_topic"], "hass_hri_probe-garage")


if __name__ == "__main__":
    unittest.main()
