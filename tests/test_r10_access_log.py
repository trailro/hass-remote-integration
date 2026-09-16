"""R10: the log searches wrote what they searched for into the log.

Home Assistant's http server logs every request through ``aiohttp.access``
at INFO, request line included, and run.py sends that logger to
process.log and to stderr (the container log).  The Logs and Log files pages
search with ``?q=<text>``: a user checking whether a secret was logged
searched for it, and wrote it in clear into process.log, from where the Logs
page showed it and the diagnostics zip packed it.  ``q`` is not a secret name,
so the scrubber left it alone.  A credential a client puts in any URL
(``authSig``, HA's signed paths; an ``access_token``) went the same way.

The values are now masked before a record is written, and the scrubber masks
them in lines an older version wrote.  Every test here fails on the tree
before the fix.
"""

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from aiohttp.web_log import AccessLogger

import logbuffer
from custom_components.integration_manager import diagnostics, logs_page

SECRET = "HRI10C-SYNTH-7f3a9c"  # synthetic
TOKEN = "HRI10C-TOKEN-b21e"
SIG = "HRI10C-SIG-9d0f"
FILE_ID = "0123456789abcdef0123456789abcdef"


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _logger(test, name, *handlers):
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.handlers = list(handlers)
    test.addCleanup(setattr, logger, "handlers", [])
    return logger


def _access(logger, url):
    """One request line, the way HA's http server logs it (aiohttp's default access log format)."""
    request = make_mocked_request("GET", url, headers={"Host": "10.0.0.2:8213", "User-Agent": "probe/1",
                                                       "Referer": "http://10.0.0.2:8213/logs"})
    AccessLogger(logger, AccessLogger.LOG_FORMAT).log(request, web.Response(text="{}"), 0.01)


def _messages(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line)["message"] for line in fh.read().splitlines()]


def _search_urls(text):
    return ("/api/logs?" + urlencode({"level": "DEBUG", "q": text, "limit": 200, "since_id": 0}),
            "/api/log_files/tail?" + urlencode({"id": FILE_ID, "lines": 500, "q": text}))


class WrittenMaskedTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(_tmp(self), "process.log")
        self.handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(self.handler.close)

    def test_the_search_text_never_reaches_process_log(self):
        logger = _logger(self, "hri.test.r10.access", self.handler)
        for url in _search_urls(SECRET) + ("/api/log_files/tail?" + urlencode({"file": f"logs/{SECRET}.log", "lines": 5}),):
            _access(logger, url)
        messages = _messages(self.path)
        self.assertEqual(len(messages), 3)
        self.assertFalse([m for m in messages if SECRET in m], messages)
        # the rest of the line stays: client, method, path, the parameters that hold no text, status, size, referer
        self.assertRegex(messages[0], r'^\S+ \[[^\]]+\] "GET /api/logs\?level=DEBUG&q=\*\*\*&limit=200&since_id=0 HTTP/1\.1" 200 \d+ '
                                      r'"http://10\.0\.0\.2:8213/logs" "probe/1"$')
        self.assertIn(f'"GET /api/log_files/tail?id={FILE_ID}&lines=500&q=*** HTTP/1.1" 200', messages[1])
        self.assertIn('"GET /api/log_files/tail?file=***&lines=5 HTTP/1.1" 200', messages[2])

    def test_the_container_log_does_not_get_it_either(self):
        """run.py puts stderr and process.log behind the queue: both are written from the masked record."""
        stream = io.StringIO()
        stderr = logging.StreamHandler(stream)
        stderr.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logger = _logger(self, "hri.test.r10.queued", stderr, self.handler)
        logbuffer.activate_queue(logger)
        try:
            for url in _search_urls(SECRET):
                _access(logger, url)
            # HA's http security filter logs the raw path of a request it refuses
            logger.warning("Filtered a request with a potential harmful query string: %s",
                           f"/api/logs?level=DEBUG&q=<script>{SECRET}&{SECRET}</script>")
            self.assertTrue(logbuffer.flush_queue(5, logger))
        finally:
            self.assertTrue(logbuffer.stop_queue(5, logger))
        self.assertNotIn(SECRET, stream.getvalue())
        self.assertIn("GET /api/logs?level=DEBUG&q=***&limit=200&since_id=0 HTTP/1.1", stream.getvalue())
        messages = _messages(self.path)
        self.assertEqual(len(messages), 3)
        self.assertFalse([m for m in messages if SECRET in m], messages)
        self.assertEqual(messages[2], "Filtered a request with a potential harmful query string: /api/logs?level=DEBUG&q=***&***")

    def test_a_credential_in_any_url(self):
        logger = _logger(self, "hri.test.r10.credential", self.handler)
        _access(logger, "/api/status?" + urlencode({"access_token": TOKEN, "authSig": SIG, "domain": "probe"}))
        [message] = _messages(self.path)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn(SIG, message)
        self.assertIn('"GET /api/status?access_token=***&authSig=***&domain=probe HTTP/1.1" 200', message)

    def test_the_line_is_the_same_for_a_right_and_a_wrong_guess(self):
        logger = _logger(self, "hri.test.r10.guess", self.handler)
        for text in (SECRET, "HRI10C-WRONG-guess"):
            for url in _search_urls(text):
                _access(logger, url)
        without_time = [re.sub(r"\[[^\]]+\]", "[t]", m) for m in _messages(self.path)]
        self.assertEqual(without_time[:2], without_time[2:])


class OlderLinesMaskedTest(unittest.TestCase):
    """process.log lines written before the fix: the Logs page, its search and the zip mask them."""

    def setUp(self):
        self.path = os.path.join(_tmp(self), "process.log")
        lines = [
            {"message": "boot"},
            {"logger": "aiohttp.access", "message": f'172.17.0.1 [16/Sep/2026:13:24:51 +0000] "GET /api/logs?level=DEBUG&q={SECRET}'
                                                    f'&limit=200&since_id=0 HTTP/1.1" 200 540 "-" "curl/8.7.1"'},
            {"logger": "aiohttp.access", "message": f'172.17.0.1 [16/Sep/2026:13:24:52 +0000] "GET /api/log_files/tail?id=&lines=500'
                                                    f'&q={SECRET} HTTP/1.1" 200 504 "-" "curl/8.7.1"'},
            {"logger": "aiohttp.access", "message": f'172.17.0.1 [16/Sep/2026:13:24:53 +0000] "GET /api/status?authSig={SIG} HTTP/1.1" '
                                                    f'200 1288 "-" "curl/8.7.1"'},
            {"message": "after"},
        ]
        with open(self.path, "w", encoding="utf-8") as fh:
            for i, rec in enumerate(lines, 1):
                fh.write(json.dumps({"id": i, "ts": "2026-09-16T13:24:50.000", "level": "INFO", "levelno": 20,
                                     "logger": "custom_components.probe", "exc": None, **rec}) + "\n")
        self.handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(self.handler.close)

    def api(self, **params):
        view = logs_page.LogsApiView(SimpleNamespace(async_add_executor_job=_job))
        request = make_mocked_request("GET", "/api/logs?" + urlencode({"level": "DEBUG", "limit": 200, "since_id": 0, **params}),
                                      headers={"Host": "10.0.0.2:8213", "X-Requested-With": "fetch"})
        with mock.patch.object(logs_page.logbuffer, "find", return_value=self.handler):
            resp = asyncio.run(view.get(request))
        self.assertEqual(resp.status, 200)
        return json.loads(resp.body)

    def test_the_logs_page_masks_them(self):
        messages = [r["message"] for r in self.api()["records"]]
        self.assertEqual(len(messages), 5)
        self.assertNotIn(SECRET, "\n".join(messages))
        self.assertNotIn(SIG, "\n".join(messages))
        self.assertIn('"GET /api/logs?level=DEBUG&q=***&limit=200&since_id=0 HTTP/1.1" 200 540', messages[1])
        self.assertIn('"GET /api/log_files/tail?id=&lines=500&q=*** HTTP/1.1" 200 504', messages[2])
        self.assertIn('"GET /api/status?authSig=*** HTTP/1.1" 200 1288', messages[3])

    def test_a_search_for_the_text_answers_like_a_wrong_guess(self):
        right, wrong = self.api(q=SECRET), self.api(q="HRI10C-WRONG-guess")
        self.assertEqual(right["records"], [])
        self.assertEqual(right, wrong)
        self.assertEqual(self.api(q=SECRET[:8]), self.api(q="HRI10C-W"))

    def test_the_diagnostics_zip_masks_them(self):
        records, _ = self.handler.query(limit=1000)
        text = diagnostics.log_records_text(records)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(SIG, text)
        self.assertIn("GET /api/logs?level=DEBUG&q=***&limit=200&since_id=0 HTTP/1.1", text)
        with open(self.path, encoding="utf-8") as fh:
            self.assertNotIn(SECRET, diagnostics.scrub_text(fh.read()))


class SearchAnswerTest(unittest.TestCase):
    """The masking above is only worth something if a search cannot tell what it hides.

    The Logs page searches the masked text and answers with the records and a
    cursor, the newest id it decided on.  The handler asked the page only about
    records whose raw text held the search, so a search matching inside a masked
    value moved the cursor (and could truncate a follow page) where a wrong guess
    did not: an old ``q=<secret>`` access line, or any ``password=...``, could be
    read out one character at a time from the cursor alone."""

    RIGHT, WRONG = "hunter2needle", "zzzzwrongzzzz"

    def setUp(self):
        self.path = os.path.join(_tmp(self), "process.log")
        messages = ["boot", f"seen {self.RIGHT} {self.WRONG}", f"login password={self.RIGHT} ok",
                    f'"GET /api/logs?level=DEBUG&q={self.RIGHT}&limit=200&since_id=0 HTTP/1.1" 200 540', "after"]
        with open(self.path, "w", encoding="utf-8") as fh:
            for i, message in enumerate(messages, 1):
                fh.write(json.dumps({"id": i, "ts": "2026-09-16T13:24:50.000", "level": "INFO", "levelno": 20,
                                     "logger": "custom_components.probe", "message": message, "exc": None}) + "\n")
        self.handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(self.handler.close)

    api = OlderLinesMaskedTest.api

    def test_right_and_wrong_guesses_get_the_same_answer(self):
        for params in ({"since_id": 0}, {"since_id": 1}, {"since_id": 1, "limit": 1}, {"since_id": 0, "limit": 1}):
            for size in (len(self.RIGHT), 7, 3):
                with self.subTest(size=size, **params):
                    right, wrong = self.api(q=self.RIGHT[:size], **params), self.api(q=self.WRONG[:size], **params)
                    self.assertEqual([r["id"] for r in right["records"]], [2])
                    self.assertEqual(right, wrong)

    def test_a_record_that_does_not_match_is_never_returned(self):
        asked = []

        def keep(rec):
            asked.append(rec["id"])
            return True  # a caller that says yes to everything still gets only records that hold the text

        records, truncated = self.handler.query(text="seen", limit=10, keep=keep)
        self.assertEqual(([r["id"] for r in records], truncated), ([2], False))
        self.assertEqual(asked, [5, 4, 3, 2, 1])
        records, truncated = self.handler.query(text="seen", since_id=1, limit=10, keep=keep)
        self.assertEqual(([r["id"] for r in records], truncated), ([2], False))


if __name__ == "__main__":
    unittest.main()
