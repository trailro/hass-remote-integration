"""The web UI after the twelfth review.

m2: a log_format pattern could stall or kill the process while it compiled,
on the event loop: the regex package writes a counted repeat out once per copy,
so ``(?P<a>a{60000}){60000}`` (22 characters) ran 23 s and was killed for its
memory.  Matching had a time limit, compiling none.

m14: the log searches' q= and file= values were masked in an access line only
when its path was spelled the canonical way: ``GET /API/logs?q=hunter2``,
``//api/logs``, ``/api/logs;x`` (none of which the router takes to a log view)
went to process.log verbatim, and the pages showed them and searched them.

m15: a logout the volume refused (full, read-only) answered 500 before it
raised the session generation and before it deleted the cookie; the page went
to /login all the same, so the user took every session for ended while all of
them stayed valid.

C1: DELETE /api/flow/<id> and /api/options/<flow_id> were the only changes
without the JSON body or X-Requested-With gate.

C4: the Log files listing named a file relative to the config dir as given and
checked it against the resolved one: with the config dir reached through a
symbolic link, every name read ``../<real dir>/...`` and the ``.storage/`` and
``integration_manager/`` exclusions matched nothing.

C5: ``Authorization: bearer <password>`` was not taken for the password (the
scheme is case-insensitive), and ``HRI_PASSWORD`` from an .env file with CRLF
line ends kept the CR, which no login form or header can carry.

C9: the component's setup read state.json, settings.json, the manager device's
files and ha.json on the event loop.

Every test fails on the tree before the fix unless its docstring says it pins
behaviour that already held.
"""

import asyncio
import json
import logging
import os
import random
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import logbuffer
from custom_components.integration_manager import auth as auth_mod, diagnostics, logfiles_page, logs_page, views
from tests.fakes import FakeInstaller
from tests.test_r10_access_log import OlderLinesMaskedTest, _logger, _messages

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


# ----- m2 -----------------------------------------------------------------------------------------

README_EXAMPLE = {
    "pattern": "^(?P<time>\\S+ \\S+) (?P<level>[A-Z]+) \\((?P<thread>[^)]*)\\) \\[(?P<logger>[^\\]]+)\\] (?P<message>.*)$",
    "hide": ["thread"],
    "dim": ["time", "logger"],
    "color_by": "level",
    "colors": {"WARNING": "warn", "ERROR": "bad", "CRITICAL": "bad", "DEBUG": "muted"},
}


def _clear_cache():
    if hasattr(logfiles_page, "_compiled"):
        logfiles_page._compiled.cache_clear()


def _limit_memory():
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))


@unittest.skipIf(logfiles_page._regex is None, "the regex package is not installed here")
class PatternCompileTest(unittest.TestCase):

    def setUp(self):
        _clear_cache()

    def refused(self, pattern):
        started = time.monotonic()
        fmt, error = logfiles_page.clean_log_format({"pattern": pattern})
        took = time.monotonic() - started
        self.assertEqual(fmt, {})
        self.assertIn("repeats too much", error or "")
        self.assertLess(took, 0.2)

    def test_the_reviewers_pattern_is_refused_at_once(self):
        """In a child process with its memory capped: on the tree before the fix the compile runs out of it
        (or out of the timeout) instead of taking the container's."""
        code = ("import json, time\n"
                "from custom_components.integration_manager import logfiles_page\n"
                "t = time.monotonic()\n"
                "fmt, error = logfiles_page.clean_log_format({'pattern': '(?P<a>a{60000}){60000}'})\n"
                "print(json.dumps([fmt, error, time.monotonic() - t]))\n")
        try:
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, cwd=ROOT,
                                 env={**os.environ, "PYTHONPATH": ROOT}, preexec_fn=_limit_memory)
        except subprocess.TimeoutExpired:
            self.fail("the pattern was not refused within 30 s")
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        fmt, error, took = json.loads(out.stdout)
        self.assertEqual(fmt, {})
        self.assertIn("repeats too much", error)
        self.assertLess(took, 0.5)

    def test_nested_counts_in_every_spelling(self):
        for pattern in ("(?P<a>a{2000}){2000}",
                        "(?P<a>(?:(?:a{30}){30}){30})",
                        "(?P<a>a{1,2000}?){2000}+",  # lazy and possessive
                        "(?P<a>(?:a{120}){120}){0}",  # a repeat that makes no copy still compiles what it repeats
                        "(?x)(?P<a>a{1 0 0 0 1})",  # verbose mode: the count as the regex package reads it
                        "x(?x)(?P<a>a{1 0 0 0 1})",  # a global flag after the start
                        "(?P<a>[ab]{10001})",
                        "(?P<a>(?>(?:a|b){200}){200})"):
            with self.subTest(pattern=pattern):
                self.refused(pattern)

    def test_ordinary_formats_still_work(self):
        """Pins behaviour that already held."""
        for value in (README_EXAMPLE,
                      {"pattern": r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{3,6})?) (?P<msg>.{0,4000})$"},
                      {"pattern": r"(?x) (?P<level> [A-Z]{4,8} ) \s+ (?P<rest> .* )"},
                      {"pattern": r"(?P<a>[{]\{2}\}{2})"}):
            with self.subTest(value=value):
                fmt, error = logfiles_page.clean_log_format(value)
                self.assertIsNone(error)
                self.assertEqual(fmt["pattern"], value["pattern"])
        columns, rows, error = logfiles_page._format_lines(logfiles_page.clean_log_format(README_EXAMPLE)[0],
                                                           ["2026-09-17 10:00:00 WARNING (MainThread) [probe] hello"])
        self.assertIsNone(error)
        self.assertEqual([c["name"] for c in columns], ["time", "level", "logger", "message"])
        self.assertEqual(rows[0]["cells"], ["2026-09-17 10:00:00", "WARNING", "probe", "hello"])
        self.assertEqual(rows[0]["color"], "warn")

    def test_a_pattern_the_compile_rejects_otherwise_is_an_answer_not_an_exception(self):
        for pattern, says in (("(?aL)(?P<x>x)", "mutually incompatible"), ("(" * 700 + "(?P<x>a)" + ")" * 700, "nested too deeply")):
            with self.subTest(pattern=pattern[:20]):
                fmt, error = logfiles_page.clean_log_format({"pattern": pattern})
                self.assertEqual(fmt, {})
                self.assertIn("does not compile", error)
                self.assertIn(says, error)

    def test_a_stored_pattern_over_the_limit_shows_whole_lines(self):
        """A format saved before the limit: the page shows the lines whole and says why, without compiling it."""
        columns, rows, error = logfiles_page._format_lines({"pattern": "(?P<a>a{2000}){2000}"}, ["one line"])
        self.assertEqual((columns, rows), ([], [{"raw": "one line", "cells": None, "color": None}]))
        self.assertIn("repeats too much", error)


@unittest.skipIf(logfiles_page._regex is None, "the regex package is not installed here")
class TailCompilesInTheExecutorTest(unittest.TestCase):

    def setUp(self):
        _clear_cache()
        cfg = _tmp(self)
        with open(os.path.join(cfg, "probe.log"), "w", encoding="utf-8") as fh:
            fh.write("2026-09-17 10:00:00 WARNING (MainThread) [probe] hello\n")
        self.installer = FakeInstaller()
        self.installer.settings = SimpleNamespace(data={"log_format": README_EXAMPLE})
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=_job)

    def test_the_pattern_is_compiled_off_the_loop_and_once(self):
        threads = []
        compile_ = logfiles_page._regex.compile

        def spy(pattern, *args, **kwargs):
            threads.append(threading.current_thread() is threading.main_thread())
            return compile_(pattern, *args, **kwargs)

        request = make_mocked_request("GET", "/api/log_files/tail?" + urlencode({"lines": 5}),
                                      headers={"Host": "10.0.0.2:8222", "X-Requested-With": "fetch"})
        with mock.patch.object(logfiles_page._regex, "compile", side_effect=spy):
            for _ in range(2):
                resp = asyncio.run(logfiles_page.LogFileTailView(self.hass, self.installer).get(request))
                self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertIsNone(body["format_error"])
        self.assertEqual(body["lines"][0]["cells"], ["2026-09-17 10:00:00", "WARNING", "probe", "hello"])
        self.assertEqual(threads, [False])  # compiled once, and not on the thread the event loop runs on


# ----- m14 ----------------------------------------------------------------------------------------

SECRET = "R12W-hunter2-5c1e"  # synthetic
WRONG = "R12W-zzzzzzz-0000"
SPELLINGS = ("/API/logs", "//api/logs", "/api/logs;x", "/api//logs", "/api/./logs", "/x/../api/logs", "///api/logs/",
             "/Api/Logs;a=b/", "//10.0.0.2:8222/api/logs", "/api/log_files//tail", "/API/LOG_FILES/TAIL;jsessionid=1",
             "/api/%4Cogs;x", "http://10.0.0.2:8222//api/logs")


def _access_line(path, text, n=0):
    return f'172.17.0.1 [17/Sep/2026:10:00:{n % 60:02d} +0000] "GET {path}?level=DEBUG&q={text}&file=logs/{text}.log HTTP/1.1" 404 14 "-" "curl/8.7.1"'


class SearchPathSpellingsTest(unittest.TestCase):

    def test_every_spelling_is_masked(self):
        for path in SPELLINGS:
            with self.subTest(path=path):
                self.assertEqual(logbuffer.mask_query_secrets(_access_line(path, SECRET)),
                                 f'172.17.0.1 [17/Sep/2026:10:00:00 +0000] "GET {path}?level=DEBUG&q=***&file=*** HTTP/1.1" 404 14 "-" "curl/8.7.1"')

    def test_other_paths_are_still_left_alone(self):
        """Pins behaviour that already held."""
        for path in ("/x/api/logs", "//x/y/api/logs", "/api/logs/level", "/api/logs/loggers", "/api/hassio/app/api/logs",
                     "/api/logs/x/../../status", "/api/log_files/tail/x"):
            with self.subTest(path=path):
                line = f'"GET {path}?level=DEBUG&q=text&page=2 HTTP/1.1" 404'
                self.assertEqual(logbuffer.mask_query_secrets(line), line)

    def test_they_never_reach_process_log(self):
        path = os.path.join(_tmp(self), "process.log")
        handler = logbuffer.FileLogHandler(path)
        self.addCleanup(handler.close)
        logger = _logger(self, "hri.test.r12.access", handler)
        for i, spelling in enumerate(SPELLINGS):
            logger.info("%s", _access_line(spelling, SECRET, i))
        messages = _messages(path)
        self.assertEqual(len(messages), len(SPELLINGS))
        self.assertFalse([m for m in messages if SECRET in m], messages)

    def test_a_line_is_the_same_for_a_right_and_a_wrong_guess(self):
        for path in SPELLINGS:
            with self.subTest(path=path):
                self.assertEqual(logbuffer.mask_query_secrets(_access_line(path, SECRET)),
                                 logbuffer.mask_query_secrets(_access_line(path, WRONG)))

    def test_the_log_files_prefilter_finds_every_line_they_mask(self):
        """The Log files search skips the one-line rules for a line holding none of their literals: every line the
        rules change must hold one, or the search decides on the raw text again."""
        rng = random.Random(12)
        parts = ["/", "//", "/api", "api", "API", "Api", "%61pi", "/logs", "logs", "/log_files", "/tail", ";x", ";", ".", "..",
                 "/x", "http://h", "//h", "%2F", "?q=v1", "&file=v2", "=", "%3B", "LOGS", "l%6Fgs"]
        corpus = ["GET " + "".join(rng.choice(parts) for _ in range(rng.randint(3, 12))) + "?q=v1" for _ in range(30_000)]
        corpus += [_access_line(p, SECRET) for p in SPELLINGS]
        changed = [line for line in corpus if diagnostics._scrub_one_line_rules(line) != line]
        self.assertGreater(len(changed), 100)  # the corpus is not vacuous
        self.assertEqual([line for line in changed if not logfiles_page._rules_may_change(line)], [])


class SearchPathSpellingsInOlderLinesTest(unittest.TestCase):
    """Lines an older version wrote: the Logs page and the Log files page answer a right guess like a wrong one."""

    def setUp(self):
        cfg = _tmp(self)
        self.path = os.path.join(cfg, "process.log")
        with open(self.path, "w", encoding="utf-8") as fh, open(os.path.join(cfg, "probe.log"), "w", encoding="utf-8") as probe:
            n = 0
            for rnd in range(20):
                for spelling in SPELLINGS:
                    n += 1
                    for message in (f"routine poll {n}", _access_line(spelling, SECRET, n)):
                        fh.write(json.dumps({"id": n, "ts": "2026-09-17T10:00:00.000", "level": "INFO", "levelno": 20,
                                             "logger": "aiohttp.access", "message": message, "exc": None}) + "\n")
                        probe.write(f"2026-09-17 10:00:00 INFO [aiohttp.access] {message}\n")
                        n += 1
        self.handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(self.handler.close)
        self.installer = FakeInstaller()
        self.installer.settings = SimpleNamespace(data={})
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=_job)

    api = OlderLinesMaskedTest.api

    def tail(self, **params):
        request = make_mocked_request("GET", "/api/log_files/tail?" + urlencode(params),
                                      headers={"Host": "10.0.0.2:8222", "X-Requested-With": "fetch"})
        resp = asyncio.run(logfiles_page.LogFileTailView(self.hass, self.installer).get(request))
        return resp.status, resp.body

    def test_the_logs_page_shows_them_masked(self):
        text = "\n".join(r["message"] for r in self.api(limit=2000)["records"])
        self.assertIn("q=***&file=***", text)
        self.assertNotIn(SECRET, text)

    def test_the_logs_page_answers_a_right_guess_like_a_wrong_one(self):
        for size in (len(SECRET), 9, 6):
            for params in ({"since_id": 0}, {"since_id": 0, "limit": 5}, {"since_id": 3, "limit": 5}):
                with self.subTest(size=size, **params):
                    self.assertEqual(self.api(q=SECRET[:size], **params), self.api(q=WRONG[:size], **params))

    def test_the_log_files_page_answers_a_right_guess_like_a_wrong_one(self):
        for size in (len(SECRET), 9, 6):
            for lines in (5, 50, 5000):
                with self.subTest(size=size, lines=lines):
                    right, wrong = self.tail(lines=lines, q=SECRET[:size]), self.tail(lines=lines, q=WRONG[:size])
                    self.assertEqual(right[0], 200)
                    self.assertEqual(json.loads(right[1])["lines"], [])
                    self.assertEqual(right, wrong)  # rows, lines read, the whole body byte for byte
        status, body = self.tail(lines=5, q="q=***&file=***")
        self.assertEqual(len(json.loads(body)["lines"]), 5)  # what the mask leaves is still found


# ----- m15 ----------------------------------------------------------------------------------------

class LogoutVolumeRefusedTest(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmp(self)

    def logout(self, auth):
        request = SimpleNamespace(headers={}, query={}, content_type="application/json", json=mock.AsyncMock(return_value={}),
                                  app={"hass": SimpleNamespace(async_add_executor_job=_job)})
        return asyncio.run(auth_mod.LogoutView(auth).post(request))

    def test_the_sessions_end_and_the_answer_says_until_when(self):
        auth = auth_mod.Auth("pw", b"k" * 32, os.path.join(self.tmp, "gone", "auth_revoked"))  # the write fails: no such directory
        cookie = auth.new_session()
        self.assertTrue(auth.valid_session(cookie))
        with self.assertLogs(auth_mod._LOGGER, logging.ERROR) as logs:
            resp = self.logout(auth)
        self.assertIn("valid again after a restart", logs.output[0])
        self.assertFalse(auth.valid_session(cookie))
        self.assertTrue(auth.valid_session(auth.new_session()))
        body = json.loads(resp.body)
        self.assertFalse(body["ok"])
        self.assertIn("valid again after the container restarts", body["error"])
        for name in (auth_mod.COOKIE, auth_mod.LEGACY_COOKIE):
            self.assertIn(name, resp.cookies)
            self.assertEqual(resp.cookies[name]["max-age"], "0")
            self.assertEqual(resp.cookies[name].value, "")

    def test_a_recorded_logout_answers_as_before(self):
        """Pins behaviour that already held."""
        auth = auth_mod.Auth("pw", b"k" * 32, os.path.join(self.tmp, "auth_revoked"))
        cookie = auth.new_session()
        resp = self.logout(auth)
        self.assertEqual((resp.status, json.loads(resp.body)), (200, {"ok": True}))
        self.assertFalse(auth.valid_session(cookie))
        self.assertEqual(resp.cookies[auth_mod.COOKIE]["max-age"], "0")
        reloaded = auth_mod.Auth("pw", b"k" * 32, auth.revoked_path)
        reloaded.load_revoked()
        self.assertFalse(reloaded.valid_session(cookie))



# ----- C1 -----------------------------------------------------------------------------------------

class FlowAbortGateTest(unittest.TestCase):

    def test_an_abort_needs_the_header(self):
        for view_cls, method in ((views.FlowResourceView, "abort"), (views.OptionsResourceView, "options_abort")):
            for headers, status in (({}, 400), ({"X-Requested-With": "other"}, 400), ({"X-Requested-With": "fetch"}, 200)):
                with self.subTest(view=view_cls.__name__, headers=headers):
                    aborted = []
                    flows = SimpleNamespace(**{method: aborted.append})
                    request = make_mocked_request("DELETE", "/api/flow/F1", headers={"Host": "10.0.0.2:8222", **headers})
                    resp = asyncio.run(view_cls(flows).delete(request, flow_id="F1"))
                    self.assertEqual(resp.status, status)
                    self.assertEqual(aborted, ["F1"] if status == 200 else [])


# ----- C4 -----------------------------------------------------------------------------------------

class SymlinkedConfigDirTest(unittest.TestCase):

    def setUp(self):
        tmp = _tmp(self)
        self.real = os.path.join(tmp, "real")
        for d in (".storage", "integration_manager", "logs"):
            os.makedirs(os.path.join(self.real, d))
        for name in ("probe.log", ".storage/auth.log", "integration_manager/process.log", "logs/radio.log", "home-assistant.log"):
            with open(os.path.join(self.real, name), "w", encoding="utf-8") as fh:
                fh.write(f"2026-09-17 10:00:00 INFO [probe] {name}\n")
        self.cfg = os.path.join(tmp, "config")
        os.symlink(self.real, self.cfg)
        self.installer = FakeInstaller()
        self.installer.settings = SimpleNamespace(data={})
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job)
        self.entry_paths = [".storage/auth.log", "integration_manager/process.log", "logs/radio.log",
                            os.path.join(self.cfg, ".storage", "auth.log")]

    def test_the_names_and_the_exclusions_are_the_same_as_without_the_link(self):
        linked = logfiles_page._log_files(self.cfg, self.installer, self.entry_paths)
        direct = logfiles_page._log_files(self.real, self.installer, self.entry_paths)
        self.assertEqual(sorted(f["name"] for f in linked), ["logs/radio.log", "probe.log"])
        self.assertEqual([(f["name"], f["path"]) for f in linked], [(f["name"], f["path"]) for f in direct])

    def test_a_tail_through_the_link(self):
        request = make_mocked_request("GET", "/api/log_files/tail?" + urlencode({"file": "logs/radio.log", "lines": 5}),
                                      headers={"Host": "10.0.0.2:8222", "X-Requested-With": "fetch"})
        with mock.patch.object(logfiles_page, "_entry_paths", return_value=self.entry_paths):
            resp = asyncio.run(logfiles_page.LogFileTailView(self.hass, self.installer).get(request))
        self.assertEqual(resp.status, 200)
        self.assertEqual([row["raw"] for row in json.loads(resp.body)["lines"]], ["2026-09-17 10:00:00 INFO [probe] logs/radio.log"])


# ----- C5 -----------------------------------------------------------------------------------------

class BearerSchemeTest(unittest.TestCase):

    def setUp(self):
        from tests.test_r3_web import SessionCookieTest, _request

        self.request = _request
        self.auth, self.guard = SessionCookieTest._guard(self)

    def run_guard(self, header):
        async def handler(request):
            return web.Response(text="ok")

        return asyncio.run(self.guard(self.request(headers={"Authorization": header}, path="/api/status"), handler))

    def test_the_scheme_in_any_case(self):
        for header in ("Bearer pw", "bearer pw", "BEARER pw", "bEaReR pw"):
            with self.subTest(header=header):
                self.assertEqual(self.run_guard(header).status, 200)

    def test_a_wrong_password_in_any_case_counts_as_a_failure(self):
        self.assertEqual(self.run_guard("bearer wrong").status, 401)
        self.assertEqual(len(self.auth._failures.get("10.0.0.9", [])), 1)


class PasswordFromTheEnvironmentTest(unittest.TestCase):

    def configured(self, value):
        env = {k: v for k, v in os.environ.items() if k != "HRI_PASSWORD_FILE"}
        with mock.patch.dict(os.environ, {**env, "HRI_PASSWORD": value}, clear=True):
            return auth_mod._configured_password()

    def test_line_ends_are_dropped(self):
        for value in ("pw\r\n", "pw\n", "pw\r", "\npw"):
            with self.subTest(value=value):
                self.assertEqual(self.configured(value), ("pw", ""))
        self.assertEqual(self.configured("pw \r\n"), ("pw ", ""))
        self.assertEqual(self.configured("\r\n"), ("", ""))

    def test_spaces_are_kept(self):
        """Pins behaviour that already held."""
        self.assertEqual(self.configured(" pw "), (" pw ", ""))


# ----- C9 -----------------------------------------------------------------------------------------

class _SetupReached(Exception):
    pass


class SetupReadsOffTheLoopTest(unittest.TestCase):

    def test_the_json_files_are_read_in_the_executor(self):
        import custom_components.integration_manager as component
        from custom_components.integration_manager import auth, events, hostguard

        tmp = _tmp(self)
        on_loop = {}

        def spy(name, real):
            def call(*args, **kwargs):
                on_loop[name] = threading.current_thread() is threading.main_thread()
                return real(*args, **kwargs)
            return call

        def background_task(coro, name):
            coro.close()

        hass = SimpleNamespace(config=SimpleNamespace(config_dir=tmp, path=lambda *p: os.path.join(tmp, *p)), data={},
                               async_add_executor_job=_job, async_create_background_task=background_task)
        installer = mock.MagicMock()
        installer.announce_smoke.side_effect = _SetupReached  # the first step after the reads
        publisher = mock.MagicMock()
        publisher.async_start = mock.AsyncMock()
        old_events = events.EVENTS
        self.addCleanup(setattr, events, "EVENTS", old_events)
        with mock.patch.object(component, "Installer", side_effect=spy("Installer", lambda hass: installer)), \
                mock.patch.object(component, "ManagerDevice", side_effect=spy("ManagerDevice", lambda *a: mock.MagicMock())), \
                mock.patch.object(component.jsonio, "read_json", side_effect=spy("read_json", component.jsonio.read_json)), \
                mock.patch.object(component, "track_delayed_stores"), mock.patch.object(component.writer, "async_register"), \
                mock.patch.object(hostguard, "install_host_guard"), \
                mock.patch.object(auth, "async_setup_auth", mock.AsyncMock(return_value=SimpleNamespace(enabled=False))), \
                mock.patch.object(component, "HaUpdater"), mock.patch.object(component, "RegistryAligner"), \
                mock.patch.object(component, "async_finish_rebuild", mock.MagicMock()), mock.patch.object(component, "FlowDriver"), \
                mock.patch.object(component, "MqttPublisher", return_value=publisher), mock.patch.object(component, "Scheduler"), \
                mock.patch.object(component.notifications, "async_watch"), mock.patch.object(events, "emit"):
            with self.assertRaises(_SetupReached):
                asyncio.run(component.async_setup(hass, {}))
        self.assertEqual(on_loop, {"Installer": False, "ManagerDevice": False, "read_json": False})
