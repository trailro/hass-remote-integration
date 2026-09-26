"""Review of b4cd1a1.  S5-4: a log read that met a rotation of process.log returned records twice: the file read as
process.log was read again as process.log.1 (without since_id), or opened twice under both names (with since_id)."""

import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

import logbuffer


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


if __name__ == "__main__":
    unittest.main()
