"""The web UI after the twelfth review.

m2: a log_format pattern could stall or kill the process while it compiled,
on the event loop: the regex package writes a counted repeat out once per copy,
so ``(?P<a>a{60000}){60000}`` (22 characters) ran 23 s and was killed for its
memory.  Matching had a time limit, compiling none.

Every test fails on the tree before the fix unless its docstring says it pins
behaviour that already held.
"""

import asyncio
import json
import os
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

from aiohttp.test_utils import make_mocked_request

from custom_components.integration_manager import logfiles_page
from tests.fakes import FakeInstaller

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
