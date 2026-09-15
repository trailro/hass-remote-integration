"""Fourth review, MQTT side: HRI_CALL_TIMEOUT, masked secret keys, unknown on/off states, disabled entities, a stop
during the first connect, identity sweeps while the broker is unreachable, call memory and concurrency, TLS."""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State, SupportsResponse
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r3_mqtt import BASE, _message, _publisher


class CallTimeoutEnvTest(unittest.TestCase):
    """HRI-04."""

    NAME = "custom_components.integration_manager._r4_timeout_probe"  # the fresh copy logs under its own module name

    def _import(self, value):
        spec = importlib.util.spec_from_file_location(self.NAME, mp.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"HRI_CALL_TIMEOUT": value}), mock.patch.dict(sys.modules, {self.NAME: module}):
            spec.loader.exec_module(module)  # a fresh import: the constant is read at import time
        return module.CALL_TIMEOUT_S

    def test_invalid_value_imports_with_the_default(self):
        with self.assertLogs(self.NAME, "WARNING"):
            self.assertEqual(self._import("abc"), 60)
        with self.assertLogs(self.NAME, "WARNING"):
            self.assertEqual(self._import("60s"), 60)

    def test_below_one_is_clamped(self):
        for value in ("0", "-5"):
            with self.subTest(value=value), self.assertLogs(self.NAME, "WARNING"):
                self.assertEqual(self._import(value), 1)

    def test_valid_value(self):
        self.assertEqual(self._import(" 90 "), 90)


class MaskedKeySuffixTest(unittest.TestCase):
    """HRI-06: keys that end in a secret word are masked too."""

    def test_suffixed_secret_keys_are_masked(self):
        for key in ("access_token", "api_token", "auth_token", "authtoken", "api_key", "apikey", "api-key", "client_secret",
                    "old_password", "user_pin", "alarm_code", "usercode", "passkey", "bindkey"):
            with self.subTest(key=key):
                self.assertNotIn("s3cr3t", mp._mask_codes(json.dumps({"entity_id": "lock.a", key: "s3cr3t"})))
                self.assertNotIn("4321", mp._mask_codes(f"x {{'{key}': 4321}}"))
        self.assertEqual(mp._mask_codes('{"access_token": "x"}'), '{"access_token": "***"}')
        self.assertEqual(mp._mask_codes('{"api_key": "y"}'), '{"api_key": "***"}')

    def test_non_secret_keys_stay(self):
        for kept in ('{"translation_key": "x"}', '{"sort_key": "a"}', '{"primary_key": "id"}', '{"token_type": "bearer"}',
                     '{"code_format": "number"}', '{"pincode_length": 4}', '{"hotkey": "F1"}', '{"monkey": 1}',
                     '{"zipcode": "12345"}', '{"spin": 3}', '{"key_count": 2}'):
            with self.subTest(kept=kept):
                self.assertEqual(mp._mask_codes(kept), kept)


class UnknownOnOffStateTest(unittest.IsolatedAsyncioTestCase):
    """HRI-12: unknown/unavailable render 'None' (unknown on the consumer), not OFF."""

    async def test_on_off_platforms_render_none_for_unknown(self):
        from homeassistant import core, loader
        from homeassistant.helpers.template import Template

        hass = core.HomeAssistant(tempfile.mkdtemp())
        loader.async_setup(hass)
        registry = mock.Mock()
        registry.async_get.return_value = None
        try:
            for domain in ("light", "fan", "siren", "humidifier"):
                with mock.patch.object(disc.er, "async_get", return_value=registry):
                    comp = disc.build_component(hass, State(f"{domain}.x", "on", {}), f"b/i/{domain}/x", "b/cmd", "b_")
                template = Template(comp["state_value_template"], hass)
                for source, expected in (("unknown", "None"), ("unavailable", "None"), ("on", "ON"), ("off", "OFF")):
                    with self.subTest(domain=domain, source=source):
                        out = template.async_render_with_possible_json_value(json.dumps({"state": source}))
                        self.assertEqual(out, expected)
        finally:
            await hass.async_stop(force=True)


def _entry(entity_id="sensor.a", disabled_by=None):
    return SimpleNamespace(entity_id=entity_id, domain=entity_id.split(".")[0], platform="demo", device_id=None,
                           disabled_by=disabled_by, disabled=disabled_by is not None, capabilities=None, unit_of_measurement=None,
                           name=None, original_name="A", icon=None, original_icon=None, entity_category=None, device_class=None,
                           original_device_class=None, unique_id="a", hidden=False, area_id=None, labels=set(),
                           config_entry_id="e1", translation_key=None)


class DisabledEntityTest(unittest.TestCase):
    """HRI-13: disabling keeps the entity announced (enabled_by_default false) instead of removing it and having the
    next full republish create it again."""

    def setUp(self):
        pub = object.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(enabled=True, discovery_enabled=True)
        pub._connected, pub._moving = True, False
        pub._live_base, pub._live_prefix, pub._key_provider = BASE, None, lambda: BASE
        pub.rules = mock.Mock()
        pub.rules.for_entity.return_value = {}
        pub.rules.apply_component.side_effect = lambda comp, rule: comp
        pub._collision_warned, pub._default_id_warned = set(), set()
        pub.hass = mock.Mock()
        pub.hass.data = {}
        pub.hass.states.async_all.return_value = []
        pub.hass.states.get.return_value = None
        pub.hass.config.components = {"demo"}
        pub._last_hash, pub._blocks, pub.stats, pub._health_last = {}, {}, {"unchanged_skipped": 0, "published": 0, "cleared": 0}, {}
        pub.manager, pub._orphan_sweep_due, pub._boot_components, pub._registry_timer = None, False, None, None
        pub._topics = {"sensor.a": f"{BASE}/demo/sensor/a"}
        did, block = disc.device_block(pub.hass, None, "demo", pub.prefix)
        pub._discovery_map = {did: {"sensor.a": {"platform": "sensor", "unique_id": f"{BASE}_sensor.a"}}}
        pub._blocks[did] = block
        self.did = did
        self.published = []
        pub._publish = lambda topic, payload, retain=True, qos=None: self.published.append((topic, payload)) or True
        self.pub = pub
        self.entry = _entry(disabled_by=er.RegistryEntryDisabler.USER)
        self.registry = mock.Mock(entities={"sensor.a": self.entry})
        self.registry.async_get.side_effect = lambda eid: self.entry if eid == "sensor.a" else None

    def test_disable_then_full_republish_keeps_the_entity(self):
        with mock.patch.object(er, "async_get", return_value=self.registry):
            self.pub._on_registry(SimpleNamespace(data={"action": "update", "entity_id": "sensor.a", "changes": {"disabled_by": None}}))
            self.pub._on_state(SimpleNamespace(data={"new_state": None, "old_state": State("sensor.a", "21", {"unit_of_measurement": "°C"})}))
            self.pub._publish_discovery_all()
        doc_payloads = [p for t, p in self.published if t == f"{BASE}/demo/sensor/a"]
        self.assertTrue(doc_payloads)
        self.assertNotIn(None, doc_payloads)  # never cleared: the consumer would keep showing the last value
        self.assertTrue(all(json.loads(p)["state"] == "unavailable" for p in doc_payloads))
        configs = [json.loads(p) for t, p in self.published if t == self.pub._discovery_topic(self.did)]
        self.assertEqual(len(configs), 1)
        comp = configs[0]["components"]["sensor_a"]
        self.assertEqual(comp["unique_id"], f"{BASE}_sensor.a")  # a full component, not a removal form
        self.assertIs(comp["enabled_by_default"], False)

    def test_config_entry_disable_still_clears_nothing_on_the_registry_event(self):
        self.entry.disabled_by = er.RegistryEntryDisabler.CONFIG_ENTRY
        with mock.patch.object(er, "async_get", return_value=self.registry):
            self.pub._on_registry(SimpleNamespace(data={"action": "update", "entity_id": "sensor.a", "changes": {"disabled_by": None}}))
        self.assertEqual(self.published, [])

    def test_deleting_still_removes(self):
        with mock.patch.object(er, "async_get", return_value=self.registry):
            self.pub._on_registry(SimpleNamespace(data={"action": "remove", "entity_id": "sensor.a"}))
        config = json.loads(next(p for t, p in self.published if t == self.pub._discovery_topic(self.did)))
        self.assertEqual(config["components"]["sensor_a"], {"platform": "sensor"})
        self.assertIn((f"{BASE}/demo/sensor/a", None), self.published)


def _connect_publisher(loop=None):
    pub = object.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig(enabled=True, force_base_topic=True)
    pub._key_provider = lambda: "hass_demo"
    pub._live_base = pub._live_prefix = None
    pub._client, pub._connected, pub._probed_ok = None, False, set()
    pub._tls_checked_at, pub._tls_error, pub._last_disconnect = 0.0, "", ""
    pub.stats, pub._last_hash = {}, {}
    pub._conn_lock = asyncio.Lock() if loop else None
    if loop:
        pub.hass = SimpleNamespace(loop=loop, async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a),
                                   config=SimpleNamespace(path=lambda *p: "/nonexistent/" + "/".join(p)))
    else:
        pub.hass = mock.Mock()
    return pub


class StopDuringFirstConnectTest(unittest.IsolatedAsyncioTestCase):
    """HRI-16."""

    async def test_stop_while_the_connect_probes_creates_no_client(self):
        pub = _connect_publisher(asyncio.get_running_loop())

        def slow_read(*_a, **_k):
            time.sleep(0.4)  # still sweeping when Home Assistant stops
            return {}

        with mock.patch.object(mp, "read_json", side_effect=slow_read), mock.patch.object(mp.MqttPublisher, "_remember_identity"), \
                mock.patch.object(mp.mqtt, "Client") as client_cls:
            connect = asyncio.create_task(pub._async_first_connect())
            await asyncio.sleep(0.1)
            await pub._on_stop(None)
            await connect
        self.assertIsNone(pub._client)
        client_cls.return_value.connect_async.assert_not_called()

    def test_stop_right_after_the_check_disconnects_the_new_client(self):
        pub = _connect_publisher()
        with mock.patch.object(mp, "read_json", return_value={}), mock.patch.object(mp.MqttPublisher, "_remember_identity"), \
                mock.patch.object(mp.mqtt, "Client") as client_cls:
            client_cls.return_value.loop_start.side_effect = lambda: setattr(pub, "_stopping", True)
            pub._connect()
        self.assertIsNone(pub._client)
        client_cls.return_value.loop_stop.assert_called()

    def test_connack_while_stopping_publishes_nothing(self):
        pub = _connect_publisher()
        pub._stopping = True
        client = mock.Mock()
        pub._on_connect(client, None, None, 0)
        client.publish.assert_not_called()
        pub.hass.loop.call_soon_threadsafe.assert_not_called()


class IdentitySweepRetryTest(unittest.IsolatedAsyncioTestCase):
    """HRI-17."""

    def test_failed_sweep_at_connect_is_marked_due(self):
        pub = _connect_publisher()
        with mock.patch.object(mp, "read_json", return_value={"base": "hass_old", "prefix": "homeassistant"}), \
                mock.patch.object(mp.MqttPublisher, "_clear_retained_under", return_value=None), \
                mock.patch.object(mp.MqttPublisher, "_remember_identity") as remember, mock.patch.object(mp.mqtt, "Client"):
            pub._connect()
        self.assertTrue(pub._identity_sweep_due)
        remember.assert_not_called()  # the old names stay recorded for the retry

    async def test_full_republish_after_the_connect_retries_it(self):
        pub = object.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(enabled=True)
        pub._connected, pub._moving, pub._pending_clears = True, False, set()
        pub._live_base, pub._key_provider = "hass_new", lambda: "hass_new"
        pub.stats = {"services_published": 0, "discovery_devices": 0, "discovery_components": 0}
        pub._orphan_sweep_due, pub._resync_excluded, pub._undiscover_due = False, False, False
        pub.hass = mock.Mock()
        pub.hass.states.async_all.return_value = []

        async def executor(fn, *args):
            return fn(*args)

        pub.hass.async_add_executor_job = executor
        pub.publish_health = pub._publish_manager_discovery = pub.publish_manager = lambda: None
        pub._publish_services = mock.AsyncMock()
        pub._identity_sweep_due = True
        with mock.patch.object(mp, "read_json", return_value={"base": "hass_old", "prefix": "homeassistant"}), \
                mock.patch.object(mp.MqttPublisher, "_clear_retained_under", side_effect=[None, 3]) as sweep, \
                mock.patch.object(mp.MqttPublisher, "_remember_identity") as remember:
            await pub.async_republish_all()
            self.assertTrue(pub._identity_sweep_due)  # still unreachable: due again
            await pub.async_republish_all()
        self.assertFalse(pub._identity_sweep_due)
        self.assertEqual(sweep.call_args.args[:2], ("hass_old", "homeassistant"))
        remember.assert_called_once_with("hass_new", "homeassistant")

    async def test_prefix_change_while_disconnected_is_left_to_the_connect(self):
        pub = object.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig(enabled=True, discovery_prefix="homeassistant")
        pub._connected, pub._live_base, pub._key_provider = False, None, lambda: "hass_demo"
        pub._last_wanted, pub._topics, pub._registry_timer, pub._services_timer = "hass_demo", {}, None, None
        pub._republish_interval = pub.config.republish_interval_s
        pub.hass = mock.Mock()

        async def executor(fn, *args):
            return fn(*args)

        pub.hass.async_add_executor_job = executor
        new = mp.MqttConfig(enabled=False, discovery_prefix="ha2")
        pub._load = lambda: new
        pub._disconnect = lambda *a: None
        pub.publish_health = lambda: None
        with mock.patch.object(mp.MqttPublisher, "_remember_identity") as remember:
            await pub._async_reconnect_locked()
        remember.assert_not_called()  # recording the new prefix now would make the next connect skip the sweep of the old one


def _running_publisher():
    pub = _publisher()
    loop = asyncio.get_running_loop()
    pub.hass.loop.call_soon_threadsafe = lambda f: f()
    pub.hass.async_create_task = loop.create_task
    pub.hass.services.has_service = lambda d, s: True
    pub.hass.services.supports_response = lambda d, s: SupportsResponse.NONE
    pub._call_target_problem = lambda data: None
    pub.release = asyncio.Event()

    async def service(*_a, **_k):
        await pub.release.wait()

    pub.hass.services.async_call = service
    return pub


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


class CallMemoryTest(unittest.TestCase):
    """HRI-18: remembered _ids are capped, slim and expired on every call."""

    def test_capped_and_slim(self):
        pub = _publisher()
        for i in range(mp.CALLS_REMEMBERED + 500):
            pub._on_call("light/turn_on", json.dumps({"_id": i, "entity_id": "light.a", "brightness": 3}))
        self.assertEqual(len(pub._calls), mp.CALLS_REMEMBERED)
        self.assertNotIn(mp._call_key("light", "turn_on", 0), pub._calls)  # the oldest went first
        self.assertIn(mp._call_key("light", "turn_on", mp.CALLS_REMEMBERED + 499), pub._calls)
        self.assertEqual(set(next(iter(pub._calls.values()))), {"received", "state", "result"})

    def test_expired_on_a_call_without_id(self):
        pub = _publisher()
        pub._calls = {"light.turn_on:1": {"received": time.time() - mp.DEDUP_WINDOW_S - 1, "state": "ok", "result": None}}
        pub._on_call("light/turn_on", "{}")
        self.assertEqual(pub._calls, {})


class InFlightCapTest(unittest.IsolatedAsyncioTestCase):
    """HRI-18: calls whose service has not returned are capped."""

    async def test_call_beyond_the_cap_is_answered_and_can_be_retried(self):
        pub = _running_publisher()
        with mock.patch.object(mp, "CALLS_IN_FLIGHT_MAX", 2):
            for i in (1, 2, 3):
                pub._on_call("light/turn_on", json.dumps({"_id": i}))
            await _settle()
            refused = [r for _d, _s, r in pub.results if r.get("ok") is False]
            self.assertEqual([r["id"] for r in refused], [3])
            self.assertIn("too many calls in progress", refused[0]["error"])
            self.assertNotIn(mp._call_key("light", "turn_on", 3), pub._calls)
            self.assertEqual(pub._in_flight, 2)
            pub.release.set()
            await _settle()
            self.assertEqual(pub._in_flight, 0)
            self.assertEqual(sorted(r["id"] for _d, _s, r in pub.results if r.get("ok")), [1, 2])
            pub._on_call("light/turn_on", json.dumps({"_id": 3}))  # the retry runs, it is no duplicate
            await _settle()
        self.assertEqual(sorted(r["id"] for _d, _s, r in pub.results if r.get("ok")), [1, 2, 3])
        self.assertFalse(any(r.get("duplicate") for _d, _s, r in pub.results))

    async def test_duplicate_is_answered_from_the_slim_record(self):
        pub = _running_publisher()
        pub.release.set()
        pub._on_call("light/turn_on", json.dumps({"_id": "a"}))
        await _settle()
        pub._on_call("light/turn_on", json.dumps({"_id": "a"}))
        self.assertEqual(pub.results[-1][2], {"id": "a", "service": "light.turn_on", "ok": True, "duplicate": True})

    async def test_command_beyond_the_cap_is_rejected(self):
        pub = _running_publisher()
        pub.stats.update(commands=0, last_command=None)
        pub._topics = {"switch.a": f"{BASE}/demo/switch/a"}
        pub._in_flight = mp.CALLS_IN_FLIGHT_MAX
        pub._handle_message(_message(f"{BASE}/cmd/switch/a/state", "ON"))
        await _settle()
        self.assertEqual(pub.history[-1]["state"], "rejected")
        self.assertIn("too many calls in progress", pub.history[-1]["error"])


class TlsConfigTest(unittest.TestCase):
    """HRI-19."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = self.tmp.name
        self.ca = os.path.join(self.config_dir, "mqtt-ca.pem")
        with open(self.ca, "w", encoding="utf-8") as fh:
            fh.write("x")
        pub = object.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig()
        pub._saved = {}
        pub.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.config_dir))
        pub.path = os.path.join(self.config_dir, "mqtt.json")
        self.pub = pub

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        cfg = self.pub._validated({"tls": True, "ca_certs": " mqtt-ca.pem ", "tls_insecure": True, "port": 8883})
        self.assertEqual((cfg.tls, cfg.ca_certs, cfg.tls_insecure), (True, self.ca, True))
        with open(self.pub.path, "w", encoding="utf-8") as fh:
            json.dump(asdict(cfg), fh)
        self.assertEqual(self.pub._load(), cfg)
        self.assertEqual(self.pub._validated({"ca_certs": ""}).ca_certs, "")  # empty: the system CAs
        self.assertEqual(mp.MqttConfig().tls, False)

    def test_invalid_values(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        os.symlink(outside.name, os.path.join(self.config_dir, "link.pem"))
        for updates in ({"tls": "yes"}, {"tls_insecure": 1}, {"ca_certs": 5}, {"ca_certs": outside.name},
                        {"ca_certs": "../" + os.path.basename(outside.name)}, {"ca_certs": "missing.pem"},
                        {"ca_certs": "link.pem"}, {"ca_certs": "."}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.pub._validated(updates)

    def test_tls_set_on_every_client(self):
        cases = ((mp.MqttConfig(), None, False),
                 (mp.MqttConfig(tls=True), {"ca_certs": None}, False),
                 (mp.MqttConfig(tls=True, ca_certs=self.ca, tls_insecure=True), {"ca_certs": self.ca}, True))
        for config, tls_args, insecure in cases:
            with self.subTest(config=config), mock.patch.object(mp.mqtt, "Client") as client_cls:
                self.pub.config = config
                self.pub._new_client("x")
                client = client_cls.return_value
                if tls_args is None:
                    client.tls_set.assert_not_called()
                else:
                    client.tls_set.assert_called_once_with(**tls_args)
                self.assertEqual(client.tls_insecure_set.call_args_list, [mock.call(True)] if insecure else [])

    def test_connection_scan_and_cleanup_use_it(self):
        pub = _connect_publisher()
        pub.config = mp.MqttConfig(enabled=True, force_base_topic=True, tls=True, ca_certs=self.ca)
        with mock.patch.object(mp, "read_json", return_value={}), mock.patch.object(mp.MqttPublisher, "_remember_identity"), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet"), mock.patch.object(mp.mqtt, "Client") as client_cls:
            client = client_cls.return_value
            client.connect.side_effect = lambda *a, **k: client.on_connect(client, None, None, 0)
            client.subscribe.side_effect = lambda topics: client.on_subscribe(client, None, 1, [0])
            client.publish.return_value.is_published.return_value = True
            pub._connect()
            pub._retained_scan("probe", [("x/#", 1)])
            pub._clear_topics("cleanup", ["x/y"])
        self.assertEqual(client.tls_set.call_args_list, [mock.call(ca_certs=self.ca)] * 3)

    def test_unreadable_ca_file_is_a_connect_error(self):
        pub = _connect_publisher()
        pub.config = mp.MqttConfig(enabled=True, force_base_topic=True, tls=True, ca_certs="/nonexistent.pem")
        with mock.patch.object(mp, "read_json", return_value={}), mock.patch.object(mp.MqttPublisher, "_remember_identity"), \
                self.assertLogs(mp._LOGGER, "ERROR"):
            pub._connect()
        self.assertIsNone(pub._client)
        self.assertTrue(pub.stats["connect_error"])


class TlsErrorReportTest(unittest.TestCase):
    """Found by the TLS end-to-end test against a real broker."""

    def _pub(self, **config):
        pub = _connect_publisher()
        pub.config = mp.MqttConfig(enabled=True, host="broker", port=8883, **config)
        return pub

    def test_changed_tls_settings_probe_again(self):
        pub = self._pub(tls=True, ca_certs="/config/a.pem")
        with mock.patch.object(mp, "read_json", return_value={}), mock.patch.object(mp.MqttPublisher, "_remember_identity"), \
                mock.patch.object(mp.MqttPublisher, "probe_foreign", return_value={}) as probe, mock.patch.object(mp.mqtt, "Client"):
            pub._connect()
            pub._connect()
            pub.config.ca_certs = "/config/b.pem"
            pub._connect()
        self.assertEqual(probe.call_count, 2)

    def test_certificate_error_reaches_the_status_once_a_minute(self):
        pub = self._pub(tls=True, ca_certs="/config/a.pem")
        ctx = mock.MagicMock()
        err = mp.ssl.SSLCertVerificationError(1, "certificate verify failed")
        err.verify_message = "unable to get local issuer certificate"
        ctx.wrap_socket.side_effect = err
        with mock.patch.object(mp.ssl, "create_default_context", return_value=ctx) as create, \
                mock.patch.object(mp.socket, "create_connection", return_value=mock.MagicMock()) as conn, \
                mock.patch.object(mp.events, "emit") as emit, self.assertLogs(mp._LOGGER, "WARNING") as logs:
            pub._on_connect_fail(None, None)
            pub._on_connect_fail(None, None)
        self.assertIn("unable to get local issuer certificate", pub.stats["connect_error"])
        create.assert_called_once_with(cafile="/config/a.pem")
        self.assertEqual(conn.call_count, 1)
        self.assertEqual((len(logs.output), emit.call_count), (1, 1))

    def test_tls_insecure_skips_only_the_host_name_check(self):
        pub = self._pub(tls=True, tls_insecure=True)
        ctx = mock.MagicMock()
        with mock.patch.object(mp.ssl, "create_default_context", return_value=ctx), \
                mock.patch.object(mp.socket, "create_connection", return_value=mock.MagicMock()):
            self.assertEqual(pub._tls_handshake_error(), "")
        self.assertFalse(ctx.check_hostname)

    def test_unreachable_broker_keeps_the_generic_message(self):
        pub = self._pub(tls=True)
        with mock.patch.object(mp.socket, "create_connection", side_effect=ConnectionRefusedError()):
            pub._on_connect_fail(None, None)
        self.assertEqual(pub.stats["connect_error"], "cannot reach the broker at broker:8883 (retrying)")

    def test_without_tls_no_handshake(self):
        pub = self._pub()
        with mock.patch.object(mp.socket, "create_connection") as conn:
            pub._on_connect_fail(None, None)
        conn.assert_not_called()
        self.assertIn("cannot reach the broker", pub.stats["connect_error"])

    def test_repeated_disconnect_logged_once_with_a_tls_hint(self):
        pub = self._pub()
        with mock.patch.object(mp.events, "emit") as emit, self.assertLogs(mp._LOGGER, "WARNING") as logs:
            for _ in range(3):
                pub._on_disconnect(None, None, None, "Unspecified error")
        self.assertEqual((len(logs.output), emit.call_count), (1, 1))
        self.assertIn("TLS listener", pub.stats["connect_error"])
        pub._on_connect(mock.Mock(), None, None, 0)
        with mock.patch.object(mp.events, "emit") as emit, self.assertLogs(mp._LOGGER, "WARNING"):
            pub._on_disconnect(None, None, None, "Unspecified error")  # after a real connection it is news again
        emit.assert_called_once()


class CleanupTest(unittest.TestCase):
    def test_host_and_prefix_stored_stripped(self):
        pub = object.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig()
        pub._saved = {}
        cfg = pub._validated({"host": "  broker  ", "discovery_prefix": " ha "})
        self.assertEqual((cfg.host, cfg.discovery_prefix), ("broker", "ha"))

    def test_group_set_fields_count(self):
        self.assertEqual(mp._entity_ids_in({"object_id": "g", "add_entities": ["light.a"], "remove_entities": "light.b, light.c"}),
                         {"light.a", "light.b", "light.c"})
        pub = _publisher()
        pub._topics = {"group.g": "t"}
        pub.hass.states.get = lambda eid: None
        self.assertIn("light.hidden", pub._call_target_problem({"object_id": "g", "add_entities": ["light.hidden"]}) or "")


if __name__ == "__main__":
    unittest.main()
