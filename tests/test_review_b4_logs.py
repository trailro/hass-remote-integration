"""Review of b4cd1a1.  S5-4: a log read that met a rotation of process.log returned records twice: the file read as
process.log was read again as process.log.1 (without since_id), or opened twice under both names (with since_id).
GET /api/logs clamped `limit` instead of refusing it: limit=0 answered 500 records, limit=2001 a page of 2000."""

import asyncio
import logging
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import make_mocked_request

import logbuffer
from custom_components.integration_manager import logs_page


class RotationDuringAReadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.handler = logbuffer.FileLogHandler(os.path.join(tmp, "process.log"), max_bytes=10**6, keep=3)
        self.addCleanup(self.handler.close)
        for i in range(10):
            self.handler.emit(logging.LogRecord("demo", logging.INFO, __file__, 1, f"line {i}", None, None))

    def _rotate(self):
        with self.handler.lock:  # what the listener thread does when process.log is full
            self.handler._rotate()

    def _ids(self, **query):
        return [r["id"] for r in self.handler.query(**query)[0]]

    def test_the_newest_page(self):
        real, calls = logbuffer._read_lines, []

        def read(path):
            lines = real(path)
            calls.append(path)
            if len(calls) == 1:
                self._rotate()
            return lines

        with mock.patch.object(logbuffer, "_read_lines", read):
            ids = self._ids(limit=100)
        self.assertEqual(ids, list(range(1, 11)))

    def test_a_follower(self):
        """since_id sits in process.log.1: process.log is opened, the rotation lands, and the same file is opened again
        as process.log.1 before the one holding since_id."""
        self._rotate()
        for i in range(5):
            self.handler.emit(logging.LogRecord("demo", logging.INFO, __file__, 1, f"after {i}", None, None))
        real, calls = logbuffer._first_id, []

        def first_id(fh):
            calls.append(fh.name)
            if len(calls) == 1:
                self._rotate()
            return real(fh)

        with mock.patch.object(logbuffer, "_first_id", first_id):
            ids = self._ids(since_id=3, limit=100)
        self.assertEqual(ids, list(range(4, 16)))
        self.assertEqual(len(calls), 3)  # the rotation did make the read open the newest records twice

    def test_without_a_rotation_nothing_changes(self):
        self._rotate()
        for i in range(3):
            self.handler.emit(logging.LogRecord("demo", logging.INFO, __file__, 1, f"after {i}", None, None))
        self.assertEqual(self._ids(limit=100), list(range(1, 14)))
        self.assertEqual(self._ids(since_id=5, limit=100), list(range(6, 14)))
        self.assertEqual(self._ids(limit=4), list(range(10, 14)))


class LimitRangeTest(unittest.TestCase):
    def test_outside_1_to_2000_is_refused(self):
        async def job(fn, *args):
            return fn(*args)

        handler = SimpleNamespace(capacity=0, path="p", query=lambda **kw: ([], False))
        view = logs_page.LogsApiView(SimpleNamespace(async_add_executor_job=job))
        with mock.patch.object(logs_page.logbuffer, "find", return_value=handler):
            for value, status in (("0", 400), ("-1", 400), ("2001", 400), ("x", 400), ("1", 200), ("2000", 200), ("", 200)):
                with self.subTest(limit=value):
                    req = make_mocked_request("GET", f"/api/logs?limit={value}", headers={"X-Requested-With": "fetch"})
                    self.assertEqual(asyncio.run(view.get(req)).status, status)


if __name__ == "__main__":
    unittest.main()
