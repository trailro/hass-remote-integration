"""Service commands from the third review: climate range halves paired into one call, literal values kept as sent."""

import asyncio
import unittest
from unittest import mock

from custom_components.integration_manager import discovery as disc
from custom_components.integration_manager import mqtt_publisher as mp


class LiteralValuesTest(unittest.TestCase):
    def test_text_message_and_option_are_kept_as_sent(self):
        self.assertEqual(disc.command_to_service("text", "t", "value", "  hello  ")[2]["value"], "  hello  ")
        self.assertEqual(disc.command_to_service("text", "t", "value", "   ")[2]["value"], "   ")
        self.assertEqual(disc.command_to_service("notify", "n", "message", "line 1\nline 2\n")[2]["message"], "line 1\nline 2\n")
        self.assertEqual(disc.command_to_service("select", "s", "option", "eco ")[2]["option"], "eco ")

    def test_tokens_and_numbers_are_still_normalised(self):
        self.assertEqual(disc.command_to_service("switch", "s", "state", " ON\n")[1], "turn_on")
        self.assertEqual(disc.command_to_service("number", "n", "value", " 21.5 ")[2]["value"], 21.5)
        self.assertEqual(disc.command_to_service("climate", "c", "mode", "heat\n")[2]["hvac_mode"], "heat")


class Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload, self.retain = topic, payload.encode(), False


def _publisher(loop, calls, low=20.0, high=24.0):
    pub = object.__new__(mp.MqttPublisher)
    pub.config = mp.MqttConfig()
    pub._live_base = "hass_demo"
    pub._key_provider = lambda: "hass_demo"
    pub._topics = {"climate.zone": "hass_demo/climate/zone"}
    pub._moving = False
    pub._range_pending = {}
    pub.history = []
    pub.stats = {"commands": 0}
    hass = mock.Mock()
    hass.loop = loop
    hass.async_create_task = loop.create_task
    hass.states.get.return_value = mock.Mock(attributes={"target_temp_low": low, "target_temp_high": high})

    async def async_call(domain, service, data, blocking=True):
        calls.append((domain, service, dict(data)))

    hass.services.async_call = async_call
    pub.hass = hass
    return pub


def _send(pub, field, value):
    pub._handle_message(Msg(f"hass_demo/cmd/climate/zone/{field}", value))


class RangePairingTest(unittest.TestCase):
    def run_scenario(self, steps, low=20.0, high=24.0):
        async def scenario():
            calls = []
            pub = _publisher(asyncio.get_running_loop(), calls, low, high)
            for step in steps:
                if isinstance(step, float):
                    await asyncio.sleep(step)
                else:
                    _send(pub, *step)
            await asyncio.sleep(0.4)
            return calls, pub.history

        with mock.patch.object(mp, "RANGE_PAIR_WAIT_S", 0.15):
            return asyncio.run(scenario())

    def test_both_bounds_up_go_out_as_one_pair(self):
        calls, history = self.run_scenario([("temperature_low", "26"), ("temperature_high", "28")])
        self.assertEqual(calls, [("climate", "set_temperature", {"entity_id": "climate.zone", "target_temp_low": 26.0, "target_temp_high": 28.0})])
        self.assertEqual([r["state"] for r in history], ["ok", "ok"])

    def test_both_bounds_down_in_reverse_order(self):
        calls, _ = self.run_scenario([("temperature_high", "18"), ("temperature_low", "16")], low=20.0, high=24.0)
        self.assertEqual(calls, [("climate", "set_temperature", {"entity_id": "climate.zone", "target_temp_high": 18.0, "target_temp_low": 16.0})])

    def test_a_single_bound_keeps_the_other_from_state(self):
        calls, history = self.run_scenario([("temperature_high", "23")])
        self.assertEqual(calls[0][2], {"entity_id": "climate.zone", "target_temp_high": 23.0, "target_temp_low": 20.0})
        self.assertEqual(history[0]["state"], "ok")

    def test_an_impossible_single_bound_is_refused_not_sent(self):
        calls, history = self.run_scenario([("temperature_low", "30")])
        self.assertEqual(calls, [])
        self.assertEqual(history[0]["state"], "rejected")
        self.assertIn("above high", history[0]["error"])

    def test_halves_of_two_separate_changes_are_not_merged(self):
        calls, _ = self.run_scenario([("temperature_high", "23"), 0.3, ("temperature_low", "19")])
        self.assertEqual([c[2]["target_temp_high"] for c in calls], [23.0, 24.0])
        self.assertEqual([c[2]["target_temp_low"] for c in calls], [20.0, 19.0])


if __name__ == "__main__":
    unittest.main()
