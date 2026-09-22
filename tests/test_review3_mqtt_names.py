"""The MQTT command history, status and log mask by the shared credential list (logbuffer), not one of their own.

The MQTT list had drifted from the one diagnostics and the request-line rule use: pass=, pw= and bearer= were
masked in a log line and printed in the command history."""

import unittest

import logbuffer
from custom_components.integration_manager import mqtt_publisher as mp


class MqttSecretNamesTest(unittest.TestCase):
    def test_the_lists_are_the_shared_ones(self):
        self.assertEqual(tuple(mp.SECRET_NAME_WORDS), tuple(logbuffer.CREDENTIAL_WORDS))
        self.assertEqual(set(logbuffer.CREDENTIAL_NAMES) - set(mp.SECRET_NAME_ENDINGS), set())
        self.assertIn("credentials", mp.SECRET_NAME_ENDINGS)  # a name must END in these here

    def test_the_words_that_drifted_are_masked(self):
        for text in ('{"pass": "hunter2"}', '{"db_pw": "hunter2"}', '{"bearer": "hunter2"}', "pass=hunter2",
                     '{"api_credentials": "hunter2"}', '{"hmac": "hunter2"}'):
            with self.subTest(text=text):
                self.assertNotIn("hunter2", mp._mask_text(text))

    def test_look_alikes_stay_readable(self):
        # (status_code is not among them: the MQTT rule masks every *_code, the safe direction, and never had the
        # result-code exemption diagnostics keeps for a readable log)
        for text in ('{"passed": 3}', '{"pass_count": 2}', '{"author": "x"}', '{"bypass": "x"}', '{"spin": 1}'):
            with self.subTest(text=text):
                self.assertEqual(mp._mask_text(text), text)


if __name__ == "__main__":
    unittest.main()
