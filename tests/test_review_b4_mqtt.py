"""Review of b4cd1a1, MQTT side.  S3-1: the value of a text.set_value call for a text entity in password mode was kept
in clear in the command history when the call was refused before it was masked (an oversized _id, a denied domain,
a payload that does not parse), and a number was never masked at all."""

import json
import unittest
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r14_mqtt import _password_publisher

SECRET = "hunter2"


class PasswordValueMaskedOnEveryRefusalTest(unittest.TestCase):
    def setUp(self):
        self.pub = _password_publisher()

    def _history(self):
        return json.dumps(list(self.pub.history), default=str)

    def _call(self, payload, topic="text/set_value"):
        with mock.patch.object(mp._LOGGER, "warning"):
            self.pub._on_call(topic, payload if isinstance(payload, str) else json.dumps(payload))

    def test_an_oversized_id(self):
        self._call({"entity_id": "text.pw", "value": SECRET, "_id": "x" * (mp.CALL_ID_MAX_BYTES + 10)})
        self.assertEqual(self.pub.history[-1]["state"], "rejected")
        self.assertNotIn(SECRET, self._history())
        self.assertIn("***", self.pub.history[-1]["data"])

    def test_a_domain_excluded(self):
        self.pub.config.exclude_integrations = ["text"]
        self._call({"entity_id": "text.pw", "value": SECRET})
        self.assertEqual(self.pub.history[-1]["state"], "rejected")
        self.assertNotIn(SECRET, self._history())

    def test_a_payload_that_does_not_parse(self):
        self._call('{"entity_id": "text.pw", "value": "' + SECRET + '", "x": NaN}')
        self.assertEqual(self.pub.history[-1]["state"], "rejected")
        self.assertNotIn(SECRET, self._history())

    def test_a_number(self):
        self._call({"entity_id": "text.pw", "value": 480913, "_id": "x" * (mp.CALL_ID_MAX_BYTES + 10)})
        self.assertNotIn("480913", self._history())

    def test_a_number_is_a_secret_to_mask_in_the_service_error(self):
        self.assertEqual(mp.password_value(self.pub.hass, "text", "set_value", {"entity_id": "text.pw", "value": 480913},
                                           lambda: []), "480913")

    def test_a_plain_text_entity_stays_readable(self):
        self._call({"entity_id": "text.plain", "value": "visible", "_id": "x" * (mp.CALL_ID_MAX_BYTES + 10)})
        self.assertIn("visible", self._history())


if __name__ == "__main__":
    unittest.main()
