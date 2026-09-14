"""The instance's timeline: what happened to it and when.  One JSON object
per line in ``integration_manager/events.jsonl`` (rotated at MAX_BYTES,
one older file kept), written by the installer (install/start/stop/
switch/rollback/smoke/restart), the MQTT publisher (connect/disconnect,
health verdict changes), the cutover and the HA updater.  Read by
``GET /api/events`` for the Manager page and by the diagnostics zip.

Thread-safe: paho's callbacks append from their own thread."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

MAX_BYTES = 512_000
KINDS = ("boot", "restore", "install", "start", "stop", "switch", "remove", "uninstall", "replace", "rollback", "smoke",
         "restart", "error", "mqtt", "health", "cutover", "ha", "yaml", "dev", "build", "notify")


class Events:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            self._size = os.path.getsize(path)
        except OSError:
            self._size = 0

    def add(self, kind: str, message: str, **data: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "message": message}
        if data:
            rec["data"] = data
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                if self._size + len(line) > MAX_BYTES:
                    os.replace(self.path, self.path + ".1")
                    self._size = 0
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                self._size += len(line.encode("utf-8"))
            except OSError as err:
                _LOGGER.warning("event not recorded: %s", err)
        _LOGGER.info("event %s: %s", kind, message)
        return rec

    def recent(self, limit: int = 100, kinds: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Newest ``limit`` events, chronological."""
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


def emit(kind: str, message: str, **data: Any) -> None:
    """Record an event; a no-op before the component is set up."""
    if EVENTS is not None:
        EVENTS.add(kind, message, **data)
