"""End-to-end campaign against a real main Home Assistant, discovery side: what the main HA's MQTT platforms make of
the components and documents this container publishes, and what the commands they send turn into here.

The consuming side is the MQTT platform code of the Home Assistant in this venv, not a copy of its rules: each
component goes through the platform's DISCOVERY_SCHEMA, becomes an instance of the platform's entity class, and gets
the entity document on the topics it subscribes to; commands are what that entity publishes."""

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
    async def asyncSetUp(self):
        self.hass = core.HomeAssistant(tempfile.mkdtemp())
        loader.async_setup(self.hass)

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
