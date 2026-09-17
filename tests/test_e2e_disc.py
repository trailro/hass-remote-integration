"""End-to-end campaign against a real main Home Assistant, discovery side: what the main HA's MQTT platforms make of
the components and documents this container publishes, and what the commands they send turn into here.

The consuming side is the MQTT platform code of the Home Assistant in this venv, not a copy of its rules: each
component goes through the platform's DISCOVERY_SCHEMA, becomes an instance of the platform's entity class, and gets
the entity document on the topics it subscribes to; commands are what that entity publishes."""

import asyncio
import importlib
import json
import logging
import tempfile
import unittest
from unittest import mock

from homeassistant import core, loader
from homeassistant.core import State

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp

BASE = "hass_demo"
_CLASSES = {  # platform -> (module, entity class, schema)
    "light": ("light.schema_basic", "MqttLight", "DISCOVERY_SCHEMA_BASIC"),
    "cover": ("cover", "MqttCover", "DISCOVERY_SCHEMA"),
    "fan": ("fan", "MqttFan", "DISCOVERY_SCHEMA"),
    "update": ("update", "MqttUpdate", "DISCOVERY_SCHEMA"),
    "vacuum": ("vacuum", "MqttStateVacuum", "DISCOVERY_SCHEMA"),
    "water_heater": ("water_heater", "MqttWaterHeater", "DISCOVERY_SCHEMA"),
    "text": ("text", "MqttTextEntity", "DISCOVERY_SCHEMA"),
    "lawn_mower": ("lawn_mower", "MqttLawnMower", "DISCOVERY_SCHEMA"),
}


def document(state: State) -> dict:
    """The entity document MqttPublisher publishes for a state (an entity without a registry entry)."""
    pub = object.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True)
    pub._live_base, pub._live_prefix, pub._key_provider = BASE, None, lambda: BASE
    pub.rules = mock.Mock()
    pub.rules.for_entity.return_value = {}
    pub.hass = mock.Mock()
    pub.hass.data = {}
    registry = mock.Mock()
    registry.async_get.return_value = None
    with mock.patch.object(mp.er, "async_get", return_value=registry):
        return pub.build_document(state)[1]


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class Consumer:
    """One entity of the main HA built from this container's discovery component."""

    def __init__(self, hass, state: State):
        from homeassistant.components.mqtt.models import MqttValueTemplateException

        self._template_error = MqttValueTemplateException
        domain = state.entity_id.split(".")[0]
        self.doc_topic = f"{BASE}/demo/{domain}/{state.entity_id.split('.', 1)[1]}"
        registry = mock.Mock()
        registry.async_get.return_value = None
        with mock.patch.object(disc.er, "async_get", return_value=registry):
            self.component = disc.build_component(hass, state, self.doc_topic, f"{BASE}/cmd", f"{BASE}_")
        module, cls_name, schema = _CLASSES[self.component["platform"]]
        mod = importlib.import_module(f"homeassistant.components.mqtt.{module}")
        payload = {k: v for k, v in self.component.items() if k != "platform"}
        payload.update({"device": {"identifiers": [f"{BASE}_x"], "name": "t"}, "origin": disc.ORIGIN})
        self.config = getattr(mod, schema)(payload)
        cls = getattr(mod, cls_name)
        ent = cls.__new__(cls)
        ent.hass, ent._config, ent.entity_id, ent._subscriptions = hass, self.config, state.entity_id, {}
        if cls_name == "MqttStateVacuum":
            ent._state_attrs = {}
        ent._setup_from_config(self.config)
        ent._prepare_subscribe_topics()
        self.entity = ent
        self.published: list[tuple[str, str]] = []

        async def publish(topic, payload, *args, **kwargs):
            self.published.append((topic, payload))

        ent.async_publish_with_config = publish
        ent.async_write_ha_state = lambda: None
        if hasattr(ent, "_publish"):  # climate / water heater publish through a config key
            async def publish_key(key, payload):
                self.published.append((self.config[key], payload))

            ent._publish = publish_key

    def receive(self, state: State) -> list[logging.LogRecord]:
        """Feed the entity document to every subscription on its topic, the way the MQTT client runs a callback;
        returns the warnings and errors logged (an exception counts as an error, as the client logs it)."""
        from homeassistant.components.mqtt.models import ReceiveMessage

        logs = _Logs()
        root = logging.getLogger("homeassistant")
        root.addHandler(logs)
        try:
            payload = json.dumps(document(state))
            for sub in self.entity._subscriptions.values():
                if sub["topic"] != self.doc_topic:
                    continue
                callback = sub["msg_callback"].args[0]
                try:
                    callback(ReceiveMessage(sub["topic"], payload, 0, False, sub["topic"], 0.0))
                except self._template_error as err:
                    logs.records.append(logging.makeLogRecord({"levelno": logging.WARNING, "msg": str(err)}))
                except Exception as err:  # noqa: BLE001 - what the MQTT client would log as an error
                    logs.records.append(logging.makeLogRecord({"levelno": logging.ERROR, "msg": f"{type(err).__name__}: {err}"}))
        finally:
            root.removeHandler(logs)
        return logs.records

    def commands(self) -> list[tuple[str, str, dict]]:
        """What the published commands become in this container."""
        prefix = f"{BASE}/cmd/"
        out = []
        for topic, payload in self.published:
            domain, object_id, field = topic[len(prefix):].split("/")
            out.append(disc.command_to_service(domain, object_id, field, str(payload)))
        return out


class _HassCase(unittest.IsolatedAsyncioTestCase):
    SERVICES: tuple[str, ...] = ()  # entity components set up in the container's HA, for their service schemas

    async def asyncSetUp(self):
        self.hass = core.HomeAssistant(tempfile.mkdtemp())
        loader.async_setup(self.hass)
        if self.SERVICES:
            asyncio.get_running_loop().slow_callback_duration = 10  # loading the components is slow, not a blocked loop
            from homeassistant import bootstrap, config_entries
            from homeassistant.setup import async_setup_component

            self.hass.config_entries = config_entries.ConfigEntries(self.hass, {})
            quiet = logging.getLogger("homeassistant.loader")  # "custom integration integration_manager": this checkout
            level, quiet.level = quiet.level, logging.ERROR
            try:
                await bootstrap.async_load_base_functionality(self.hass)
                for domain in self.SERVICES:
                    self.assertTrue(await async_setup_component(self.hass, domain, {}))
            finally:
                quiet.setLevel(level)

    async def run_here(self, consumer) -> list[tuple[str, str, dict]]:
        """Every command the main HA published, mapped here and validated against the service's own schema."""
        out = consumer.commands()
        services = self.hass.services.async_services_internal()
        for domain, service, data in out:
            services[domain][service].schema(data)
        consumer.published.clear()
        return out

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    def assertQuiet(self, consumer, state):
        records = consumer.receive(state)
        self.assertEqual([r.getMessage() for r in records], [], state)


class NoValueTemplatesTest(_HassCase):
    """A light that is off, a cover without a position, an update without a release URL: no traceback or warning
    on the main HA, and nothing made up."""

    async def test_light_off_brightness_and_colour_temperature(self):
        on = State("light.l", "on", {"supported_color_modes": ["color_temp"], "brightness": 128, "color_temp_kelvin": 3000})
        consumer = Consumer(self.hass, on)
        self.assertQuiet(consumer, on)
        self.assertEqual((consumer.entity.brightness, consumer.entity.color_temp_kelvin), (128, 3000))
        for st in ("off", "unknown", "unavailable"):
            with self.subTest(state=st):
                self.assertQuiet(consumer, State("light.l", st, {"supported_color_modes": ["color_temp"], "brightness": None,
                                                                  "color_temp_kelvin": None}))
                self.assertQuiet(consumer, State("light.l", st, {}))

    async def test_cover_without_position_or_tilt(self):
        attrs = {"current_position": 40, "current_tilt_position": 20, "supported_features": 255}
        consumer = Consumer(self.hass, State("cover.c", "open", attrs))
        self.assertQuiet(consumer, State("cover.c", "open", attrs))
        self.assertEqual((consumer.entity.current_cover_position, consumer.entity.current_cover_tilt_position), (40, 20))
        for st in ("unknown", "unavailable"):
            with self.subTest(state=st):
                self.assertQuiet(consumer, State("cover.c", st, {}))
                self.assertQuiet(consumer, State("cover.c", st, {"current_position": None, "current_tilt_position": None}))
        self.assertQuiet(consumer, State("cover.c", "closed", {"current_position": 0, "current_tilt_position": 0}))
        self.assertEqual((consumer.entity.current_cover_position, consumer.entity.current_cover_tilt_position), (0, 0))

    async def test_update_fields_without_a_value(self):
        attrs = {"installed_version": "1.0", "latest_version": "1.1", "title": "Firmware", "release_url": None,
                 "release_summary": None, "in_progress": False}
        consumer = Consumer(self.hass, State("update.u", "on", attrs))
        self.assertQuiet(consumer, State("update.u", "on", attrs))
        self.assertEqual((consumer.entity.installed_version, consumer.entity.latest_version, consumer.entity.title),
                         ("1.0", "1.1", "Firmware"))
        for st in ("unknown", "unavailable"):
            with self.subTest(state=st):
                self.assertQuiet(consumer, State("update.u", st, {}))
                self.assertQuiet(consumer, State("update.u", st, {k: None for k in attrs}))
        self.assertEqual(consumer.entity.installed_version, "1.0")


class VacuumStateTest(_HassCase):
    """MQTT vacuum has no value template and reads the document's top level: the fan speed was always 0 there."""

    ATTRS = {"fan_speed_list": ["quiet", "standard", "turbo"], "fan_speed": "standard", "battery_level": 80,
             "supported_features": 14140}

    async def test_state_and_fan_speed_reach_the_main_ha(self):
        docked = State("vacuum.bot", "docked", self.ATTRS)
        consumer = Consumer(self.hass, docked)
        self.assertQuiet(consumer, docked)
        self.assertEqual((consumer.entity.activity, consumer.entity.fan_speed), ("docked", "standard"))
        self.assertQuiet(consumer, State("vacuum.bot", "cleaning", {**self.ATTRS, "fan_speed": "turbo"}))
        self.assertEqual((consumer.entity.activity, consumer.entity.fan_speed), ("cleaning", "turbo"))
        self.assertQuiet(consumer, State("vacuum.bot", "cleaning", {k: v for k, v in self.ATTRS.items() if k != "fan_speed"}))
        self.assertIsNone(consumer.entity.fan_speed)  # gone at the source: not the last speed

    async def test_document_keeps_the_attributes(self):
        doc = document(State("vacuum.bot", "docked", self.ATTRS))
        self.assertEqual(doc["attributes"], self.ATTRS)
        self.assertEqual((doc["state"], doc["fan_speed"]), ("docked", "standard"))
        self.assertNotIn("fan_speed", document(State("fan.f", "on", {"fan_speed": "x"})))

    async def test_battery_level_stays_an_attribute(self):
        from homeassistant.components.mqtt.vacuum import MQTT_VACUUM_ATTRIBUTES_BLOCKED

        self.assertNotIn("battery_level", MQTT_VACUUM_ATTRIBUTES_BLOCKED)
        self.assertEqual(Consumer(self.hass, State("vacuum.bot", "docked", self.ATTRS)).component["json_attributes_topic"],
                         f"{BASE}/demo/vacuum/bot")

    async def test_features_follow_the_source(self):
        consumer = Consumer(self.hass, State("vacuum.bot", "docked", self.ATTRS))
        self.assertEqual(int(consumer.entity.supported_features), 14140)  # the source's; STATE (4096) is always there
        basic = Consumer(self.hass, State("vacuum.bot", "docked", {"supported_features": 4096 | 8192 | 16}))
        self.assertEqual(int(basic.entity.supported_features), 4096 | 8192 | 16)
        legacy = Consumer(self.hass, State("vacuum.bot", "docked", {}))  # no features known (a registry entry): all of them
        self.assertEqual(legacy.component["supported_features"], list(disc._VACUUM_FEATURES))


class VacuumCommandsTest(_HassCase):
    SERVICES = ("vacuum",)

    def consumer(self):
        return Consumer(self.hass, State("vacuum.bot", "docked", VacuumStateTest.ATTRS))

    async def test_send_command_with_params_from_the_main_ha(self):
        consumer = self.consumer()
        await consumer.entity.async_send_command("clean_room", params={"room": "kitchen", "repeat": 2})
        self.assertEqual(await self.run_here(consumer), [
            ("vacuum", "send_command", {"entity_id": "vacuum.bot", "command": "clean_room", "params": {"room": "kitchen", "repeat": 2}})])
        await consumer.entity.async_send_command("beep")
        self.assertEqual(await self.run_here(consumer), [("vacuum", "send_command", {"entity_id": "vacuum.bot", "command": "beep"})])
        await consumer.entity.async_send_command("go", params={"params": {"a": 1}})  # a parameter named params stays one
        self.assertEqual((await self.run_here(consumer))[0][2]["params"], {"params": {"a": 1}})

    async def test_send_command_params_cannot_retarget(self):
        consumer = self.consumer()
        await consumer.entity.async_send_command("go", params={"a": 1, "entity_id": "vacuum.other", "area_id": "kitchen"})
        self.assertEqual(await self.run_here(consumer), [
            ("vacuum", "send_command", {"entity_id": "vacuum.bot", "command": "go", "params": {"a": 1}})])

    async def test_json_without_a_command_is_refused(self):
        for payload in ('{"room": "kitchen"}', '{"command": 5}', '{"command": " "}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                disc.command_to_service("vacuum", "bot", "send_command", payload)

    async def test_actions_from_the_main_ha(self):
        consumer = self.consumer()
        for action in ("async_start", "async_pause", "async_stop", "async_return_to_base", "async_clean_spot", "async_locate"):
            await getattr(consumer.entity, action)()
        await consumer.entity.async_set_fan_speed("turbo")
        self.assertEqual([svc for _, svc, _ in await self.run_here(consumer)],
                         ["start", "pause", "stop", "return_to_base", "clean_spot", "locate", "set_fan_speed"])


class CommandTokenTest(unittest.TestCase):
    """Command tokens match in any case on every platform, and a wrong one says what is accepted."""

    def test_vacuum_and_lawn_mower_ignore_case(self):
        self.assertEqual(disc.command_to_service("vacuum", "bot", "command", "START")[1], "start")
        self.assertEqual(disc.command_to_service("vacuum", "bot", "command", " Return_To_Base ")[1], "return_to_base")
        self.assertEqual(disc.command_to_service("lawn_mower", "m", "command", "START_MOWING")[1], "start_mowing")
        self.assertEqual(disc.command_to_service("lawn_mower", "m", "command", "Dock")[1], "dock")

    def test_unknown_token_names_the_accepted_ones_not_the_payload(self):
        cases = {("vacuum", "command"): "start, pause, stop, return_to_base, clean_spot, locate",
                 ("lawn_mower", "command"): "start_mowing, pause, dock", ("cover", "command"): "open, close, stop",
                 ("valve", "command"): "open, close, stop", ("lock", "command"): "lock, unlock, open",
                 ("alarm_control_panel", "command"): "arm_home, arm_away"}
        for (domain, field), accepted in cases.items():
            with self.subTest(domain=domain), self.assertRaises(ValueError) as caught:
                disc.command_to_service(domain, "x", field, "DISARM CODE=1234 X")
            self.assertIn(f"expected one of: {accepted}", str(caught.exception))
            self.assertNotIn("1234", str(caught.exception))


class CoverFeaturesTest(_HassCase):
    SERVICES = ("cover",)

    async def test_tilt_only_cover(self):
        tilt = State("cover.slats", "closed", {"current_tilt_position": 0, "supported_features": 240})
        consumer = Consumer(self.hass, tilt)
        self.assertEqual(int(consumer.entity.supported_features), 240)  # was 251: open, close and stop failed here
        self.assertNotIn("command_topic", consumer.component)
        self.assertQuiet(consumer, tilt)
        for action in ("async_stop_cover_tilt", "async_open_cover_tilt", "async_close_cover_tilt"):
            await getattr(consumer.entity, action)()
        await consumer.entity.async_set_cover_tilt_position(tilt_position=40)
        self.assertEqual(await self.run_here(consumer), [
            ("cover", "stop_cover_tilt", {"entity_id": "cover.slats"}),
            ("cover", "set_cover_tilt_position", {"entity_id": "cover.slats", "tilt_position": 100}),
            ("cover", "set_cover_tilt_position", {"entity_id": "cover.slats", "tilt_position": 0}),
            ("cover", "set_cover_tilt_position", {"entity_id": "cover.slats", "tilt_position": 40})])

    async def test_stop_tilt_of_a_full_cover(self):
        full = State("cover.c", "open", {"current_position": 50, "current_tilt_position": 10, "supported_features": 255})
        consumer = Consumer(self.hass, full)
        self.assertEqual(int(consumer.entity.supported_features), 255)
        await consumer.entity.async_stop_cover_tilt()
        await consumer.entity.async_stop_cover()
        await consumer.entity.async_set_cover_position(position=30)
        self.assertEqual([svc for _, svc, _ in await self.run_here(consumer)], ["stop_cover_tilt", "stop_cover", "set_cover_position"])

    async def test_features_follow_the_source(self):
        for features, attrs in ((1 | 2, {}), (1 | 2 | 8, {}), (1 | 2 | 4, {"current_position": 0}),
                                (1 | 2 | 8 | 4, {"current_position": 0, "current_tilt_position": 0}), (0, {})):
            with self.subTest(features=features):
                consumer = Consumer(self.hass, State("cover.c", "closed", {**attrs, "supported_features": features}))
                self.assertEqual(int(consumer.entity.supported_features), features)
        legacy = Consumer(self.hass, State("cover.c", "closed", {"current_position": 0, "current_tilt_position": 0}))
        self.assertEqual(int(legacy.entity.supported_features), 255)  # features unknown: everything, as before

    async def test_registry_entry_features(self):
        from types import SimpleNamespace

        entry = SimpleNamespace(entity_id="cover.c", capabilities=None, unit_of_measurement=None, disabled=True,
                                supported_features=1 | 2, name=None, original_name="C", icon=None, original_icon=None,
                                entity_category=None, device_class=None, original_device_class=None)
        registry = mock.Mock()
        registry.async_get.return_value = entry
        with mock.patch.object(disc.er, "async_get", return_value=registry):
            comp = disc.build_component_from_entry(self.hass, entry, f"{BASE}/demo/cover/c", f"{BASE}/cmd", f"{BASE}_")
        self.assertEqual((comp["payload_open"], comp["payload_close"], comp["payload_stop"]), ("OPEN", "CLOSE", None))
