"""The log pages after the eleventh review.

N2: the Log files search skips the one-line rules for a line that holds none
of their literals, but logbuffer.mask_query_secrets decides on the
percent-decoded parameter name and path: ``GET /x?%73ession=<secret>`` was
masked and skipped the rules, so the raw search matched a right guess, the
tail stopped early and ``total_lines_scanned`` read 11 for a right prefix and
511 for a wrong one while the rows were empty for both.

N7: a ``-----BEGIN ...-----`` line with no END marker masked the start of the
next, unrelated line (``***-09-16 10:00:01 ...``).

N8: the Logs page's follow cursor moved only through records the level and
logger filters let through: with a filter matching nothing it never moved.

N9: a follow page (since_id) parsed and held every newer record before the
page limit applied: 23 MB for 200 rows over a 4.7 MB log.

N10: a query parameter was masked when its name merely held a credential word
(zipcode, keyword, design, monkey, sort_key), and every parameter of any URL
whose path ended in /api/logs.

N16 and the events drain: run.py's messages.

Every answer a search gives is compared whole between a right and a wrong
guess.  Every test fails on the tree before the fix unless its docstring says
it pins behaviour that already held.
"""

import asyncio
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import tracemalloc
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from aiohttp.test_utils import make_mocked_request

import logbuffer
import run
from custom_components.integration_manager import diagnostics, logfiles_page, logs_page
from tests.fakes import FakeInstaller
from tests.test_log_follow import _Log

SECRET = "SNTL-7f3a9c41"  # synthetic
RIGHT, WRONG = SECRET[:7], "SNTL-8f"
PIN = "572931"  # synthetic


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


# ----- N2 -----------------------------------------------------------------------------------------

class DecodedNameSearchTest(unittest.TestCase):

    def setUp(self):
        self.cfg = _tmp(self)
        with open(os.path.join(self.cfg, "probe.log"), "w", encoding="utf-8") as fh:
            fh.write("2026-09-16 10:00:00 INFO [probe] start\n")
            for i in range(500):
                fh.write(f"2026-09-16 10:00:01 INFO [probe] routine poll {i}\n")
            for i in range(10):
                fh.write(f'2026-09-16 10:00:02 INFO [aiohttp.access] 172.17.0.1 "GET /api/states?%73ession={SECRET}{i} HTTP/1.1" 200 12\n')
        self.installer = FakeInstaller()
        self.installer.settings = SimpleNamespace(data={})
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job)

    def _tail(self, **params):
        request = make_mocked_request("GET", "/api/log_files/tail?" + urlencode(params),
                                      headers={"Host": "10.0.0.2:8222", "X-Requested-With": "fetch"})
        resp = asyncio.run(logfiles_page.LogFileTailView(self.hass, self.installer).get(request))
        return resp.status, resp.body

    def test_the_reviewers_case_answers_byte_for_byte_the_same(self):
        for lines in (10, 50, 5000):
            with self.subTest(lines=lines):
                right, wrong = self._tail(lines=lines, q=RIGHT), self._tail(lines=lines, q=WRONG)
                self.assertEqual(right[0], 200)
                self.assertEqual(json.loads(right[1])["lines"], [])
                self.assertEqual(right, wrong)

    def test_every_prefix_of_the_value(self):
        baseline = self._tail(lines=10, q="SNTL-zzzzzzzzzzzz")
        for n in range(len("SNTL-") + 1, len(SECRET) + 1):
            with self.subTest(guess=SECRET[:n]):
                self.assertEqual(self._tail(lines=10, q=SECRET[:n]), baseline)

    def test_a_search_still_finds_what_the_mask_leaves(self):
        """Pins behaviour that already held."""
        status, body = self._tail(lines=50, q="%73ession")
        self.assertEqual(status, 200)
        rows = [row["raw"] for row in json.loads(body)["lines"]]
        self.assertEqual(len(rows), 10)
        self.assertTrue(all("%73ession=*** HTTP/1.1" in row for row in rows), rows)


class PrefilterSeesDecodedNamesTest(unittest.TestCase):
    """The literals logfiles_page checks before it runs the rules, against lines whose parameter name or search
    path is percent-encoded: every line the rules change must pass the prefilter."""

    WORDS = ("token", "access_token", "authSig", "password", "passwd", "passphrase", "passcode", "session", "sessionid",
             "apikey", "api_key", "APIKey", "code", "key", "sig", "signature", "cookie", "credential", "pwd", "secret")
    PATHS = ("/api/logs", "/api/log_files/tail")

    @staticmethod
    def _encodings(word):
        """The word, each character of it percent-encoded in turn (both hex cases), all of it, and the characters
        re's IGNORECASE takes for an ASCII letter, encoded as UTF-8."""
        out = [word, "".join(f"%{ord(c):02X}" for c in word)]
        for i, c in enumerate(word):
            out += [word[:i] + f"%{ord(c):02X}" + word[i + 1:], word[:i] + f"%{ord(c):02x}" + word[i + 1:]]
        for plain, other in (("s", "ſ"), ("k", "K"), ("i", "ı")):
            if plain in word.lower():
                i = word.lower().index(plain)
                out.append(word[:i] + "".join(f"%{b:02X}" for b in other.encode()) + word[i + 1:])
        return out

    def _missed(self, corpus):
        changed = [line for line in corpus if diagnostics._scrub_one_line_rules(line) != line]
        self.assertTrue(changed)  # the corpus is not vacuous
        return [line for line in changed if not logfiles_page._rules_may_change(line)]

    def test_the_reviewers_line(self):
        line = f'"GET /x?%73ession={SECRET} HTTP/1.1" 200'
        self.assertNotEqual(diagnostics._scrub_one_line_rules(line), line)
        self.assertTrue(logfiles_page._rules_may_change(line))

    def test_every_encoded_credential_name_and_search_path(self):
        corpus = [f'"GET /x?a=1&{name}=v1 HTTP/1.1" 200' for word in self.WORDS for name in self._encodings(word)]
        corpus += [f'"GET {path}?level=DEBUG&q=v1 HTTP/1.1" 200' for p in self.PATHS for path in self._encodings(p)]
        corpus += [f"Filtered a request: http://10.0.0.2:8222{path}?q=v1&v2" for p in self.PATHS for path in self._encodings(p)]
        self.assertEqual(self._missed(corpus), [])

    def test_random_urls(self):
        rng = random.Random(11)
        parts = ["?", "&", "=", "v1", "/x", "/api/", "log", "s", "_files/tail", "%73", "%65", "%6B", "%6b", "%69", "%67",
                 "ession", "ey", "k", "o", "de", "c", "ig", "%C5%BF", "%E2%84%AA", "%C4%B1", "http://h", "+", "%2F", "%25",
                 "pa", "%73%73", "wd", "tok", "en", "%ab", "asic"]
        corpus = ["GET " + "".join(rng.choice(parts) for _ in range(rng.randint(3, 14))) for _ in range(30_000)]
        self.assertEqual(self._missed(corpus), [])


# ----- N7 -----------------------------------------------------------------------------------------

class BeginWithoutEndTest(unittest.TestCase):
    BEGIN = "2026-09-16 10:00:00 INFO [probe] -----BEGIN CERTIFICATE-----"
    NEXT = "2026-09-16 10:00:01 INFO [probe] next unrelated line"
    BODY = "MIIBSYNTHETICbody"  # short: only the PEM rule can mask it

    def test_the_next_line_keeps_every_character(self):
        self.assertEqual(diagnostics.scrub_lines([self.BEGIN, self.NEXT])[1], self.NEXT)
        self.assertEqual(diagnostics.scrub_text(f"{self.BEGIN}\n{self.NEXT}").split("\n")[1], self.NEXT)
        self.assertEqual(diagnostics.scrub(f"{self.BEGIN}\n{self.NEXT}").split("\n")[1], self.NEXT)

    def test_a_line_of_words_is_not_eaten_either(self):
        self.assertEqual(diagnostics.scrub_lines(["-----BEGIN CERTIFICATE-----", "next unrelated line"])[1], "next unrelated line")

    def test_the_next_line_reads_the_same_in_every_window(self):
        path = os.path.join(_tmp(self), "app.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{self.BEGIN}\n{self.NEXT}\n")
        windows = {needle: logfiles_page._tail_masked(path, 50, needle)[0] for needle in ("", "INFO", "[probe]", "next")}
        self.assertEqual({needle: rows[-1] for needle, rows in windows.items()}, dict.fromkeys(windows, self.NEXT))

    def test_a_cut_body_under_the_begin_line_is_still_masked(self):
        """Pins behaviour that already held: the lines under a BEGIN line that are nothing but base64."""
        out = diagnostics.scrub_lines(["-----BEGIN PRIVATE KEY-----", self.BODY, "QUJD=="])
        self.assertEqual(out, ["-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----", "***", "***"])
        self.assertNotIn(self.BODY, diagnostics.scrub(f"cut -----BEGIN EC PRIVATE KEY-----\n{self.BODY}"))
        self.assertNotIn(self.BODY, diagnostics.scrub(f"-----BEGIN RSA PRIVATE KEY-----\\n{self.BODY}\\n"))  # a repr, cut


# ----- N8 -----------------------------------------------------------------------------------------

class _PairLog(_Log):
    def __init__(self, root):
        self.handler = logbuffer.FileLogHandler(os.path.join(root, "process.log"))


ROUTINE = [(f"routine poll {i}", "custom_components.probe", logging.INFO) for i in range(41)]
LINES = ROUTINE + [(f"connection pin_code={PIN}", "custom_components.probe", logging.INFO)] + [
    (f"library chatter {i}", "other.lib", logging.DEBUG) for i in range(5)]
NEWEST = len(LINES)


class FilterCursorTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _answers(self, lines, params):
        for name in os.listdir(self.root):
            os.remove(os.path.join(self.root, name))
        log = _PairLog(self.root)
        try:
            for message, logger, level in lines:
                log.write(message, logger=logger, level=level)
            return [log.api(**p) for p in params]
        finally:
            log.handler.close()

    def test_a_filter_that_matches_nothing_moves_the_cursor(self):
        for params in ({"level": "CRITICAL"}, {"level": "DEBUG", "prefix": "nothing.matches"}):
            for since_id in (0, 41):
                with self.subTest(since_id=since_id, **params):
                    a, = self._answers(LINES, [dict(params, limit=200, since_id=since_id)])
                    self.assertEqual((a["records"], a["truncated"], a["cursor"]), ([], False, NEWEST))

    def test_the_follow_does_not_parse_the_same_records_on_every_poll(self):
        log = _Log(self)
        for message, logger, level in LINES:
            log.write(message, logger=logger, level=level)
        since, parsed = 1, []
        for _ in range(3):
            with mock.patch.object(logbuffer, "json", wraps=json) as spy:
                _, _, since = logs_page._query_masked(log.handler, min_level=logging.CRITICAL, since_id=since, limit=200)
            parsed.append(spy.loads.call_count)
        self.assertEqual(since, NEWEST)
        self.assertEqual(parsed[1:], [0, 0])

    def test_right_and_wrong_guesses_with_filters(self):
        cases = [
            dict(level="WARNING"), dict(level="DEBUG", prefix="nothing.matches"), dict(level="DEBUG", prefix="other.lib"),
            dict(level="INFO", prefix="custom_components.probe"), dict(level="INFO", limit=1), dict(level="DEBUG", limit=3),
        ]
        for params in cases:
            for since_id in (0, 1, 40, 41):
                for n in (1, 3, len(PIN)):
                    right, wrong = "pin_code=" + PIN[:n], "pin_code=" + PIN[:n - 1] + str((int(PIN[n - 1]) + 1) % 10)
                    with self.subTest(right=right, since_id=since_id, **params):
                        p = dict({"limit": 200, "since_id": since_id}, **params)
                        a, b = self._answers(LINES, [dict(p, q=right), dict(p, q=wrong)])
                        self.assertEqual(a, b)
                        self.assertEqual(a["records"], [])

    def test_the_same_guess_over_two_pins(self):
        other = [(m.replace(PIN, "672931"), lg, lv) for m, lg, lv in LINES]
        for params in (dict(level="WARNING"), dict(level="DEBUG", prefix="other.lib"), dict(level="INFO", limit=1)):
            for since_id in (0, 41):
                with self.subTest(since_id=since_id, **params):
                    p = dict({"limit": 200, "since_id": since_id, "q": "pin_code=5"}, **params)
                    self.assertEqual(self._answers(LINES, [p]), self._answers(other, [p]))


# ----- N9 -----------------------------------------------------------------------------------------

class FollowPageMemoryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.handler = logbuffer.FileLogHandler(os.path.join(cls.root, "process.log"))
        i = 0
        while cls._size() < 4_700_000:
            for _ in range(500):
                cls.handler.emit(logging.LogRecord("custom_components.probe.sub", logging.INFO, __file__, 0,
                                                   f"routine poll {i} of the probe device, answer 200 OK, payload 1234 bytes", None, None))
                i += 1

    @classmethod
    def tearDownClass(cls):
        cls.handler.close()
        shutil.rmtree(cls.root, True)

    @classmethod
    def _size(cls):
        return sum(os.path.getsize(p) for p in cls.handler._files() if os.path.exists(p))  # noqa: SLF001

    def test_a_follow_page_holds_the_page_not_the_log(self):
        tracemalloc.start()
        try:
            records, truncated, cursor = logs_page._query_masked(self.handler, since_id=1, limit=200)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual((len(records), truncated, cursor), (200, True, 201))
        self.assertLess(peak, 2_000_000, f"{peak / 1e6:.1f} MB for 200 rows over {self._size() / 1e6:.1f} MB of log")

    def test_the_pages_are_the_ones_a_whole_read_gives(self):
        """A follow page is still the oldest `limit` records newer than since_id that pass the filters, across the
        rotated files, and truncated still says whether one more passes; the cursor it reports is new (N8)."""
        with open(self.handler.path, "a", encoding="utf-8") as fh:
            fh.write('{"id": 99999999, "ts": "torn\n')  # a torn line, skipped like any line that is not JSON
        self.addCleanup(self._drop_last_line)
        every = []
        for p in reversed(self.handler._files()):  # noqa: SLF001
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        every.append(json.loads(line))
                    except ValueError:
                        pass
        rng = random.Random(9)
        for _ in range(40):
            since_id = rng.choice([1, every[0]["id"] + 1, every[len(every) // 2]["id"], every[-5]["id"], every[-1]["id"]])
            limit = rng.choice([1, 7, 200, 2000])
            prefixes = rng.choice([(), ("custom_components.probe",), ("nothing",)])
            text = rng.choice(["", "poll 1", "zzz"])
            with self.subTest(since_id=since_id, limit=limit, prefixes=prefixes, text=text):
                passing = [r for r in every if r["id"] > since_id and (not prefixes or r["logger"].startswith(prefixes))
                           and text in r["message"]]
                seen = []
                records, truncated = self.handler.query(since_id=since_id, limit=limit, prefixes=prefixes, text=text, cursor=seen.append)
                self.assertEqual(records, passing[:limit])
                self.assertEqual(truncated, len(passing) > limit)
                # a full page ends at the record read before the first one that did not fit
                self.assertEqual(seen, [every[every.index(passing[limit]) - 1]["id"] if len(passing) > limit else every[-1]["id"]])

    def _drop_last_line(self):
        with open(self.handler.path, "rb+") as fh:
            data = fh.read()
            fh.seek(0)
            fh.truncate()
            fh.write(data[:data.rstrip(b"\n").rfind(b"\n") + 1])


class LineSeparatorTest(unittest.TestCase):
    def test_a_message_holding_a_unicode_line_separator_is_shown(self):
        """str.splitlines cuts a line at U+2028, which json.dumps writes as it is: the record was never shown
        (the follow read of N9 splits on newlines only, and the newest window now does the same)."""
        log = _Log(self)
        log.write("before")
        log.write("one two")
        log.write("after")
        for since_id in (0, 1):
            with self.subTest(since_id=since_id):
                self.assertIn("one two", [r["message"] for r in log.api(level="DEBUG", limit=200, since_id=since_id)["records"]])


# ----- N10 ----------------------------------------------------------------------------------------

class QueryParameterNamesTest(unittest.TestCase):

    def test_ordinary_parameters_are_left_as_they_are(self):
        for line in ('"GET /api/geo?zipcode=12345&keyword=bakery&design=flat&monkey=1&sort_key=name HTTP/1.1" 200',
                     "fetch https://api.example/v2/list?zip=1&primary_key=7&translation_key=title&signal=3&passenger=2&bypass=1",
                     '"GET /api/hassio/app/api/logs?page=2&filter=warn HTTP/1.1" 200',
                     '"GET /x/api/logs?level=DEBUG&q=text HTTP/1.1" 200',
                     '"GET /api/logs/loggers?view=all HTTP/1.1" 200'):
            with self.subTest(line=line):
                self.assertEqual(logbuffer.mask_query_secrets(line), line)

    def test_credentials_are_still_masked(self):
        for name in ("access_token", "authSig", "api_key", "apiKey", "APIKEY", "apikey", "client_secret", "password", "passwd",
                     "pass", "passcode", "code", "session", "sessionid", "SESSIONID", "sessionId", "X-Amz-Signature",
                     "X-Amz-Credential", "refresh_token", "cookie", "pwd", "key", "sig", "%73ession", "ToKeN", "db.pass",
                     "wifi_key", "code_verifier", "Key"):
            with self.subTest(name=name):
                self.assertEqual(logbuffer.mask_query_secrets(f"GET /x?{name}={SECRET}&a=1"), f"GET /x?{name}=***&a=1")

    def test_the_search_endpoints_are_matched_exactly(self):
        for path in ("/api/logs", "/api/log_files/tail", "/api/logs/", "/api/%6Cogs", "http://10.0.0.2:8222/api/logs"):
            with self.subTest(path=path):
                self.assertEqual(logbuffer.mask_query_secrets(f"{path}?level=DEBUG&q={SECRET}&page=2"),
                                 f"{path}?level=DEBUG&q=***&page=***")

    def test_a_masked_line_is_the_same_for_a_right_and_a_wrong_guess(self):
        """Pins behaviour that already held."""
        for template in ("/api/logs?level=DEBUG&q={}", "/api/states?authSig={}", "/api/states?%73ession={}"):
            with self.subTest(template=template):
                self.assertEqual(logbuffer.mask_query_secrets(template.format(RIGHT)), logbuffer.mask_query_secrets(template.format(WRONG)))


# ----- N16 and the events drain -------------------------------------------------------------------

class RunMessagesTest(unittest.TestCase):

    def test_a_store_too_deep_to_read_is_reported_as_one(self):
        cfg = _tmp(self)
        os.makedirs(os.path.join(cfg, ".storage"))
        depth = 5000
        with open(os.path.join(cfg, ".storage", "http"), "w", encoding="utf-8") as fh:
            fh.write('{"key": "http", "data": ' + '{"data": ' * depth + '{"server_port": 9999}' + "}" * depth + "}")
        with self.assertLogs(run._LOGGER, "WARNING") as logs:
            self.assertIsNone(run.drop_foreign_http_port(cfg, 8087))
        [message] = logs.output
        self.assertIn("is nested too deep to read", message)
        self.assertNotIn("unreadable store instead of", message)
        self.assertIn("for port 8087", message)

    def test_a_foreign_port_is_still_reported_with_its_number(self):
        """Pins behaviour that already held."""
        cfg = _tmp(self)
        os.makedirs(os.path.join(cfg, ".storage"))
        with open(os.path.join(cfg, ".storage", "http"), "w", encoding="utf-8") as fh:
            json.dump({"key": "http", "data": {"server_port": 8123}}, fh)
        with self.assertLogs(run._LOGGER, "WARNING") as logs:
            self.assertEqual(run.drop_foreign_http_port(cfg, 8087), 8123)
        self.assertIn("port 8123 instead of 8087", logs.output[0])

    def test_the_watchdog_says_when_timeline_events_are_left_behind(self):
        with mock.patch.object(run, "_stop_watchdog", None), mock.patch.object(run.threading, "Thread") as thread:
            run._arm_stop_watchdog(0)
        watch = thread.call_args.kwargs["target"]
        for drained, expected in ((False, 1), (True, 0)):
            with self.subTest(drained=drained):
                events = SimpleNamespace(drain=mock.Mock(return_value=drained))
                writer = SimpleNamespace(drain=mock.Mock(return_value=True))
                with mock.patch.dict(sys.modules, {"custom_components.integration_manager.events": events,
                                                   "custom_components.integration_manager.writer": writer}), \
                        mock.patch.object(run.time, "sleep"), mock.patch.object(run.logbuffer, "flush_queue", return_value=True), \
                        mock.patch.object(run.os, "_exit", side_effect=SystemExit), self.assertLogs(run._LOGGER, "CRITICAL") as logs:
                    with self.assertRaises(SystemExit):
                        watch()
                self.assertEqual(len([m for m in logs.output if "events still pending" in m]), expected, logs.output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
