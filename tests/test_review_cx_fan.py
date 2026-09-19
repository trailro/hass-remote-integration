"""A malformed oscillation payload used to stop the fan instead of being refused.

`{"oscillating": p == "oscillate_on"}` reads every payload that is not exactly that token as False -
a typo, a producer that sends `true`, an empty string - and Home Assistant's fan contract takes a
boolean, so the malformed value had already become a valid action by the time it reached the service.
Every other command token in this file is checked against the list it belongs to; this one is now too.
"""

import unittest

from custom_components.integration_manager import discovery as disc


class OscillationTokenTest(unittest.TestCase):
    def call(self, payload):
        return disc.command_to_service("fan", "living", "oscillate", payload)

    def test_the_two_tokens_do_what_they_say(self):
        self.assertEqual(self.call("oscillate_on"), ("fan", "oscillate", {"entity_id": "fan.living", "oscillating": True}))
        self.assertEqual(self.call("oscillate_off"), ("fan", "oscillate", {"entity_id": "fan.living", "oscillating": False}))

    def test_case_and_surrounding_spaces_are_ignored_like_every_other_token(self):
        for payload in ("OSCILLATE_ON", " oscillate_on ", "Oscillate_On"):
            with self.subTest(payload=payload):
                self.assertIs(self.call(payload)[2]["oscillating"], True)

    def test_a_malformed_payload_is_refused_instead_of_stopping_the_fan(self):
        for payload in ("garbage", "true", "1", "0", "off", "", "   ", "oscillate", "oscillate_onn"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as ctx:
                    self.call(payload)
                self.assertIn("oscillate_on", str(ctx.exception))
                self.assertIn("oscillate_off", str(ctx.exception))

    def test_the_refusal_does_not_echo_the_payload(self):
        """Like the other tokens: a payload may carry a code, and the answer goes back over MQTT."""
        with self.assertRaises(ValueError) as ctx:
            self.call("secret-looking-payload")
        self.assertNotIn("secret-looking-payload", str(ctx.exception))

    def test_the_other_fan_commands_are_unchanged(self):
        self.assertEqual(self.call_field("state", "ON")[1], "turn_on")
        self.assertEqual(self.call_field("percentage", "40")[2]["percentage"], 40)
        self.assertEqual(self.call_field("preset_mode", "eco")[2]["preset_mode"], "eco")
        self.assertEqual(self.call_field("direction", "forward")[2]["direction"], "forward")

    def call_field(self, field, payload):
        return disc.command_to_service("fan", "living", field, payload)
