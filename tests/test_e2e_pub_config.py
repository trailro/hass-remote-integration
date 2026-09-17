"""End-to-end campaign on 0.17.0, publisher side: an mqtt.json that is JSON but not an object broke the setup of the
manager (a restart loop, /api/mqtt/reconnect answered 500), and an unreadable one fell back to the defaults without a
word."""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
        self.pub.path = os.path.join(self.tmp, "mqtt.json")

    def write(self, raw: bytes):
        with open(self.pub.path, "wb") as fh:
            fh.write(raw)


class UnusableConfigFileTest(_ConfigCase):
    def test_json_that_is_not_an_object_gives_the_defaults_with_a_warning(self):
        for raw in (b"[]", b"[1, 2]", b"\"text\"", b"5", b"null", b"true"):
            with self.subTest(raw=raw), mock.patch.object(mp.events, "emit") as emit:
                self.write(raw)
                with self.assertLogs(mp._LOGGER, "WARNING") as logs:
                    self.assertEqual(self.pub._load(), mp.MqttConfig())
                self.assertIn("not a JSON object", logs.output[0])
                emit.assert_called_once()
                self.assertEqual(emit.call_args.args[0], "mqtt")
                self.assertIn("default settings", emit.call_args.args[1])

    def test_unparseable_file_is_no_longer_silent(self):
        for raw in (b"{", b"", b"\xff\xfe{}", b"{\"enabled\": true,}"):
            with self.subTest(raw=raw), mock.patch.object(mp.events, "emit") as emit:
                self.write(raw)
                with self.assertLogs(mp._LOGGER, "WARNING") as logs:
                    self.assertEqual(self.pub._load(), mp.MqttConfig())
                self.assertIn("cannot be read", logs.output[0])
                emit.assert_called_once()

    def test_no_file_is_a_fresh_volume_and_says_nothing(self):
        with mock.patch.object(mp.events, "emit") as emit, self.assertNoLogs(mp._LOGGER, "WARNING"):
            self.assertEqual(self.pub._load(), mp.MqttConfig())
        emit.assert_not_called()

    def test_values_of_the_wrong_type_fall_back_one_by_one(self):
        self.write(json.dumps({"enabled": True, "host": None, "password": 1234, "discovery_enabled": "yes",
                               "exclude_integrations": "demo", "discovery_prefix": "ha", "port": 1884}).encode())
        with self.assertLogs(mp._LOGGER, "WARNING") as logs:
            config = self.pub._load()
        self.assertEqual((config.enabled, config.host, config.password, config.discovery_enabled), (True, "mosquitto", "", False))
        self.assertEqual((config.exclude_integrations, config.discovery_prefix, config.port), (["integration_manager"], "ha", 1884))
        self.assertFalse(any("1234" in line for line in logs.output))  # the password is never quoted

    def test_a_good_file_is_read_as_it_is(self):
        self.write(json.dumps({"enabled": True, "host": "broker", "exclude_integrations": ["x"], "unknown": 1}).encode())
        with self.assertNoLogs(mp._LOGGER, "WARNING"):
            config = self.pub._load()
        self.assertEqual((config.enabled, config.host, config.exclude_integrations), (True, "broker", ["x"]))


class ReconnectWithAnUnusableFileTest(unittest.IsolatedAsyncioTestCase):
    """/api/mqtt/reconnect answered 500: the reload of the file raised AttributeError."""

    async def test_reload_adopts_the_defaults(self):
        tmp = tempfile.mkdtemp()
        pub = mp.MqttPublisher.__new__(mp.MqttPublisher)
        pub.path = os.path.join(tmp, "mqtt.json")
        with open(pub.path, "w") as fh:
            fh.write("[]")
        pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True)
        pub._conn_lock = asyncio.Lock()
        pub.hass = SimpleNamespace(async_add_executor_job=lambda f, *a: _done(f(*a)))
        pub._drop_newly_excluded = lambda new: None
        pub._set_undiscover_due = lambda due: None
        with self.assertLogs(mp._LOGGER, "WARNING"):
            await pub.async_reload_config()
        self.assertEqual(pub.config, mp.MqttConfig())


async def _done(value):
    return value
