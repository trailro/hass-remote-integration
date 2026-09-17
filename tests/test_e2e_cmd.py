"""End-to-end campaign on 0.17.0, commands and calls.

m2: pincode, passphrase, otp, credentials, auth and wifi_psk were shown in clear by the MQTT masking (command history,
status, DEBUG log), and pincode, credentials and auth by the diagnostics masker (zip, Logs and Log files pages): the two
now share one list of names.
m3: a rejected command's reason quoted its payload, a code included, unmasked (history, status, WARNING log).
m4: a refused call payload (NaN, -Infinity, 1e999, a 5000-digit number, broken JSON) was answered with id null.
m7: a restart over MQTT was refused while another manager action ran, although it waits for installer work.
c3: the Services page answered "domain and service required" for a deny-listed domain in mixed case.
c4: an unknown manager action's name was cut in the answer but not in its error.
c5: an entity_id that is not a string reached Home Assistant, which failed on it with a TypeError.
c6: a repeated _id after its timeout, the service still running, was answered with the timeout; an empty call's
history row read domain/service.

Every test fails on the tree before the fix unless its docstring says it pins behaviour that already held.
Secrets are synthetic."""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from aiohttp.test_utils import make_mocked_request

from custom_components.integration_manager import diagnostics, logfiles_page
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import services_page
from tests.fakes import FakeInstaller, FakePublisher, FakeUpdater
from tests.test_log_follow import _Log
from tests.test_r11_mqtt import FAST_S, _seconds
from tests.test_r3_mqtt import BASE, _message, _publisher
from tests.test_r4_mqtt import _running_publisher, _settle

SECRET = "SNTL-7f3a9c41"  # synthetic
RIGHT, WRONG = SECRET[:9], "SNTL-7f3b"
NEW_NAMES = ("pincode", "passphrase", "otp", "credentials", "credential", "auth", "wifi_psk", "psk", "basic_auth",
             "user_pin", "door_passcode")
PLAIN_NAMES = ("author", "authority", "oauth", "oauth_scope", "auth_mode", "spin", "zipcode", "token_type", "psk_mode",
               "credential_id", "otp_length", "translation_key")


# ----- m2 -----------------------------------------------------------------------------------------

class SharedSecretNamesTest(unittest.TestCase):

    def test_the_mqtt_masking_hides_the_new_names(self):
        for name in NEW_NAMES:
            with self.subTest(name=name):
                for text in (json.dumps({"entity_id": "lock.x", name: SECRET}), f"{name}={SECRET}", f'x "{name}": "{SECRET}" y',
                             json.dumps({"data": json.dumps({name: SECRET})})):
                    self.assertNotIn(SECRET, mp._mask_codes(text, mp.MASK_SCAN_CHARS), text)
                self.assertTrue(mp._SECRET_KEY.fullmatch(name))

    def test_the_mqtt_masking_leaves_plain_names_readable(self):
        """Pins behaviour that already held."""
        for name in PLAIN_NAMES:
            with self.subTest(name=name):
                self.assertIn(SECRET, mp._mask_codes(json.dumps({name: SECRET})))
                self.assertIn(SECRET, mp._mask_text(f"{name}={SECRET}"))

    def test_the_diagnostics_masker_hides_the_same_names(self):
        for name in NEW_NAMES:
            with self.subTest(name=name):
                self.assertEqual(diagnostics.scrub({name: SECRET}), {name: "***"})
                for line in (f"{name}={SECRET}", f"MQTT call x.y start data={json.dumps({name: SECRET})}", f"{name}: {SECRET}"):
                    self.assertNotIn(SECRET, diagnostics._scrub_one_line_rules(line), line)
                    self.assertNotIn(SECRET, diagnostics.scrub_text(line), line)

    def test_every_shared_name_is_hidden_by_both(self):
        for name in mp.SECRET_NAME_ENDINGS + tuple(f"x_{w}" for w in mp.SECRET_NAME_WORDS) + mp.SECRET_NAME_WORDS:
            with self.subTest(name=name):
                self.assertNotIn(SECRET, mp._mask_codes(json.dumps({name: SECRET})))
                self.assertNotIn(SECRET, mp._mask_text(f"{name}={SECRET}"))
                self.assertEqual(diagnostics.scrub({name: SECRET}), {name: "***"})
                self.assertNotIn(SECRET, diagnostics._scrub_one_line_rules(f"{name}={SECRET}"))

    def test_the_diagnostics_masker_leaves_prose_readable(self):
        """Pins behaviour that already held, for the names the word rule could reach."""
        for line in ("author=Jane", "authority: local", "oauth=ok", "spin=3", "oauth_scope=read"):
            with self.subTest(line=line):
                self.assertEqual(diagnostics._scrub_one_line_rules(line), line)

    def test_the_mqtt_status_in_the_zip(self):
        pub = _publisher()
        pub._remember("call", "hri_probe.tick", {"pincode": SECRET, "wifi_psk": SECRET})
        pub._remember("cmd", "text.x/value", f"credentials={SECRET} auth={SECRET}")
        dumped = diagnostics._dump({"history": pub.recent_commands()})
        self.assertNotIn(SECRET, json.dumps(pub.recent_commands()))
        self.assertNotIn(SECRET, dumped)

    def test_the_debug_log_of_a_call(self):
        async def run():
            pub = _running_publisher()
            pub.release.set()
            with self.assertLogs(mp._LOGGER, "DEBUG") as logs:
                pub._on_call("hri_probe/tick", json.dumps({"otp": SECRET, "credentials": {"user": "u"}, "auth": SECRET,
                                                            "wifi_psk": SECRET}))
                await _settle()
            return logs.output

        output = asyncio.run(run())
        self.assertTrue(any("start" in line for line in output), output)
        self.assertNotIn(SECRET, "\n".join(output))
        self.assertNotIn('"user"', "\n".join(output))


class SharedSecretNamesTimeTest(unittest.TestCase):
    """The new names keep both text rules linear on adversarial text.  Pins behaviour that already held (the rules
    before the fix did not know these names)."""

    def test_the_mqtt_rule(self):
        for text in ('"credential" * 30000', '"a_" * 130000 + "auth"', '"_auth" * 50000', '"pincod" * 40000',
                     '"x-otp " * 40000', '"wifi_ps" * 35000 + "k"', '"auth:" * 50000'):
            with self.subTest(text=text):
                self.assertLess(_seconds(f"mp._mask_codes({text})"), FAST_S)
                self.assertLess(_seconds(f"mp._mask_text({text}, mp.MASK_SCAN_CHARS)"), FAST_S)

    def test_the_diagnostics_rule(self):
        for text in ('"credential" * 30000', '"a_" * 130000 + "auth"', '"_auth" * 50000', '"pincod" * 40000',
                     '"x-otp=" * 40000', '"auth: " * 40000'):
            with self.subTest(text=text):
                statement = ("from custom_components.integration_manager import diagnostics\n"
                             f"diagnostics._scrub_one_line_rules({text})")
                self.assertLess(_seconds(statement), FAST_S)


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


class NewNamesSearchTest(unittest.TestCase):
    """A search for part of a value under a new name answers the same for a right and a wrong guess."""

    LINES = [f"pincode={SECRET}", f"auth: {SECRET}", f"credentials={SECRET}", f"wifi_psk={SECRET}", f"user_pin={SECRET}"]

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        with open(os.path.join(self.cfg, "probe.log"), "w", encoding="utf-8") as fh:
            for i in range(300):
                fh.write(f"2026-09-17 10:00:01 INFO [probe] routine poll {i}\n")
            for line in self.LINES:
                fh.write(f"2026-09-17 10:00:02 INFO [probe] connect {line}\n")
        self.installer = FakeInstaller()
        self.installer.settings = SimpleNamespace(data={})
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job)

    def _tail(self, **params):
        request = make_mocked_request("GET", "/api/log_files/tail?" + urlencode(params),
                                      headers={"Host": "10.0.0.2:8222", "X-Requested-With": "fetch"})
        resp = asyncio.run(logfiles_page.LogFileTailView(self.hass, self.installer).get(request))
        return resp.status, resp.body

    def test_log_files_right_and_wrong_guesses(self):
        for lines in (10, 50, 5000):
            for right, wrong in ((RIGHT, WRONG), ("=" + RIGHT, "=" + WRONG), (": " + RIGHT, ": " + WRONG)):
                with self.subTest(lines=lines, right=right):
                    a, b = self._tail(lines=lines, q=right), self._tail(lines=lines, q=wrong)
                    self.assertEqual(a[0], 200)
                    self.assertEqual(json.loads(a[1])["lines"], [])
                    self.assertEqual(a, b)

    def test_logs_page_right_and_wrong_guesses(self):
        log = _Log(self)
        for i in range(40):
            log.write(f"routine poll {i}")
        for line in self.LINES:
            log.write(f"connect {line}", level=logging.DEBUG)
        for params in ({"level": "DEBUG"}, {"level": "DEBUG", "limit": 2}, {"level": "DEBUG", "since_id": 40}):
            with self.subTest(**params):
                a = log.api(**dict({"limit": 200}, **params, q=RIGHT))
                b = log.api(**dict({"limit": 200}, **params, q=WRONG))
                self.assertEqual(a["records"], [])
                self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
