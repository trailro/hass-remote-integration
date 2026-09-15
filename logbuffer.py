"""The process log on disk, one JSON object per line:
``<config>/integration_manager/process.log`` (rotated at MAX_BYTES, KEEP
older files kept).  Installed by run.py on the root logger before Home
Assistant boots, so the manager UI (/logs) shows what every integration
and HA itself logged from the first line, and the log survives a restart
or a crash.  Nothing is held in memory but a per-logger record counter.

Kept outside the custom component on purpose: run.py imports it before
/config/custom_components is importable, and the component finds the
handler by its ``query`` method, on the root logger or behind its queue.

run.py puts the root handlers (stderr and this file) behind a queue, like
HA's bootstrap does with homeassistant.util.logging: the thread that logs
(the event loop included) only enqueues, one listener thread writes.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import logging.handlers
import os
import queue
import threading
import time
from typing import Any

MAX_BYTES = 2_000_000
KEEP = 2  # process.log.1, process.log.2
_TAIL = 8192  # bytes read from the end of a file to find the last id


class FileLogHandler(logging.Handler):
    def __init__(self, path: str, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> None:
        super().__init__(level=logging.DEBUG)
        self.path = path
        self.max_bytes = max_bytes
        self.keep = keep
        self.loggers: dict[str, int] = {}  # logger name -> record count since boot
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")
        self._size = self._fh.tell()
        self._ids = itertools.count(self._last_id() + 1)  # ids keep growing across restarts

    @property
    def capacity(self) -> str:
        return f"{self.max_bytes // 1_000_000} MB × {self.keep + 1} files on disk"

    def _files(self) -> list[str]:
        """Newest first."""
        return [self.path] + [f"{self.path}.{i}" for i in range(1, self.keep + 1)]

    def _last_id(self) -> int:
        for p in self._files():
            try:
                with open(p, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    span = _TAIL
                    while True:  # a last record longer than the tail (a traceback) needs a longer look back
                        start = max(0, size - span)
                        fh.seek(start)
                        lines = fh.read(size - start).decode("utf-8", "replace").splitlines()
                        if start > 0:
                            lines = lines[1:]  # cut in the middle
                        for line in reversed(lines):
                            try:
                                return int(json.loads(line)["id"])
                            except (ValueError, KeyError, TypeError):
                                continue
                        if start == 0:
                            break
                        span *= 4
            except OSError:
                continue
        return 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        # through the queue the traceback arrives rendered (exc_text), the exception itself dropped
        exc = logging.Formatter().formatException(record.exc_info) if record.exc_info else record.exc_text
        rec = {
            "id": 0,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "levelno": record.levelno,
            "logger": record.name,
            "message": msg,
            "exc": exc,
        }
        with self.lock:
            rec["id"] = next(self._ids)  # under the lock: ids grow in file order, also when a direct write at exit meets the listener
            data = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            self.loggers[record.name] = self.loggers.get(record.name, 0) + 1
            try:
                if self._size + len(data) > self.max_bytes:
                    self._rotate()
                if self._fh.closed:
                    self._fh = open(self.path, "a", encoding="utf-8")
                self._fh.write(data)
                self._fh.flush()
                self._size += len(data.encode("utf-8"))
            except (OSError, ValueError):
                pass  # a full disk or a failed reopen must never take the process (or a caller's log line) down

    def _rotate(self) -> None:
        try:
            self._fh.close()
            for i in range(self.keep, 0, -1):
                src = self.path if i == 1 else f"{self.path}.{i - 1}"
                if os.path.exists(src):
                    os.replace(src, f"{self.path}.{i}")
        finally:
            self._fh = open(self.path, "a", encoding="utf-8")  # always an open file afterwards, rotated or not
            self._size = self._fh.tell()

    def close(self) -> None:
        with self.lock:
            try:
                self._fh.close()
            except OSError:
                pass
        super().close()

    def query(
        self,
        *,
        prefixes: tuple[str, ...] = (),
        min_level: int = logging.DEBUG,
        text: str = "",
        since_id: int = 0,
        limit: int = 500,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Matching records, chronological.  Without since_id: the newest
        `limit`.  With since_id (a follower catching up): the OLDEST `limit`
        newer than since_id, so nothing is skipped; truncated=True tells the
        client to poll again immediately.  Reads from the end, so the usual
        page load parses only the tail of the newest file."""
        text = text.lower()
        out: list[dict[str, Any]] = []
        truncated = False
        done = False
        for p in self._files():
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a line still being written, or a torn one
                if rec.get("id", 0) <= since_id:
                    done = True
                    break
                if rec.get("levelno", 0) < min_level:
                    continue
                if prefixes and not str(rec.get("logger", "")).startswith(prefixes):
                    continue
                if text and text not in str(rec.get("message", "")).lower() and text not in str(rec.get("logger", "")).lower():
                    continue
                out.append(rec)
                if since_id == 0 and len(out) >= limit:
                    done = True
                    break
            if done:
                break
        out.reverse()
        if since_id and len(out) > limit:
            out, truncated = out[:limit], True
        return out, truncated


def install(path: str) -> FileLogHandler:
    if isinstance(existing := find(), FileLogHandler):
        return existing
    handler = FileLogHandler(path)
    logging.getLogger().addHandler(handler)
    return handler


def find() -> FileLogHandler | None:
    for h in logging.getLogger().handlers:
        listener = getattr(h, "listener", None)
        for candidate in (listener.handlers if listener is not None else (h,)):
            if hasattr(candidate, "query") and hasattr(candidate, "loggers"):
                return candidate  # type: ignore[return-value]
    return None


class _QueueListener(logging.handlers.QueueListener):
    def handle(self, record: Any) -> None:
        if isinstance(record, threading.Event):
            record.set()  # a flush marker: everything queued before it is written
            return
        super().handle(record)

    def stop(self, timeout: float | None = None) -> bool:  # the stock stop joins without a timeout
        thread = self._thread
        if thread is None:
            return True
        self.enqueue_sentinel()
        thread.join(timeout)
        if thread.is_alive():
            return False
        self._thread = None
        return True


class _QueueHandler(logging.handlers.QueueHandler):
    """The message is rendered on the logging thread, like the stock handler
    (its arguments may change afterwards), but the traceback stays apart:
    the stock prepare folds it into the message, process.log keeps both
    fields."""

    listener: _QueueListener

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        record = copy.copy(record)
        try:
            record.message = record.getMessage()
        except Exception:  # noqa: BLE001
            record.message = str(record.msg)
        record.msg, record.args = record.message, None
        if record.exc_info:
            record.exc_text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            record.exc_info = None  # the traceback objects hold frames: not kept alive in the queue
        return record

    def handle(self, record: logging.LogRecord) -> Any:
        # no handler lock: the queue is thread-safe (HomeAssistantQueueHandler skips it too)
        rv = self.filter(record)
        if isinstance(rv, logging.LogRecord):
            record = rv
        if rv:
            self.emit(record)
        return rv


def _queue_handler(logger: logging.Logger) -> _QueueHandler | None:
    return next((h for h in logger.handlers if isinstance(h, _QueueHandler)), None)


def activate_queue(logger: logging.Logger | None = None) -> None:
    """Move the logger's handlers (root: stderr and process.log) behind a
    queue.  The swap is one assignment, not atomic against another thread
    logging at that moment: call it while one thread logs (run.py, before HA
    boots)."""
    logger = logger or logging.getLogger()
    if _queue_handler(logger) is not None:
        return
    q: queue.SimpleQueue = queue.SimpleQueue()
    handler = _QueueHandler(q)
    handler.listener = _QueueListener(q, *logger.handlers, respect_handler_level=True)
    handler.listener.start()
    logger.handlers = [handler]


def flush_queue(timeout: float = 5.0, logger: logging.Logger | None = None) -> bool:
    """Wait, at most `timeout`, until what was queued so far is written."""
    handler = _queue_handler(logger or logging.getLogger())
    if handler is None:
        return True
    done = threading.Event()
    handler.queue.put_nowait(done)
    return done.wait(timeout)


def stop_queue(timeout: float = 5.0, logger: logging.Logger | None = None) -> bool:
    """Back to the handlers themselves, what was queued written first (bounded):
    run.py before os._exit, so lines logged afterwards are written at once.
    False when a handler kept the listener from finishing (a blocked stderr)."""
    logger = logger or logging.getLogger()
    handler = _queue_handler(logger)
    if handler is None:
        return True
    flushed = flush_queue(timeout, logger)
    logger.handlers = [h for h in logger.handlers if h is not handler] + list(handler.listener.handlers)
    return handler.listener.stop(timeout if flushed else 0.1) and flushed
