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
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import unquote_plus

MAX_BYTES = 2_000_000
KEEP = 2  # process.log.1, process.log.2
# records waiting for the listener: bounded, so a listener stuck on a blocked stderr cannot grow memory without
# limit; a full queue drops new records and counts them, and the count is logged once the listener runs again
QUEUE_MAX = 50_000
_TAIL = 8192  # bytes read from the end of a file to find the last id
_TORN_ID = re.compile(r'\{"id":\s*(\d+)')  # the id at the start of a record whose line a crash cut short

# A URL in a log line: aiohttp.access writes the request line of every request (and its Referer), HA's http security
# filter the raw path of a request it refuses.  The log searches send what the user typed as ?q= (someone checking
# whether a secret was logged types the secret), the Log files page may be asked for a file by a name that holds what
# its mask hides, and a client may put a credential in any URL (HA's signed paths: authSig).  Those values are masked
# before a record is written, so they never reach process.log or the container log.
_URL_QUERY = re.compile(r"([^\s\"'?#]*)\?([^\s\"'#]+)")
_URL_ORIGIN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*")
_LOG_SEARCH_PATHS = ("/api/logs", "/api/log_files/tail")  # the paths themselves: not /x/api/logs, not /api/logs/level
# on the log search endpoints every value is masked but these, in the form the pages send them
_LOG_SEARCH_PLAIN = {
    "level": re.compile(r"[A-Za-z]{1,10}"),
    "limit": re.compile(r"\d{1,9}"),
    "since_id": re.compile(r"\d{1,19}"),
    "lines": re.compile(r"\d{1,9}"),
    "id": re.compile(r"[0-9a-f]{32}"),
    "prefix": re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,199}"),  # a logger name
}
# names ending in "key" that are known not to be secrets (diagnostics masks every other *key the same way)
PLAIN_KEYS = r"(?:translation|sort|primary)_key"
# elsewhere, a parameter named like a credential: a name holding one of the long words anywhere, or a word of the
# name - cut at "_", "-", ".", a digit and a camelCase hump (authSig, APIKey) - that is one of the short ones, which
# inside a longer word are ordinary (zipcode, keyword, design, monkey).  Each word holds a literal the Log files
# page's search looks for before it runs the scrubber on a line: logfiles_page._RULE_LITERALS
_CREDENTIAL_PARAM = re.compile(
    r"(?i:token|secret|passw|passphrase|credential|cookie|signature|pwd)"
    r"|(?:^|(?<=[^A-Za-z])|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z]))"
    r"(?i:pass|passcode|passkey|sig|key|apikey|code|session|sessionid)(?![a-z])")
_PLAIN_PARAM = re.compile(PLAIN_KEYS, re.I)


def _is_log_search(path: str) -> bool:
    return _URL_ORIGIN.sub("", unquote_plus(path)).rstrip("/") in _LOG_SEARCH_PATHS


def _mask_query(match: re.Match[str]) -> str:
    path, query = match.group(1), match.group(2)
    search = _is_log_search(path)
    parts = []
    for part in query.split("&"):
        name, sep, value = part.partition("=")
        if search and part and not sep:
            part = "***"  # a value with an unencoded "&" in it (the raw path HA's security filter logs)
        elif sep and value:
            name_text = unquote_plus(name)
            if search:
                plain = _LOG_SEARCH_PLAIN.get(name_text)
                if plain is None or not plain.fullmatch(value):
                    part = f"{name}=***"
            elif _CREDENTIAL_PARAM.search(name_text) and not _PLAIN_PARAM.fullmatch(name_text):
                part = f"{name}=***"
        parts.append(part)
    return f"{path}?{'&'.join(parts)}"


def mask_query_secrets(text: str) -> str:
    """``text`` with the query values above replaced by ``***``, whatever
    they hold: the rest of the line (method, path, status, size, time) is
    kept, and a masked line does not depend on what the value was."""
    if "?" not in text or "=" not in text:
        return text
    return _URL_QUERY.sub(_mask_query, text)


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
        if self._size and not self._ends_with_newline():
            # a crash cut the last record short: the next one starts on a line of its own instead of joining
            # that line, which no query can read (the torn bytes stay, skipped like any line that is not JSON)
            self._fh.write("\n")
            self._fh.flush()
            self._size += 1

    def _ends_with_newline(self) -> bool:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) == b"\n"
        except OSError:
            return True  # unreadable: nothing to repair that can be seen, and the log must still open

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
                                # a torn record: a page may have shown it before the crash cut it short, so its id is not given out again
                                if (torn := _TORN_ID.match(line)) is not None:
                                    return int(torn.group(1))
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
            "message": mask_query_secrets(msg),
            "exc": mask_query_secrets(exc) if exc else exc,
        }
        with self.lock:
            rec["id"] = next(self._ids)  # under the lock: ids grow in file order, also when a direct write at exit meets the listener
            data = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            size = len(data.encode("utf-8"))  # max_bytes and _size are bytes; a non-ASCII message has more of them than characters
            self.loggers[record.name] = self.loggers.get(record.name, 0) + 1
            try:
                if self._size + size > self.max_bytes:
                    self._rotate()
                if self._fh.closed:
                    self._fh = open(self.path, "a", encoding="utf-8")
                self._fh.write(data)
                self._fh.flush()
                self._size += size
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
        keep: Callable[[dict[str, Any]], bool] | None = None,
        cursor: Callable[[int], None] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Matching records, chronological.  Without since_id: the newest
        `limit`.  With since_id (a follower catching up): the OLDEST `limit`
        newer than since_id, so nothing is skipped; truncated=True tells the
        client to poll again immediately.  Reads from the end, so the usual
        page load parses only the tail of the newest file.  With since_id it
        reads forward from there a line at a time, and holds the page, not
        the records newer than since_id.

        ``keep`` is the caller's last word on a record that passed the other
        filters, asked before the record counts toward ``limit`` (the Logs
        page searches the masked text, which this module cannot produce: it
        is imported before the component is).  It is asked newest first
        without since_id, and oldest first with it, never about more records
        than it takes to fill the page.

        ``text`` searches the raw message.  A caller whose ``keep`` searches
        the masked text passes no ``text`` (the Logs page does): which records
        reach ``keep``, and so how far the page reaches and how long the answer
        takes, would otherwise depend on whether a guess matched inside a masked
        value.  With both ``text`` and ``keep``, a record whose text does not
        contain ``text`` is never returned, but ``keep`` is still asked about
        it, with its message and traceback left out, so what the caller learns
        from being asked is the same whether a masked value held the text or not.

        ``cursor`` is told, once, where a follower continues: the id of the
        newest record read, whatever the level and logger filters made of it
        (with since_id and a full page, of the record read before the first one
        that did not fit).  A filter that matches nothing still moves it, so a
        follower does not read the same records on every poll."""
        text = text.lower()
        out: list[dict[str, Any]] = []
        truncated = False
        newest = since_id

        def candidate(rec: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
            """(record, matched) for a record the level and logger filters let through."""
            if rec.get("levelno", 0) < min_level:
                return None
            if prefixes and not str(rec.get("logger", "")).startswith(prefixes):
                return None
            matched = not text or text in str(rec.get("message", "")).lower() or text in str(rec.get("logger", "")).lower()
            if not matched:
                if keep is None:
                    return None
                rec = {**rec, "message": "", "exc": None}
            return rec, matched

        if not since_id:
            for p in self._files():
                lines = _read_lines(p)
                done = False
                for line in reversed(lines):
                    rec = _record(line)
                    if rec is None:
                        continue  # a line still being written, or a torn one
                    if rec.get("id", 0) <= 0:
                        done = True
                        break
                    newest = max(newest, rec["id"])
                    if (c := candidate(rec)) is None or keep is not None and not keep(c[0]) or not c[1]:
                        continue
                    out.append(c[0])
                    if len(out) >= limit:
                        done = True
                        break
                if done:
                    break
            out.reverse()
        else:
            # oldest first from the line after since_id, a line at a time: what is held is the page, not the log
            files: list[Any] = []
            try:
                for p in self._files():
                    try:
                        fh = open(p, encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    files.append(fh)
                    if _first_id(fh) <= since_id:
                        break  # this file holds since_id: the older ones hold nothing newer
                for fh in reversed(files):
                    fh.seek(0)
                    for line in fh:
                        if (rid := _line_id(line)) is not None and rid <= since_id:
                            continue
                        if (rec := _record(line)) is None:
                            continue  # a line still being written (its id is not passed: it is shown once it is whole)
                        if (c := candidate(rec)) is not None:
                            if len(out) >= limit:
                                truncated = True
                                break
                            if (keep is None or keep(c[0])) and c[1]:
                                out.append(c[0])
                        newest = rec.get("id", newest)
                    if truncated:
                        break
            finally:
                for fh in files:
                    fh.close()
        if cursor is not None:
            cursor(newest)
        return out, truncated


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().split("\n")  # not splitlines: a message may hold U+2028, which json.dumps leaves as it is
    except OSError:
        return []


def _record(line: str) -> dict[str, Any] | None:
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _first_id(fh: Any) -> int:
    """The id of the first record line of an open file (0 for a file with none)."""
    for line in fh:
        if (rid := _line_id(line)) is not None:
            return rid
    return 0


def _line_id(line: str) -> int | None:
    """The id of a record line without parsing it (the handler writes the id first), None for a line that is
    no record.  Only to find where the records newer than since_id begin: a torn record's id is in file order
    like any other."""
    if (m := _TORN_ID.match(line)) is not None:
        return int(m.group(1))
    rec = _record(line)
    return None if rec is None else rec.get("id", 0)


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
    source: "_QueueHandler | None" = None

    def handle(self, record: Any) -> None:
        if isinstance(record, threading.Event):
            record.set()  # a flush marker: everything queued before it is written
            return
        super().handle(record)
        if self.source is not None and (dropped := self.source.dropped):
            self.source.dropped -= dropped
            note = logging.LogRecord(__name__, logging.WARNING, __file__, 0,
                                     "%s log records were dropped: the log queue was full (a blocked stderr?)", (dropped,), None)
            super().handle(note)

    def stop(self, timeout: float | None = None) -> bool:  # the stock stop joins without a timeout
        thread = self._thread
        if thread is None:
            return True
        try:
            self.queue.put(self._sentinel, timeout=timeout)  # a full queue: waits for room, bounded
        except queue.Full:
            return False
        thread.join(timeout)
        if thread.is_alive():
            return False
        self._thread = None
        return True


class _QueueHandler(logging.handlers.QueueHandler):
    """The message is rendered on the logging thread, like the stock handler
    (its arguments may change afterwards), but the traceback stays apart:
    the stock prepare folds it into the message, process.log keeps both
    fields.  Query values that can carry a secret are masked here, so the
    stderr handler behind the queue (the container log) never gets them
    either."""

    listener: _QueueListener
    dropped = 0  # records not queued because the queue was full (approximate across threads; reset when reported)

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1  # never block or raise in the thread that logs (the event loop among them)

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        record = copy.copy(record)
        try:
            record.message = record.getMessage()
        except Exception:  # noqa: BLE001
            record.message = str(record.msg)
        record.message = mask_query_secrets(record.message)
        record.msg, record.args = record.message, None
        if record.exc_info:
            record.exc_text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            record.exc_info = None  # the traceback objects hold frames: not kept alive in the queue
        if record.exc_text:
            record.exc_text = mask_query_secrets(record.exc_text)
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


def activate_queue(logger: logging.Logger | None = None, maxsize: int = QUEUE_MAX) -> None:
    """Move the logger's handlers (root: stderr and process.log) behind a
    queue.  The swap is one assignment, not atomic against another thread
    logging at that moment: call it while one thread logs (run.py, before HA
    boots)."""
    logger = logger or logging.getLogger()
    if _queue_handler(logger) is not None:
        return
    q: queue.Queue = queue.Queue(maxsize)
    handler = _QueueHandler(q)
    handler.listener = _QueueListener(q, *logger.handlers, respect_handler_level=True)
    handler.listener.source = handler
    handler.listener.start()
    logger.handlers = [handler]


def flush_queue(timeout: float = 5.0, logger: logging.Logger | None = None) -> bool:
    """Wait, at most `timeout`, until what was queued so far is written."""
    handler = _queue_handler(logger or logging.getLogger())
    if handler is None:
        return True
    done = threading.Event()
    deadline = time.monotonic() + timeout
    try:
        handler.queue.put(done, timeout=timeout)  # never dropped like a record: waits for room, bounded
    except queue.Full:
        return False
    return done.wait(max(0.0, deadline - time.monotonic()))


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
