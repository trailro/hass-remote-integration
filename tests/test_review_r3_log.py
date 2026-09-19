"""Fourth review, the logging findings: one log line must never be able to end
logging (a lone surrogate reaching a message did), the settings POST must arm
the scheduler for the value it saved whatever the client does, and the events
file must survive a line a crash cut short or a JSON line that is no object."""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import logbuffer
from custom_components.integration_manager import events
from custom_components.integration_manager.manage_views import SettingsView
from custom_components.integration_manager.settings import Settings

# a file name a volume from another system carries: not valid UTF-8, so Python decodes it with surrogateescape
# and an OSError's text holds a lone surrogate no UTF-8 file can take
BAD_NAME = "/data/\udcff.db"


class _Request:
    content_type = "application/json"

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)


class _Scheduler:
    def __init__(self):
        self.rearmed = 0

    def rearm(self):
        self.rearmed += 1


class SurrogateInALogLineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "process.log")
        self.log = logging.getLogger(f"hri.test.{id(self)}")
        self.log.propagate = False
        self.log.setLevel(logging.DEBUG)
        self.addCleanup(self._teardown)

    def _teardown(self):
        logbuffer.stop_queue(2.0, self.log)
        self.log.handlers = []

    def _behind_a_queue(self, *handlers):
        for h in handlers:
            self.log.addHandler(h)
        logbuffer.activate_queue(self.log)
        return self.log.handlers[0].listener

    def _alive(self, listener):
        return listener._thread is not None and listener._thread.is_alive()

    def _lines(self):
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            return [json.loads(line) for line in fh.read().splitlines() if line.strip()]

    def test_a_lone_surrogate_does_not_kill_the_listener(self):
        listener = self._behind_a_queue(logbuffer.FileLogHandler(self.path))
        self.log.error("cannot open %s", BAD_NAME)
        logbuffer.flush_queue(2.0, self.log)
        self.assertTrue(self._alive(listener), "the listener thread died on one log line")
        self.log.info("the line after the bad one")
        self.assertTrue(logbuffer.flush_queue(2.0, self.log))
        self.assertIn("the line after the bad one", [r["message"] for r in self._lines()])

    def test_the_surrogate_line_is_written_as_text_a_reader_can_take(self):
        self._behind_a_queue(logbuffer.FileLogHandler(self.path))
        self.log.error("cannot open %s", BAD_NAME)
        logbuffer.flush_queue(2.0, self.log)
        messages = [r["message"] for r in self._lines()]
        self.assertEqual(len(messages), 1, "the record itself was dropped")
        self.assertIn("\\udcff", messages[0])  # the escape as text, not a surrogate read back
        for message in messages:
            message.encode("utf-8")  # what answers /api/logs (orjson) refuses a lone surrogate

    def test_a_handler_that_raises_does_not_end_logging(self):
        class _Angry(logging.Handler):
            def emit(self, record):
                raise RuntimeError("no")

        listener = self._behind_a_queue(_Angry(), logbuffer.FileLogHandler(self.path))
        self.log.error("first")
        logbuffer.flush_queue(2.0, self.log)
        self.assertTrue(self._alive(listener), "a raising handler ended the listener thread")
        self.log.info("second")
        self.assertTrue(logbuffer.flush_queue(2.0, self.log))
        self.assertEqual([r["message"] for r in self._lines()], ["first", "second"])

    def test_the_handler_itself_survives_a_record_it_cannot_write(self):
        handler = logbuffer.FileLogHandler(self.path)
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "cannot open %s", (BAD_NAME,), None)
        handler.emit(record)  # no queue, no listener: the caller's own thread
        handler.emit(logging.LogRecord("x", logging.INFO, __file__, 2, "after", (), None))
        self.assertEqual([r["message"] for r in self._lines()][-1], "after")


class SettingsSaveFollowUpsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = Settings(self.dir)
        self.settings.data["backup_daily_hour"] = 0
        self.scheduler = _Scheduler()
        self.installer = SimpleNamespace(settings=self.settings, hass=None, _releases_cache={"x": 1},
                                         scheduler=self.scheduler)
        self.view = SettingsView(self.installer)

    def test_a_client_that_disconnects_still_rearms_the_daily_backup(self):
        # the write is shielded, so the new hour is on disk; the cancellation lands on the await in the handler
        saved = mock.AsyncMock(side_effect=asyncio.CancelledError())
        with mock.patch.object(type(self.settings), "async_save", saved):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self.view.post(_Request({"backup_daily_hour": 7})))
        self.assertEqual(self.settings.data["backup_daily_hour"], 7)
        self.assertEqual(self.scheduler.rearmed, 1, "the daily backup kept the old hour")
        self.assertFalse(self.installer._releases_cache)

    def test_a_failed_save_does_not_discard_a_concurrent_successful_one(self):
        calls = []

        async def save(_self=None):
            calls.append(1)
            if len(calls) == 1:  # the first save fails, slowly: the other request runs meanwhile
                for _ in range(10):
                    await asyncio.sleep(0)
                raise OSError("no space left on device")

        async def both():
            return await asyncio.gather(self.view.post(_Request({"backup_daily_hour": 1})),
                                        self.view.post(_Request({"backup_daily_hour": 2})))

        with mock.patch.object(type(self.settings), "async_save", save):
            first, second = asyncio.run(both())
        self.assertFalse(json.loads(first.body.decode())["ok"])
        self.assertTrue(json.loads(second.body.decode())["ok"])
        # the rolled-back request must not put back a value that was already replaced by one that was written
        self.assertEqual(self.settings.data["backup_daily_hour"], 2)
        self.assertEqual(self.scheduler.rearmed, 2)

    def test_a_failed_save_arms_the_scheduler_for_the_value_it_kept(self):
        with mock.patch.object(type(self.settings), "async_save", mock.AsyncMock(side_effect=OSError("full"))):
            out = json.loads(asyncio.run(self.view.post(_Request({"backup_daily_hour": 9}))).body.decode())
        self.assertFalse(out["ok"])
        self.assertEqual(self.settings.data["backup_daily_hour"], 0)
        self.assertEqual(self.scheduler.rearmed, 1)


class EventsFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "events.jsonl")

    def test_a_line_a_crash_cut_short_does_not_swallow_the_next_event(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-01-01T00:00:00", "kind": "boot", "message": "up"}\n')
            fh.write('{"ts": "2026-01-01T00:00:01", "kind": "start", "mess')  # the power went out here
        store = events.Events(self.path)
        store.add("install", "after the crash")
        self.assertTrue(store.drain(5))
        rows = store.recent()
        self.assertEqual([r["message"] for r in rows], ["up", "after the crash"])

    def test_a_json_line_that_is_no_object_is_skipped_when_filtering_on_kinds(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2, 3]\n")
            fh.write('{"ts": "2026-01-01T00:00:00", "kind": "boot", "message": "up"}\n')
        store = events.Events(self.path)
        self.assertEqual([r["message"] for r in store.recent(kinds=("boot",))], ["up"])

    def test_a_json_line_that_is_no_object_never_reaches_a_reader(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("12\n")
            fh.write('"a string"\n')
            fh.write('{"ts": "2026-01-01T00:00:00", "kind": "boot", "message": "up"}\n')
        rows = events.Events(self.path).recent()
        self.assertEqual(rows, [{"ts": "2026-01-01T00:00:00", "kind": "boot", "message": "up"}])


if __name__ == "__main__":
    unittest.main()
