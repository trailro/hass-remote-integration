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


# ----- m3 -----------------------------------------------------------------------------------------

class RejectedCommandReasonTest(unittest.TestCase):

    def _command(self, topic, payload):
        pub = _publisher()
        pub._topics = {"alarm_control_panel.house": "t", "number.level": "t"}
        pub.stats.update(commands=0, last_command=None)
        with self.assertLogs(mp._LOGGER, "WARNING") as logs:
            pub._on_message(None, None, _message(f"{BASE}/cmd/{topic}", payload))
        return pub, "\n".join(logs.output)

    def test_an_unknown_alarm_action_with_a_code(self):
        pub, log = self._command("alarm_control_panel/house/command", f"DISARM CODE={SECRET}")
        row = pub.history[-1]
        self.assertEqual(row["state"], "rejected")
        self.assertNotIn(SECRET, row["error"])
        self.assertNotIn(SECRET, json.dumps(pub.recent_commands()))
        self.assertNotIn(SECRET, log)
        self.assertIn("unknown command", row["error"])

    def test_a_number_that_does_not_convert(self):
        pub, log = self._command("number/level/value", f'{{"pin": "{SECRET}"}}')
        self.assertNotIn(SECRET, pub.history[-1]["error"])
        self.assertNotIn(SECRET, log)
        self.assertIn("could not convert", pub.history[-1]["error"])


# ----- m4 -----------------------------------------------------------------------------------------

class RefusedCallIdTest(unittest.TestCase):

    def _answer(self, payload, topic="light/turn_on"):
        pub = _publisher()
        pub._on_message(None, None, _message(f"{BASE}/call/{topic}", payload))
        self.assertEqual(len(pub.results), 1, pub.results)
        self.assertFalse(pub.results[0][2]["ok"])
        return pub.results[0][2], pub.history[-1]

    def test_the_reported_payloads(self):
        for payload in ('{"_id": "r1", "brightness": NaN}', '{"_id": "r1", "brightness": -Infinity}',
                        '{"_id": "r1", "brightness": 1e999}', '{"_id": "r1", "brightness": ' + "9" * 5000 + "}",
                        '{"_id": "r1", "brightness": }', '{"brightness": NaN, "_id": "r1"}'):
            with self.subTest(payload=payload[:60]):
                answer, row = self._answer(payload)
                self.assertIn("bad payload", answer["error"])
                self.assertEqual(answer["id"], "r1")
                self.assertEqual(row["id"], "r1")

    def test_too_large_and_too_deep(self):
        for payload in (json.dumps({"_id": 42, "text": "x" * (mp.CALL_MAX_BYTES + 1)}),
                        '{"_id": 42, "a": ' + "[" * 100 + "]" * 100 + "}"):
            with self.subTest(size=len(payload)):
                self.assertEqual(self._answer(payload)[0]["id"], 42)

    def test_a_denied_domain_with_a_payload_that_does_not_parse(self):
        self.assertEqual(self._answer('{"_id": true, "x": NaN}', "shell_command/x")[0]["id"], True)

    def test_the_id_keeps_its_type(self):
        for literal, expected in (('"7"', "7"), ("7", 7), ("-7.5", -7.5), ("null", None), ("false", False),
                                  ('"\\u005fx\\"y"', '_x"y')):
            with self.subTest(literal=literal):
                self.assertEqual(self._answer('{"_id": %s, "v": NaN}' % literal)[0]["id"], expected)

    def test_an_escaped_key(self):
        self.assertEqual(self._answer('{"\\u005fid": "e1", "v": NaN}')[0]["id"], "e1")

    def test_what_is_not_the_outer_id(self):
        """Pins null where no id of the outer object can be read."""
        for payload in ('{"data": {"_id": "inner"}, "v": NaN}', '{"_id": NaN}', '{"_id": 1e999, "v": 1}', '{"_id": [1], "v": NaN}',
                        '{"_id": ' + "9" * 5000 + "}", '[{"_id": "a"}]', '{"text": "\\"_id\\": \\"s\\"", "v": NaN}',
                        '{"_id": {"a": 1}, "v": NaN}'):
            with self.subTest(payload=payload[:60]):
                self.assertIsNone(self._answer(payload)[0]["id"])

    def test_the_last_id_wins_like_json(self):
        self.assertEqual(self._answer('{"_id": 1, "_id": 2, "v": NaN}')[0]["id"], 2)

    def test_linear_time(self):
        for text in ('"{" + \'"_id":1,\' * 60000', '"{\\"_id\\": " + "\\"\\\\\\\\" * 80000',
                     '"{" + \'"\\\\u005fid":1,\' * 20000', '"{\\"_id\\":1," + "[" * 250000',
                     '"{" + \'"_id":\' * 60000 + "NaN"', '"{\\"_id\\": " + "9" * 250000'):
            with self.subTest(text=text):
                self.assertLess(_seconds(f"mp._refused_call_id({text})"), FAST_S)


# ----- m7 -----------------------------------------------------------------------------------------

def _device():
    inst = FakeInstaller()
    return md.ManagerDevice(SimpleNamespace(), inst, FakeUpdater(), FakePublisher(log=inst.log))


class RestartWaitsForAnActionTest(unittest.IsolatedAsyncioTestCase):

    async def test_a_restart_right_after_check_updates(self):
        dev = _device()
        release = asyncio.Event()

        async def check():
            await release.wait()
            return {"ok": True, "note": "integration up to date"}

        dev._do_check_updates = check
        first = asyncio.create_task(dev.async_action("check_updates"))
        await asyncio.sleep(0)
        self.assertEqual(dev._running, "check_updates")
        waited = []
        real_sleep = asyncio.sleep

        async def sleep(seconds):
            waited.append(seconds)
            if len(waited) == 4:
                release.set()
            await real_sleep(0)

        with mock.patch.object(md.asyncio, "sleep", sleep):
            restart = await asyncio.wait_for(dev.async_action("restart"), 10)
        release.set()  # the tree before the fix refused at once, without waiting
        self.assertTrue((await first)["ok"])
        self.assertTrue(restart["ok"], restart)
        self.assertIn(("restart",), dev.installer.log)
        self.assertGreaterEqual(len(waited), 4)

    async def test_the_wait_is_five_minutes_in_all(self):
        dev = _device()
        never = asyncio.Event()

        async def hung():
            await never.wait()
            return {"ok": True}

        dev._do_backup = hung
        first = asyncio.create_task(dev.async_action("backup"))
        self.addCleanup(first.cancel)
        await asyncio.sleep(0)
        waited = []

        async def sleep(seconds):
            waited.append(seconds)

        with mock.patch.object(md.asyncio, "sleep", sleep):
            res = await dev.async_action("restart")
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"].startswith("restart skipped: backup is still running"), res["error"])
        self.assertEqual(sum(waited), 300)
        self.assertNotIn(("restart",), dev.installer.log)

    async def test_the_action_wait_and_the_installer_wait_share_the_five_minutes(self):
        dev = _device()
        release = asyncio.Event()

        async def check():
            await release.wait()
            dev.installer.busy = True  # an install from the UI, still running when the action ends
            return {"ok": True}

        dev._do_check_updates = check
        first = asyncio.create_task(dev.async_action("check_updates"))
        await asyncio.sleep(0)
        waited = []
        real_sleep = asyncio.sleep

        async def sleep(seconds):
            waited.append(seconds)
            if len(waited) == 100:
                release.set()
            await real_sleep(0)

        with mock.patch.object(md.asyncio, "sleep", sleep):
            res = await asyncio.wait_for(dev.async_action("restart"), 10)
        release.set()
        await first
        self.assertEqual(res["error"], "restart skipped: another action is still running")
        self.assertEqual(sum(waited), 300)

    async def test_any_other_second_action_is_still_refused(self):
        """Pins behaviour that already held."""
        dev = _device()
        release = asyncio.Event()

        async def check():
            await release.wait()
            return {"ok": True}

        dev._do_check_updates = check
        first = asyncio.create_task(dev.async_action("check_updates"))
        await asyncio.sleep(0)
        res = await dev.async_action("backup")
        self.assertTrue(res["error"].startswith("check_updates is still running"), res["error"])
        release.set()
        await first


# ----- c4 -----------------------------------------------------------------------------------------

class UnknownManagerActionTest(unittest.TestCase):

    def test_a_long_name_is_cut_the_same_everywhere(self):
        pub = _publisher()
        pub._client, pub._connected = mock.Mock(), True
        name = "a" * 300
        pub._on_manager_command(name, "PRESS")
        answer = json.loads(pub._client.publish.call_args.args[1])
        self.assertEqual(answer["action"], "a" * 40)
        self.assertEqual(answer["error"], f"unknown action {'a' * 40!r}")
        self.assertEqual(pub.history[-1]["error"], answer["error"])
        self.assertEqual(pub.history[-1]["what"], "a" * 40)


# ----- c5 -----------------------------------------------------------------------------------------

class UnreadableEntityIdTest(unittest.TestCase):

    def test_ids_that_are_not_strings(self):
        pub = _publisher()
        pub._topics = {"light.published": "t"}
        for ids in (5, 5.5, True, {"a": 1}, ["light.published", 5], [None]):
            with self.subTest(ids=ids):
                problem = pub._call_target_problem({"entity_id": ids}, "light", "turn_on")
                self.assertEqual(problem, "the target cannot be read (entity_id): entity, device, area, floor and label ids must be strings")

    def test_the_call_is_answered_and_never_reaches_home_assistant(self):
        called = []

        async def service(*a, **k):
            called.append(a)

        async def run():
            pub = _running_publisher()
            del pub._call_target_problem  # the real check
            pub._service_reach = lambda *_a: None
            pub._topics = {"light.published": "t"}
            pub.hass.services.async_call = service
            with self.assertLogs(mp._LOGGER, "WARNING"):
                pub._on_call("light/turn_on", json.dumps({"_id": "c5", "entity_id": 5}))
                await _settle()
            return pub

        pub = asyncio.run(run())
        self.assertEqual(called, [])
        self.assertIn("the target cannot be read", pub.results[-1][2]["error"])
        self.assertEqual(pub.results[-1][2]["id"], "c5")


# ----- c6 -----------------------------------------------------------------------------------------

class RepeatAfterTimeoutTest(unittest.IsolatedAsyncioTestCase):

    async def test_a_repeat_while_the_service_still_runs(self):
        pub = _running_publisher()
        with mock.patch.object(mp, "CALL_TIMEOUT_S", 0), self.assertLogs(mp._LOGGER, "WARNING"):
            pub._on_call("hri_probe/tick", json.dumps({"_id": "t1"}))
            await _settle()
            self.assertIn("timeout after 0s", pub.results[-1][2]["error"])
            pub._on_call("hri_probe/tick", json.dumps({"_id": "t1"}))
            self.assertEqual(pub.results[-1][2], {"id": "t1", "service": "hri_probe.tick", "ok": None, "state": "running", "duplicate": True})
            pub.release.set()
            await _settle()
            self.assertEqual(pub.results[-1][2], {"id": "t1", "service": "hri_probe.tick", "ok": True, "late": True})
            pub._on_call("hri_probe/tick", json.dumps({"_id": "t1"}))
            self.assertEqual(pub.results[-1][2], {"id": "t1", "service": "hri_probe.tick", "ok": True, "late": True, "duplicate": True})
        self.assertEqual(pub._in_flight, 0)


class EmptyCallRowTest(unittest.TestCase):

    def test_the_row_names_the_service(self):
        pub = _publisher()
        pub._on_message(None, None, _message(f"{BASE}/call/Hri_Probe/tick", " "))
        self.assertEqual(pub.history[-1]["what"], "hri_probe.tick")
        self.assertEqual(pub.results[-1][2]["service"], "hri_probe.tick")
        pub._on_message(None, None, _message(f"{BASE}/call/hri_probe/tick/extra", ""))
        self.assertEqual(pub.history[-1]["what"], "hri_probe/tick/extra")


if __name__ == "__main__":
    unittest.main()
