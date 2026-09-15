"""Tracebacks still reach process.log when the handlers sit behind the log queue."""

import json
import logging
import os
import tempfile
import threading
import unittest

import logbuffer


class QueuedExceptionTest(unittest.TestCase):
    def test_traceback_written_through_the_queue(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "integration_manager", "process.log")
            logger = logging.getLogger("hri.test.queue.exc")
            logger.propagate = False
            logger.setLevel(logging.DEBUG)
            handler = logbuffer.FileLogHandler(path)
            logger.handlers = [handler]
            logbuffer.activate_queue(logger)
            try:
                def worker():
                    try:
                        1 / 0
                    except ZeroDivisionError:
                        logger.exception("boom in %s", "a thread")

                t = threading.Thread(target=worker)
                t.start()
                t.join()
                self.assertTrue(logbuffer.flush_queue(5, logger))
            finally:
                self.assertTrue(logbuffer.stop_queue(5, logger))
                handler.close()
            with open(path, encoding="utf-8") as fh:
                rec = json.loads(fh.read().splitlines()[-1])
            self.assertEqual(rec["message"], "boom in a thread")
            self.assertIn("ZeroDivisionError", rec["exc"] or "")
            self.assertIn("Traceback", rec["exc"] or "")


if __name__ == "__main__":
    unittest.main()
