"""The instance's timeline: what happened to it and when.  One JSON object
per line in ``integration_manager/events.jsonl`` (rotated at MAX_BYTES,
one older file kept), written by the installer (install/start/stop/
switch/rollback/smoke/restart), the MQTT publisher (connect/disconnect,
health verdict changes), the cutover and the HA updater.  Read by
``GET /api/events`` for the Manager page and by the diagnostics zip.

Thread-safe: paho's callbacks append from their own thread.  Every event is
written by a thread of its own, in the order added, from the event loop or any
other thread: neither file I/O nor a backlog may hold up the loop or paho's
network thread.  A read waits for what was added before it; run.py drains the
queue before the process exits."""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import threading
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

MAX_BYTES = 512_000
READ_DRAIN_S = 5  # a read waits this long at most for the lines added before it
KINDS = ("boot", "restore", "install", "start", "stop", "switch", "remove", "uninstall", "replace", "rollback", "smoke",
         "restart", "error", "mqtt", "health", "cutover", "ha", "yaml", "dev", "build", "notify", "change", "auth", "rebuild")


class Events:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._size: int | None = None  # read by the writing thread, after whatever an earlier instance still had queued

    def add(self, kind: str, message: str, **data: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "message": message}
        if data:
            rec["data"] = data
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        _submit(self, line)  # one queue: the order events were added in is the order they are written in
        _LOGGER.info("event %s: %s", kind, message)
        return rec

    def _append(self, line: str) -> None:
        # the cap is in bytes: a line of non-ASCII text is longer than its characters.  "replace": a message may carry a
        # lone surrogate (an OSError's text with a file name Python decoded with surrogateescape), which utf-8 refuses
        data = line.encode("utf-8", errors="replace")
        try:
            if self._size is None:
                try:
                    self._size = os.path.getsize(self.path)
                except OSError:
                    self._size = 0
            if self._size + len(data) > MAX_BYTES:
                os.replace(self.path, self.path + ".1")
                self._size = 0
            with open(self.path, "ab") as fh:
                fh.write(data)
            self._size += len(data)
        except OSError as err:
            _LOGGER.warning("event not recorded: %s", err)

    def drain(self, timeout: float) -> bool:
        return drain(timeout)

    def recent(self, limit: int = 100, kinds: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Newest ``limit`` events, chronological.  Blocking: waits for the events added before the call."""
        self.drain(READ_DRAIN_S)
        out: list[dict[str, Any]] = []
        for p in (self.path, self.path + ".1"):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if kinds and rec.get("kind") not in kinds:
                    continue
                out.append(rec)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        out.reverse()
        return out


EVENTS: Events | None = None

# one thread for every instance: a new instance on the same file (a test, a reload) reads its size after the
# lines an older one queued are written
_QUEUE: queue.SimpleQueue = queue.SimpleQueue()
_APPEND = threading.Lock()  # one append at a time: the writing thread, or a caller off the loop
_COND = threading.Condition()
_PENDING = 0
_THREAD: threading.Thread | None = None


def _submit(store: Events, line: str) -> None:
    global _PENDING
    with _COND:
        _PENDING += 1
        _QUEUE.put((store, line))
        _ensure_writer()


def _ensure_writer() -> None:
    """Under _COND.  A writing thread that ended anyway (whatever _run did not catch) is replaced: every event queued
    after it would wait for it for good, and every read and the exit for READ_DRAIN_S."""
    global _THREAD
    if _THREAD is None or not _THREAD.is_alive():
        _THREAD = threading.Thread(target=_run, name="hri-events", daemon=True)
        _THREAD.start()


def _run() -> None:
    global _PENDING
    while True:
        store, line = _QUEUE.get()
        try:
            with _APPEND:
                store._append(line)  # noqa: SLF001
        except Exception:  # noqa: BLE001 - one event that cannot be written must not take every later one with it
            _LOGGER.exception("event not recorded")
        finally:
            with _COND:
                _PENDING -= 1
                if not _PENDING:
                    _COND.notify_all()


def emit(kind: str, message: str, **data: Any) -> None:
    """Record an event; a no-op before the component is set up."""
    if EVENTS is not None:
        EVENTS.add(kind, message, **data)


def drain(timeout: float) -> bool:
    """Blocking (not on the loop, not from the writing thread): True once every event added so far is in its file.
    Also before the process exits; run.py leaves with os._exit, which skips atexit, and has to call it itself."""
    with _COND:
        if _PENDING:
            try:
                _ensure_writer()
            except RuntimeError:  # at interpreter shutdown no thread starts: wait for the one there is
                pass
        return _COND.wait_for(lambda: _PENDING == 0, timeout)


atexit.register(drain, READ_DRAIN_S)
