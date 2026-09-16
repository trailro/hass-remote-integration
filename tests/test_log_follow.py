"""The log pages after the searches moved onto masked text.

F13: the Logs page follows with since_id, and advanced its cursor only from
the records an answer returned.  The handler picks the oldest `limit` raw
matches and the page drops those whose match the mask removed, so a page of
``password=needle`` records came back empty and the same page was asked for
on every poll: the real match after it never showed.

F16: the Log files page used the masked name as a file's identity, and two
names that mask to the same text (``session-token=alpha`` and ``-beta``)
always opened the same file.

The tests use the real FileLogHandler across page boundaries, so redaction,
filtering and cursor advancement are checked together; the older redaction
tests mock the query with truncated=False.  Every test here fails on the tree
before the fix unless its docstring says it pins behaviour that already held.
"""

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

import logbuffer
from custom_components.integration_manager import logfiles_page, logs_page
from tests.fakes import FakeInstaller

BODY1 = "MIIFSYNTHETICKEYMATERIALAAAABBBBCCCCDDDD"  # synthetic, no real key
BODY2 = "EEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLLMMMMNNNN"
PAGE = 200  # static/logs.js asks for MAX_ROWS records


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _get(url):
    return make_mocked_request("GET", url, headers={"Host": "10.0.0.2:8196", "X-Requested-With": "fetch"})


class _Log:
    """A real process log in a temporary directory."""

    def __init__(self, test, **kw):
        self.handler = logbuffer.FileLogHandler(os.path.join(_tmp(test), "process.log"), **kw)
        test.addCleanup(self.handler.close)

    def write(self, message, logger="custom_components.probe", level=logging.INFO):
        self.handler.emit(logging.LogRecord(logger, level, __file__, 0, message, None, None))

    def api(self, **params):
        """GET /api/logs as the page sends it."""
        view = logs_page.LogsApiView(SimpleNamespace(async_add_executor_job=_job))
        with mock.patch.object(logs_page.logbuffer, "find", return_value=self.handler):
            resp = asyncio.run(view.get(_get("/api/logs?" + urlencode(params))))
        body = json.loads(resp.body)
        assert resp.status == 200, body
        return body

    def follow(self, q, since, polls, limit=PAGE):
        """Follow polls from lastId=since, as static/logs.js makes them."""
        shown, last = [], since
        for _ in range(polls):
            r = self.api(level="DEBUG", q=q, limit=limit, since_id=last)
            shown += r["records"]
            last = _advance(last, r)
        return shown, last


def _advance(last, answer):
    """lastId after an answer, the way fetchLogs moves it: past every record
    shown and to the answer's cursor (test_log_pages_js runs the real
    function against these answers)."""
    return max([last, answer.get("cursor") or 0, *(x["id"] for x in answer["records"])])


# ----- F13: the follow ----------------------------------------------------------------------------

class FollowPastMaskedMatchesTest(unittest.TestCase):

    def setUp(self):
        self.log = _Log(self)

    def test_a_page_of_matches_inside_masked_values_does_not_stop_the_follow(self):
        """The reviewer's case: the follower is at record 1, records 2-201 match
        only inside a password, record 202 is the real match."""
        self.log.write("needle: first")
        r = self.log.api(level="DEBUG", q="needle", limit=PAGE, since_id=0)
        self.assertEqual([x["message"] for x in r["records"]], ["needle: first"])
        last = _advance(0, r)
        self.assertEqual(last, 1)
        for i in range(PAGE):
            self.log.write(f"login {i} password=needle")
        self.log.write("needle: actual failure")
        shown, last = self.log.follow("needle", last, polls=5)
        self.assertEqual([x["message"] for x in shown], ["needle: actual failure"])
        self.assertEqual(last, PAGE + 2)

    def test_one_follow_poll_reaches_the_real_match(self):
        """Filtered before the page is cut: the page holds the real match, not
        the 200 records the mask emptied."""
        self.log.write("needle: first")
        for i in range(PAGE):
            self.log.write(f"login {i} password=needle")
        self.log.write("needle: actual failure")
        r = self.log.api(level="DEBUG", q="needle", limit=PAGE, since_id=1)
        self.assertEqual([x["message"] for x in r["records"]], ["needle: actual failure"])
        self.assertFalse(r["truncated"])
        self.assertEqual(_advance(1, r), PAGE + 2)

    def test_the_newest_window_is_not_filled_with_matches_the_mask_removes(self):
        """A reset showed nothing when the newest `limit` raw matches were all
        inside masked values, although an older real match exists."""
        self.log.write("needle: the one real match")
        for i in range(PAGE):
            self.log.write(f"login {i} token=needle")
        r = self.log.api(level="DEBUG", q="needle", limit=PAGE)
        self.assertEqual([x["message"] for x in r["records"]], ["needle: the one real match"])

    def test_the_cursor_moves_past_an_empty_answer(self):
        """Nothing matches on the masked text: the answer is empty, and the next
        poll does not examine those records again."""
        for i in range(10):
            self.log.write(f"login {i} password=needle")
        r = self.log.api(level="DEBUG", q="needle", limit=PAGE, since_id=0)
        self.assertEqual(r["records"], [])
        self.assertEqual(r.get("cursor"), 10)
        self.log.write("needle: later")
        r = self.log.api(level="DEBUG", q="needle", limit=PAGE, since_id=_advance(0, r))
        self.assertEqual([x["message"] for x in r["records"]], ["needle: later"])

    def test_pages_of_a_follow_from_the_start_add_up_to_the_whole_log(self):
        self.log.write("needle: boot")
        first = self.log.api(level="DEBUG", q="needle", limit=20, since_id=0)
        for i in range(3 * PAGE):
            self.log.write(f"line {i} needle" if i % 7 == 0 else f"login {i} secret=needle")
        shown, last = self.log.follow("needle", _advance(0, first), polls=10, limit=20)
        shown = [x["message"] for x in first["records"] + shown]
        self.assertEqual(shown, ["needle: boot"] + [f"line {i} needle" for i in range(3 * PAGE) if i % 7 == 0])
        self.assertEqual(last, 3 * PAGE + 1)

    def test_a_filter_on_the_logger_name_still_shows_masked_records(self):
        """Pins behaviour that already held: the name is not masked, so a search
        for it keeps records whose message is."""
        self.log.write("password=needle", logger="custom_components.needle")
        r = self.log.api(level="DEBUG", q="needle", limit=PAGE)
        self.assertEqual([x["message"] for x in r["records"]], ["password=***"])


class FollowAcrossPageBoundariesTest(unittest.TestCase):
    """Redaction, the search and the cursor over a key an integration logs one
    line per record, with pages that cut the block."""

    PEM = ("before the key", "-----BEGIN PRIVATE KEY-----", BODY1, BODY2, "-----END PRIVATE KEY-----", "after the key")

    def setUp(self):
        self.log = _Log(self)
        self.log.write("boot")

    def _follow(self, q, limit):
        r = self.log.api(level="DEBUG", q=q, limit=limit, since_id=0)
        for line in self.PEM:
            self.log.write(line)
        shown, last = self.log.follow(q, _advance(0, r), polls=len(self.PEM) + 2, limit=limit)
        return r["records"] + shown, last

    def test_a_page_of_two_never_shows_the_body(self):
        """Pins behaviour that already held without a search."""
        shown, last = self._follow("", 2)
        text = "\n".join(x["message"] for x in shown)
        self.assertNotIn(BODY1, text)
        self.assertNotIn(BODY2, text)
        self.assertEqual([x["id"] for x in shown], sorted({x["id"] for x in shown}))  # once each, in order
        self.assertEqual(shown[-1]["message"], "after the key")
        self.assertEqual(last, len(self.PEM) + 1)

    def test_every_page_size_shows_every_record_once_and_no_body(self):
        """How the block's own lines read depends on how much of it one page
        holds (a whole block puts both markers on its first line), so the
        comparison is of the ids and of the lines around the block."""
        whole, _ = self._follow("", 1)
        around = {1, 2, 7}
        for limit in (2, 3, 4, 5, 7, 200):
            with self.subTest(limit=limit):
                self.log = _Log(self)
                self.log.write("boot")
                shown, _ = self._follow("", limit)
                self.assertEqual([x["id"] for x in shown], [x["id"] for x in whole])
                self.assertEqual([x["message"] for x in shown if x["id"] in around],
                                 [x["message"] for x in whole if x["id"] in around])
                text = "\n".join(x["message"] for x in shown)
                self.assertNotIn(BODY1, text)
                self.assertNotIn(BODY2, text)

    def test_a_search_for_bytes_of_the_key_finds_nothing_and_the_follow_passes_the_block(self):
        shown, last = self._follow("MIIF", 1)
        self.assertEqual(shown, [])
        self.assertGreaterEqual(last, 3)  # the cursor reached the record that held the body
        self.log.write("MIIF is a prefix, said in words")
        r = self.log.api(level="DEBUG", q="MIIF", limit=1, since_id=last)
        self.assertEqual([x["message"] for x in r["records"]], ["MIIF is a prefix, said in words"])

    def test_a_search_matching_the_markers_shows_them_and_no_body(self):
        """Pins behaviour that already held."""
        shown, _ = self._follow("key", 1)
        text = "\n".join(x["message"] for x in shown)
        self.assertNotIn(BODY1, text)
        self.assertIn("after the key", text)


class FollowCostTest(unittest.TestCase):
    """The mask runs inside the scan only on records the raw search already
    matched, and only until the page is full."""

    def test_a_search_over_a_full_log_of_masked_matches_stays_fast(self):
        """The worst case: every record of the three files (6 MB) matches the
        raw search and none matches once masked, so each is masked once."""
        log = _Log(self)
        n = 0
        while not (os.path.exists(log.handler.path + ".2")
                   and os.path.getsize(log.handler.path) > logbuffer.MAX_BYTES - 1000):
            log.write(f"login {n} password=needle from 192.0.2.{n % 250}")
            n += 1
        log.write("needle: actual failure")
        t0 = time.perf_counter()
        r = log.api(level="DEBUG", q="needle", limit=PAGE, since_id=0)
        newest = time.perf_counter() - t0
        t0 = time.perf_counter()
        f = log.api(level="DEBUG", q="needle", limit=PAGE, since_id=1)
        follow = time.perf_counter() - t0
        self.assertEqual([x["message"] for x in r["records"]], ["needle: actual failure"])
        self.assertEqual([x["message"] for x in f["records"]], ["needle: actual failure"])
        size = sum(os.path.getsize(p) for p in log.handler._files() if os.path.exists(p))  # noqa: SLF001
        self.assertLess(max(newest, follow), 5.0, f"{n} masked matches in {size / 1e6:.1f} MB of log: "
                        f"newest window {newest * 1000:.0f} ms, follow from the start {follow * 1000:.0f} ms")

    def test_a_search_without_masked_matches_costs_what_it_did(self):
        """Pins behaviour that already held: a search whose raw matches are real
        masks only the records that fill the page."""
        log = _Log(self)
        for i in range(20_000):
            log.write(f"routine line {i}")
        t0 = time.perf_counter()
        r = log.api(level="DEBUG", q="routine", limit=PAGE)
        spent = time.perf_counter() - t0
        self.assertEqual(len(r["records"]), PAGE)
        self.assertLess(spent, 1.0)


# ----- F16: the file a Log files option opens ------------------------------------------------------

class CollidingFileNamesTest(unittest.TestCase):

    ALPHA = "alphaSYNTHETIC1"
    BETA = "betaSYNTHETIC2"

    def setUp(self):
        self.cfg = _tmp(self)
        self.installer = FakeInstaller(running="demo", spec={"log_dir": "logs"})
        self.installer.settings = SimpleNamespace(data={})
        os.makedirs(os.path.join(self.cfg, "logs"))
        for token, text in ((self.ALPHA, "contents of alpha"), (self.BETA, "contents of beta")):
            with open(os.path.join(self.cfg, "logs", f"session-token={token}.log"), "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        with open(os.path.join(self.cfg, "secrets.yaml"), "w", encoding="utf-8") as fh:
            fh.write("mqtt_password: synthetic-not-listed\n")
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job,
                                    config_entries=SimpleNamespace(async_entries=lambda domain=None: []))

    def _listing(self):
        resp = asyncio.run(logfiles_page.LogFilesView(self.hass, self.installer).get(_get("/api/log_files")))
        return resp.body.decode()

    @staticmethod
    def _select(f):
        """What the page sends for an option (before the fix, the value was the masked name)."""
        return {"id": f["id"]} if "id" in f else {"file": f["name"]}

    def _tail(self, **params):
        resp = asyncio.run(logfiles_page.LogFileTailView(self.hass, self.installer).get(
            _get("/api/log_files/tail?" + urlencode(params))))
        return resp.status, resp.body.decode()

    def test_both_files_are_listed_under_the_same_label(self):
        """Pins behaviour that already held: the names are masked."""
        files = json.loads(self._listing())
        self.assertEqual([f["name"] for f in files], ["logs/session-token=***"] * 2)

    def test_each_option_opens_its_own_file(self):
        files = json.loads(self._listing())
        opened = set()
        for f in files:
            status, body = self._tail(**self._select(f))
            self.assertEqual(status, 200, body)
            opened.add("alpha" if "contents of alpha" in body else "beta" if "contents of beta" in body else body)
        self.assertEqual(opened, {"alpha", "beta"})

    def _touch(self, newer, older):
        now = time.time()
        os.utime(os.path.join(self.cfg, "logs", f"session-token={newer}.log"), (now, now))
        os.utime(os.path.join(self.cfg, "logs", f"session-token={older}.log"), (now - 60, now - 60))

    def test_the_file_an_option_opens_does_not_depend_on_the_order_of_the_listing(self):
        """The listing puts the newest file first, so which file came first
        changed with every write to either."""
        self._touch(self.ALPHA, self.BETA)
        files = json.loads(self._listing())
        first = [json.loads(self._tail(**self._select(f))[1])["lines"] for f in files]
        self._touch(self.BETA, self.ALPHA)
        again = [json.loads(self._tail(**self._select(f))[1])["lines"] for f in files]  # the options the page still holds
        self.assertEqual(again, first)
        self.assertNotEqual(first[0], first[1])

    def test_the_real_name_never_reaches_the_page(self):
        """Pins behaviour that already held, now that the page also gets an id."""
        listing = self._listing()
        tails = [self._tail(**self._select(f))[1] for f in json.loads(listing)]
        for text in [listing, *tails]:
            self.assertNotIn(self.ALPHA, text)
            self.assertNotIn(self.BETA, text)

    def test_an_id_outside_the_listing_opens_nothing(self):
        for bad in ("secrets.yaml", "../secrets.yaml", "0" * 32, getattr(logfiles_page, "_file_id", str)("secrets.yaml"), "é"):
            with self.subTest(id=bad):
                self.assertEqual(self._tail(id=bad)[0], 404)

    def test_the_real_name_does_not_confirm_a_guess(self):
        self.assertEqual(self._tail(file=f"logs/session-token={self.ALPHA}.log")[0], 404)

    def test_an_ambiguous_masked_name_is_refused_not_resolved_to_either(self):
        status, body = self._tail(file="logs/session-token=***")
        self.assertEqual(status, 409, body)
        self.assertNotIn("contents of", body)

    def test_a_masked_name_shared_by_no_other_file_still_selects_it(self):
        """Pins behaviour that already held: an API caller that names the file
        the listing shows keeps working."""
        os.remove(os.path.join(self.cfg, "logs", f"session-token={self.BETA}.log"))
        status, body = self._tail(file="logs/session-token=***")
        self.assertEqual(status, 200, body)
        self.assertIn("contents of alpha", body)


# ----- also: the Log files search asked about a password one guess at a time ---------------------

class LogFileSearchOnMaskedTextTest(unittest.TestCase):
    """_tail searched the text with key material masked and the one-line rules
    ran on the survivors, so a search inside a password returned the line
    ``password=***`` exactly while the guess matched: the oracle the search on
    masked text is there to close."""

    def setUp(self):
        self.path = os.path.join(_tmp(self), "app.log")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("2026-09-16 10:00:14 INFO [probe] login password=hunter2syn\n"
                     "2026-09-16 10:00:15 INFO [probe] Authorization: Bearer abcdefSYNTHETIC123\n"
                     "2026-09-16 10:00:16 INFO [probe] hunter2syn is also a word here\n")

    def test_a_search_inside_a_password_returns_no_masked_row(self):
        for guess in ("hunter", "hunter2s", "=hunter", "abcdefSYN"):
            with self.subTest(guess=guess):
                found, _ = logfiles_page._tail_masked(self.path, 50, guess)
                self.assertNotIn("***", "\n".join(found))

    def test_a_match_outside_the_masked_part_is_still_found(self):
        found, _ = logfiles_page._tail_masked(self.path, 50, "hunter2syn")
        self.assertEqual(found, ["2026-09-16 10:00:16 INFO [probe] hunter2syn is also a word here"])
        found, _ = logfiles_page._tail_masked(self.path, 50, "password")
        self.assertEqual(found, ["2026-09-16 10:00:14 INFO [probe] login password=***"])

    def test_the_window_is_filled_past_the_lines_the_mask_removes(self):
        with open(self.path, "a", encoding="utf-8") as fh:
            for i in range(100):
                fh.write(f"2026-09-16 10:01:00 INFO [probe] retry {i} password=hunter2syn\n")
        found, _ = logfiles_page._tail_masked(self.path, 1, "hunter2syn")
        self.assertEqual(found, ["2026-09-16 10:00:16 INFO [probe] hunter2syn is also a word here"])

    def test_a_search_that_every_line_matches_only_inside_a_password_stays_bounded(self):
        """The worst case of the extra rules: every scanned line matched, none
        once masked, up to the scan budget."""
        line = "2026-09-16 10:00:00.123 WARNING (MainThread) [custom_components.demo] retry password=needle%d\n"
        with open(self.path, "w", encoding="utf-8") as fh:
            for i in range(80_000):  # ~7 MB
                fh.write(line % i)
        logfiles_page._tail_masked(self.path, 50, "")  # warm the page cache
        t0 = time.perf_counter()
        found, scanned = logfiles_page._tail_masked(self.path, 50, "needle")
        spent = time.perf_counter() - t0
        t0 = time.perf_counter()
        logfiles_page._tail_masked(self.path, 50, "nothing matches this")
        baseline = time.perf_counter() - t0
        self.assertEqual(found, [])
        self.assertLessEqual(scanned, logfiles_page.MAX_MASKED_OUT + 1000)  # stopped by the budget, not the file's end
        self.assertLess(spent, 2.0, f"{scanned} lines matched only inside a password: {spent * 1000:.0f} ms "
                        f"(a search matching nothing: {baseline * 1000:.0f} ms)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
