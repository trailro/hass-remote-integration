"""One thread writes the manager's own JSON files (settings, MQTT config and
rules, latest versions, action limits) in the order they were asked for.

They used to be loose executor jobs: two saves of the same file could land
in either order (a check's older result over a newer one), and json.dump ran
in the executor over dicts the loop kept changing.  Now the data is copied
on the caller's thread at submit, one worker writes the copies FIFO, and an
awaiting caller gets the write's exception as before.  Drained before a
restart, at HA's final write, before a backup zips the files, and by run.py
before the process exits."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import logging
import queue
import threading
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import Event, HomeAssistant

import jsonio

_LOGGER = logging.getLogger(__name__)
FINAL_WRITE_DRAIN_S = 30  # HA gives the final write stage 60 s


def _resolve(fut: concurrent.futures.Future, err: BaseException | None) -> None:
    try:
        if err is None:
            fut.set_result(None)
        else:
            fut.set_exception(err)
    except concurrent.futures.InvalidStateError:
        pass  # nobody waits any more; the write happened anyway


class Writer:
    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._cond = threading.Condition()
        self._pending = 0
        self._thread: threading.Thread | None = None
        self._stopped: threading.Thread | None = None  # a worker told to stop, maybe still writing what came before

    def write_nowait(self, path: str, data: Any, **kwargs: Any) -> concurrent.futures.Future:
        """Thread-safe.  `data` is copied here: what the caller changes afterwards is not written."""
        return self._submit((path, copy.deepcopy(data), kwargs))

    async def async_write(self, path: str, data: Any, **kwargs: Any) -> None:
        # shielded: a cancelled request must not cancel a write whose change is already in memory
        await asyncio.shield(asyncio.wrap_future(self.write_nowait(path, data, **kwargs)))

    def _submit(self, job: tuple | None) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._cond:
            self._pending += 1
            self._queue.put((job, fut))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, args=(self._stopped,), name="hri-json-writer", daemon=True)
                self._thread.start()
        return fut

    def _run(self, previous: threading.Thread | None) -> None:
        if previous is not None:
            previous.join()  # FIFO across a stop: the old worker first writes what was queued before its sentinel
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, fut = item
            err = None
            try:
                if job is not None:  # None: a drain marker
                    path, data, kwargs = job
                    jsonio.write_json(path, data, **kwargs)
            except Exception as exc:  # noqa: BLE001 - handed to the caller
                err = exc
            _resolve(fut, err)
            with self._cond:
                self._pending -= 1
                if not self._pending:
                    self._cond.notify_all()

    def drain(self, timeout: float) -> bool:
        """Thread-safe (not from the worker): True once nothing is queued or being written."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout)

    async def async_drain(self, timeout: float) -> bool:
        """True once everything queued before this call is written."""
        if not self._pending:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._submit(None))), timeout)
        except TimeoutError:
            return False
        return True

    def stop(self, timeout: float) -> bool:
        """Let the worker finish the queue and exit; a later write starts a new one."""
        with self._cond:
            thread, self._thread = self._thread, None
            if thread is None:
                return True
            self._stopped = thread
            self._queue.put(None)
        thread.join(timeout)
        return not thread.is_alive()


_WRITER: Writer | None = None
_WRITER_GUARD = threading.Lock()


def get() -> Writer:
    global _WRITER
    with _WRITER_GUARD:
        if _WRITER is None:
            _WRITER = Writer()
        return _WRITER


async def async_write(path: str, data: Any, **kwargs: Any) -> None:
    await get().async_write(path, data, **kwargs)


def write_nowait(path: str, data: Any, **kwargs: Any) -> concurrent.futures.Future:
    """For a caller that cannot await: a failure is logged."""
    fut = get().write_nowait(path, data, **kwargs)

    def done(f: concurrent.futures.Future) -> None:
        if f.exception() is not None:
            _LOGGER.error("%s could not be written: %s", path, f.exception())

    fut.add_done_callback(done)
    return fut


def drain(timeout: float) -> bool:
    return _WRITER.drain(timeout) if _WRITER is not None else True


async def async_drain(timeout: float) -> bool:
    return await _WRITER.async_drain(timeout) if _WRITER is not None else True


def stop(timeout: float) -> bool:
    return _WRITER.stop(timeout) if _WRITER is not None else True


def async_register(hass: HomeAssistant) -> None:
    async def _final_write(_event: Event | None) -> None:
        if not await async_drain(FINAL_WRITE_DRAIN_S):
            _LOGGER.error("JSON saves still pending %s s into the final write", FINAL_WRITE_DRAIN_S)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_FINAL_WRITE, _final_write)
