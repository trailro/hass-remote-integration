"""The Logs page after the tenth review.

F17: the follow cursor (and truncated, and the time an answer took) came from
the records the raw search matched.  FileLogHandler.query searched the raw
message before it asked the page's masked search, and the page's predicate
moved the cursor for every record it was asked about, so a search for
``pin_code=5`` over ``connection pin_code=572931`` (id 42) answered ``[]`` with
cursor 42 and ``pin_code=6`` answered ``[]`` with cursor 0: a right guess was
confirmed, and a numeric PIN could be walked out digit by digit.  Every test
here builds the same log twice, or asks the same log twice, and differs only
inside the masked value: every field of the two answers must be equal.

F19: a crash that cut the last record of process.log short left a line
without a newline; the handler appended the first record after the restart to
it, one more line no query can read, so the first record after a crash - the
one that says what happened - was never shown.

Every test fails on the tree before the fix unless its docstring says it pins
behaviour that already held.
"""

import json
import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

import logbuffer
from custom_components.integration_manager import logs_page
from tests.test_log_follow import _Log

PIN = "572931"  # synthetic
PAGE = 200


class _SamePathLog(_Log):
    """A real process log at a path shared by the paired fixtures, so the
    answers can be compared whole (the path is one of their fields)."""

    def __init__(self, test, root):
        self.handler = logbuffer.FileLogHandler(os.path.join(root, "process.log"))


class _Pairs(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _answers(self, lines, params):
        """Each answer to `params` (a list of query dicts) over a fresh log of `lines`."""
        for name in os.listdir(self.root):
            os.remove(os.path.join(self.root, name))
        log = _SamePathLog(self, self.root)
        try:
            for line in lines:
                log.write(line)
            return [log.api(**p) for p in params]
        finally:
            log.handler.close()

    def assertSameAnswers(self, a, b):
        self.assertEqual(sorted(a), sorted(b))
        for field in a:
            self.assertEqual(a[field], b[field], field)


class GuessInsideAMaskedValueTest(_Pairs):

    ROUTINE = [f"routine poll {i}" for i in range(41)]

    def _pair(self, right, wrong, lines=None, **params):
        lines = self.ROUTINE + [f"connection pin_code={PIN}"] if lines is None else lines
        a, b = self._answers(lines, [dict(params, q=right), dict(params, q=wrong)])
        self.assertSameAnswers(a, b)
        return a

    def test_the_reviewers_case_on_a_reset(self):
        a = self._pair("pin_code=5", "pin_code=6", level="DEBUG", limit=PAGE, since_id=0)
        self.assertEqual(a["records"], [])

    def test_the_reviewers_case_on_a_follow(self):
        a = self._pair("pin_code=5", "pin_code=6", level="DEBUG", limit=PAGE, since_id=41)
        self.assertEqual(a["records"], [])
        self.assertEqual(a["cursor"], 42)  # the follow still moves past the record it was not shown

    def test_the_pin_cannot_be_walked_out_digit_by_digit(self):
        for n in range(1, len(PIN) + 1):
            right = "pin_code=" + PIN[:n]
            wrong = "pin_code=" + PIN[:n - 1] + str((int(PIN[n - 1]) + 1) % 10)
            for since_id in (0, 41):
                with self.subTest(right=right, since_id=since_id):
                    self._pair(right, wrong, level="DEBUG", limit=PAGE, since_id=since_id)

    def test_the_same_guess_over_two_pins(self):
        """The other pairing: one guess, two logs that differ only inside the value."""
        for since_id in (0, 41):
            with self.subTest(since_id=since_id):
                a, = self._answers(self.ROUTINE + [f"connection pin_code={PIN}"], [dict(level="DEBUG", q="pin_code=5", limit=PAGE, since_id=since_id)])
                b, = self._answers(self.ROUTINE + ["connection pin_code=672931"], [dict(level="DEBUG", q="pin_code=5", limit=PAGE, since_id=since_id)])
                self.assertSameAnswers(a, b)

    def test_truncated_does_not_count_the_matches_inside_masked_values(self):
        """A full page followed by a record that matched only raw: truncated was
        true for the right guess and false for the wrong one."""
        lines = ["boot", "limits =5 and =6 apply", f"connection pin_code={PIN}"]
        a = self._pair("=5", "=6", lines=lines, level="DEBUG", limit=1, since_id=1)
        self.assertEqual([r["message"] for r in a["records"]], ["limits =5 and =6 apply"])
        self.assertEqual(a["cursor"], 2)

    def test_many_masked_values_and_a_real_match(self):
        """Pins behaviour that already held: a match outside the masked values
        is shown, past a page of records that match only inside them."""
        lines = ["boot"] + [f"login {i} pin_code={PIN}" for i in range(300)] + ["limits =5 and =6 apply"]
        for since_id, limit in ((0, PAGE), (1, PAGE), (0, 20), (1, 20)):
            with self.subTest(since_id=since_id, limit=limit):
                a = self._pair("=5", "=6", lines=lines, level="DEBUG", limit=limit, since_id=since_id)
                self.assertEqual([r["message"] for r in a["records"]], ["limits =5 and =6 apply"])
                self.assertEqual(a["cursor"], 302)

    def test_the_same_records_are_masked_for_a_right_and_a_wrong_guess(self):
        """The time an answer takes: the handler asked the masked search only
        about the records the raw search matched, so a right guess masked every
        record holding the PIN (~530 ms over a full log) and a wrong one none
        (~40 ms).  Counted here instead of timed."""
        lines = ["boot"] + [f"connection {i} pin_code={PIN}" if i % 3 == 0 else f"routine poll {i}" for i in range(600)]
        for since_id in (0, 1):
            with self.subTest(since_id=since_id):
                masked = {}
                real = logs_page.scrub_lines
                for guess in ("pin_code=5", "pin_code=6"):
                    calls = masked[guess] = []
                    with mock.patch.object(logs_page, "scrub_lines", side_effect=lambda texts, calls=calls: calls.append(list(texts)) or real(texts)):
                        self._answers(lines, [dict(level="DEBUG", q=guess, limit=PAGE, since_id=since_id)])
                self.assertEqual(len(masked["pin_code=5"]), len(masked["pin_code=6"]))
                self.assertEqual(masked["pin_code=5"], masked["pin_code=6"])


class FollowStillAdvancesTest(unittest.TestCase):
    """F13's follow, which moved past records it examined and did not show,
    moved only for a guess that matched inside the masked value: a wrong
    guess left the cursor where the follower was."""

    def test_an_empty_answer_with_either_guess_moves_the_cursor(self):
        log = _Log(self)
        for i in range(10):
            log.write(f"login {i} pin_code={PIN}")
        for guess in ("pin_code=5", "pin_code=6"):
            with self.subTest(guess=guess):
                r = log.api(level="DEBUG", q=guess, limit=PAGE, since_id=3)
                self.assertEqual((r["records"], r["cursor"], r["truncated"]), ([], 10, False))


# ----- F19 ----------------------------------------------------------------------------------------

class TornTailTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "process.log")

    def _crash(self, tail: bytes):
        first = {"id": 1, "ts": "2026-09-16T10:00:00.000", "level": "INFO", "levelno": 20, "logger": "probe", "message": "before", "exc": None}
        with open(self.path, "wb") as fh:
            fh.write(json.dumps(first).encode() + b"\n" + tail)

    def _restart_and_log(self, *messages):
        handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(handler.close)
        for message in messages:
            handler.emit(logging.LogRecord("probe", logging.WARNING, __file__, 0, message, None, None))
        return handler.query()[0]

    def test_the_first_record_after_a_crash_is_readable(self):
        for tail in (b'{"id":2,"message":"torn', b'{"id": 2, "ts": "2026-09-16T10:00:01.000", "message": "caf\xc3'):
            with self.subTest(tail=tail):
                self._crash(tail)
                recs = self._restart_and_log("first after the restart", "second")
                self.assertEqual([r["message"] for r in recs], ["before", "first after the restart", "second"])

    def test_ids_keep_growing_past_the_torn_record(self):
        self._crash(b'{"id":2,"message":"torn')
        recs = self._restart_and_log("first after the restart", "second")
        self.assertEqual([r["id"] for r in recs], [1, 3, 4])

    def test_a_follower_past_the_torn_record_sees_the_next_one(self):
        self._crash(b'{"id":2,"message":"torn')
        handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(handler.close)
        handler.emit(logging.LogRecord("probe", logging.WARNING, __file__, 0, "first after the restart", None, None))
        self.assertEqual([r["message"] for r in handler.query(since_id=2)[0]], ["first after the restart"])

    def test_a_whole_last_line_is_left_as_it_is(self):
        """Pins behaviour that already held."""
        self._crash(b"")
        size = os.path.getsize(self.path)
        handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(handler.close)
        self.assertEqual(os.path.getsize(self.path), size)
        self.assertEqual(next(handler._ids), 2)  # noqa: SLF001

    def test_an_empty_file_is_left_as_it_is(self):
        """Pins behaviour that already held."""
        open(self.path, "wb").close()
        handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(handler.close)
        self.assertEqual(os.path.getsize(self.path), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
