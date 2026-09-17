"""MQTT hardening from the second full review: call scope, deny list, NaN, nesting, dedup key, masked codes,
reload under the connection lock, excluded entities in the orphan sweep, action limits kept on disk."""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import mqtt_publisher as mp


class CommandParsingTest(unittest.TestCase):
    def test_nan_and_infinity_are_refused(self):
        for value in ("nan", "NaN", "inf", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                disc.command_to_service("number", "x", "value", value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                disc.command_to_service("climate", "x", "temperature", value)
        self.assertEqual(disc.command_to_service("number", "x", "value", "21.5")[2]["value"], 21.5)

    def test_send_command_payload_cannot_add_targets(self):
        payload = json.dumps({"command": "go", "params": {"a": 1}, "area_id": "kitchen", "device_id": "d1", "entity_id": "vacuum.other"})
        _, _, data = disc.command_to_service("vacuum", "robo", "send_command", payload)
        # every key but the command and the targets is a parameter: MQTT vacuum flattens params into the payload
        self.assertEqual(data, {"command": "go", "params": {"params": {"a": 1}}, "entity_id": "vacuum.robo"})

    def test_deep_nesting_is_text_not_a_crash(self):
        deep = "[" * 200000 + "]" * 200000
        self.assertEqual(disc._json_or_text(deep), deep)
        with self.assertRaises(ValueError):  # an unknown action: the publisher rejects it (ValueError is in its tuple)
            disc.command_to_service("alarm_control_panel", "a", "command", "{" + '"a":' * 100000 + "1" + "}" * 100000)


class HelpersTest(unittest.TestCase):
    def test_deny_list(self):
        self.assertIn("persistent_notification", mp.MQTT_CALL_DENY_DOMAINS)
        self.assertNotIn("persistent_notification", mp.CALL_DENY_DOMAINS)  # the operator's Services page keeps it
        self.assertTrue(mp.CALL_DENY_DOMAINS <= mp.MQTT_CALL_DENY_DOMAINS)

    def test_dedup_key_keeps_the_id_type(self):
        self.assertNotEqual(mp._call_key("a", "b", 1), mp._call_key("a", "b", "1"))
        self.assertNotEqual(mp._call_key("a", "b", True), mp._call_key("a", "b", "True"))
        self.assertEqual(mp._call_key("a", "b", "x"), mp._call_key("a", "b", "x"))

    def test_codes_are_masked(self):
        self.assertEqual(mp._mask_codes('{"action": "DISARM", "code": "1234"}'), '{"action": "DISARM", "code": "***"}')
        self.assertEqual(mp._mask_codes('{"code": 9876, "x": 1}'), '{"code": "***", "x": 1}')
        self.assertNotIn("1234", mp._mask_codes("alarm_control_panel.alarm_disarm {'entity_id': 'a', 'code': '1234'}"))
        for kept in ('{"zipcode": "12345"}', '{"code_format": "number"}', '{"barcode": "5"}'):
            self.assertEqual(mp._mask_codes(kept), kept)

    def test_nan_constant_refused_in_calls(self):
        with self.assertRaises(ValueError):
            json.loads('{"value": NaN}', parse_constant=mp._no_constant)

    def test_remember_masks_the_history_row(self):
        pub = object.__new__(mp.MqttPublisher)
        pub.history = []
        rec = pub._remember("call", "alarm_control_panel.alarm_disarm", {"entity_id": "alarm_control_panel.a", "code": "4321"})
        self.assertNotIn("4321", rec["data"])


def _selected(referenced=(), indirect=()):
    return mock.Mock(referenced=set(referenced), indirectly_referenced=set(indirect))


class CallScopeTest(unittest.TestCase):
    def setUp(self):
        self.pub = object.__new__(mp.MqttPublisher)
        self.pub.hass = mock.Mock()
        self.pub._topics = {"switch.published": "t1", "climate.zone": "t2"}

    def _problem(self, data, selected):
        with mock.patch("homeassistant.helpers.target.async_extract_referenced_entity_ids", return_value=selected):
            return self.pub._call_target_problem(data)

    def test_published_entities_pass(self):
        self.assertIsNone(self._problem({"entity_id": "switch.published"}, _selected(["switch.published"])))

    def test_no_target_passes(self):
        # ramses_cc.send_packet: device_id is a RAMSES address, not a registry device, and resolves to nothing
        self.assertIsNone(self._problem({"device_id": "18:000730", "verb": "RQ"}, _selected()))

    def test_excluded_or_unknown_entity_refused(self):
        self.assertIn("switch.rule_excluded", self._problem({"entity_id": "switch.rule_excluded"}, _selected(["switch.rule_excluded"])))

    def test_area_resolving_outside_refused(self):
        self.assertIn("light.kitchen", self._problem({"area_id": "kitchen"}, _selected(indirect=["light.kitchen", "switch.published"])))

    def test_comma_separated_ids_are_split_before_resolving(self):
        with mock.patch("homeassistant.helpers.target.TargetSelection") as selection, \
                mock.patch("homeassistant.helpers.target.async_extract_referenced_entity_ids", return_value=_selected(["switch.published"])):
            self.assertIsNone(self.pub._call_target_problem({"entity_id": "switch.published , climate.zone"}))
        self.assertEqual(selection.call_args.args[0]["entity_id"], ["switch.published", "climate.zone"])

    def test_all_refused(self):
        self.assertIn("all", self._problem({"entity_id": "all"}, _selected()))
        self.assertIn("all", self._problem({"entity_id": ["switch.published", "all"]}, _selected()))


class ReloadLockTest(unittest.TestCase):
    def test_reload_waits_for_a_reconnect_in_flight(self):
        async def scenario():
            pub = object.__new__(mp.MqttPublisher)
            pub._conn_lock = asyncio.Lock()
            pub.config = mock.Mock(discovery_enabled=True, exclude_integrations=[])
            new = mock.Mock(discovery_enabled=True, exclude_integrations=[])
            pub.hass = mock.Mock()
            pub.hass.async_add_executor_job = mock.AsyncMock(return_value=new)
            await pub._conn_lock.acquire()  # a reconnect holds the lock
            task = asyncio.create_task(pub.async_reload_config())
            await asyncio.sleep(0.05)
            waited = not task.done() and pub.config is not new
            pub._conn_lock.release()
            await task
            return waited, pub.config is new

        self.assertEqual(asyncio.run(scenario()), (True, True))


class ExcludedSweepTest(unittest.TestCase):
    def test_rule_or_integration_exclusion_counts(self):
        pub = object.__new__(mp.MqttPublisher)
        pub.hass = mock.Mock()
        pub.config = mock.Mock(exclude_integrations=["integration_manager", "other"])
        pub.rules = mock.Mock()
        registry = mock.Mock()
        with mock.patch("homeassistant.helpers.entity_registry.async_get", return_value=registry):
            pub.rules.for_entity.return_value = {"exclude": True}
            registry.async_get.return_value = None
            self.assertTrue(pub._excluded_now("sensor.a"))
            pub.rules.for_entity.return_value = {}
            registry.async_get.return_value = mock.Mock(platform="other")
            self.assertTrue(pub._excluded_now("sensor.b"))
            registry.async_get.return_value = mock.Mock(platform="ramses_cc")
            self.assertFalse(pub._excluded_now("sensor.c"))


class ActionLimitsTest(unittest.TestCase):
    def _device(self, d):
        device = md.ManagerDevice.__new__(md.ManagerDevice)
        device.hass = mock.Mock()
        device.hass.async_add_executor_job = mock.AsyncMock(side_effect=lambda f, *a: f(*a))
        device.publisher = mock.Mock()
        device.publisher.async_publish_manager_result = mock.AsyncMock()
        device.publisher.async_after_start = mock.AsyncMock()
        device._action_lock = asyncio.Lock()
        device._running = None
        device.last_action = None
        device._runs_file = os.path.join(d, "integration_manager", md.RUNS_FILE)
        runs = md.read_json(device._runs_file, {})
        device._last_run = {k: float(v) for k, v in runs.items() if isinstance(v, (int, float))}
        return device

    def test_a_backup_just_before_a_restart_still_counts(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "integration_manager"))
            first = self._device(d)
            first._do_backup = mock.AsyncMock(return_value={"ok": True, "note": "b"})
            with mock.patch.object(md.events, "emit"):
                self.assertTrue(asyncio.run(first.async_action("backup"))["ok"])
            saved = md.read_json(first._runs_file, {})
            self.assertIn("backup", saved)
            second = self._device(d)  # a new process after the restart reads the same file
            second._do_backup = mock.AsyncMock(return_value={"ok": True})
            with mock.patch.object(md.events, "emit"):
                res = asyncio.run(second.async_action("backup"))
            self.assertFalse(res["ok"])
            self.assertIn("ran moments ago", res["error"])
            second._do_backup.assert_not_awaited()
            # restart stays unlimited: with the backup limit on disk, alternating restart and backup rotates nothing away
            self.assertNotIn("restart", md.MIN_INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
