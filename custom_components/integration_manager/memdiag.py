"""GET /api/diag/memory: what the process is made of right now, to tell a
leak from a warm-up.  RSS split (anonymous vs file-backed), threads, file
descriptors, asyncio tasks and timers, event-bus listeners per event,
live gc-tracked objects grouped by type, and tracemalloc's top
allocation sites when the process was started with ``HRI_TRACEMALLOC=<n
frames>`` (tracemalloc costs memory and CPU, so it is opt-in).  Two
snapshots hours apart show what grew.  ``?refs=<TypeName>`` instead walks
the heap for instances of one type and reports what keeps them alive."""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import threading
import time
import tracemalloc
from collections import Counter
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from .http_util import ManagerView

TOP_TYPES = 40
TOP_SITES = 30
REF_SAMPLE = 200  # instances examined by the referrer probe


def referrers(type_name: str) -> dict[str, Any]:
    """Who keeps instances of ``type_name`` alive: the types that refer to a
    sample of them, and for a referring container the attribute of its own
    owner that holds it.  Answers "which cache is growing" without a
    debugger; costs a full gc walk, so it runs only when asked for."""
    objs = [o for o in gc.get_objects() if type(o).__qualname__ == type_name or f"{type(o).__module__}.{type(o).__qualname__}" == type_name]
    total = len(objs)
    sample = objs[:REF_SAMPLE]
    by_referrer: Counter[str] = Counter()
    holders: Counter[str] = Counter()
    own = (objs, sample)  # the probe's own lists refer to every instance: not holders
    for o in sample:
        for r in gc.get_referrers(o):
            if any(r is x for x in own):
                continue
            rt = type(r)
            by_referrer[f"{rt.__module__}.{rt.__qualname__}"] += 1
            if rt in (dict, list, set, tuple):
                for owner in gc.get_referrers(r):
                    ot = type(owner)
                    if ot in (dict, list, set, tuple):
                        continue
                    attr = next((k for k, v in list((getattr(owner, "__dict__", None) or {}).items()) if v is r), None)
                    holders[f"{ot.__module__}.{ot.__qualname__}.{attr or '?'} ({rt.__name__})"] += 1
    del objs, sample
    return {
        "type": type_name,
        "instances": total,
        "sampled": min(total, REF_SAMPLE),
        "referrer_types": [{"type": k, "count": v} for k, v in by_referrer.most_common(15)],
        "held_by": [{"where": k, "count": v} for k, v in holders.most_common(15)],
    }


def _proc_status() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("VmRSS", "RssAnon", "RssFile", "RssShmem", "VmSwap", "Threads"):
                    out[key] = int(rest.split()[0])
    except OSError:
        pass
    return out


def _sync_part() -> dict[str, Any]:
    objs = gc.get_objects()
    counts: Counter[str] = Counter()
    for o in objs:
        t = type(o)
        counts[f"{t.__module__}.{t.__qualname__}"] += 1
    n_objs = len(objs)
    del objs
    out: dict[str, Any] = {
        "proc": _proc_status(),
        "threads": threading.active_count(),
        "thread_names": sorted(t.name for t in threading.enumerate()),
        "gc": {"tracked_objects": n_objs, "counts": gc.get_count(), "garbage": len(gc.garbage),
               "allocated_blocks": sys.getallocatedblocks()},
        "objects_by_type": [{"type": k, "count": v} for k, v in counts.most_common(TOP_TYPES)],
    }
    try:
        out["fds"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    if tracemalloc.is_tracing():
        cur, peak = tracemalloc.get_traced_memory()
        snap = tracemalloc.take_snapshot()
        out["tracemalloc"] = {
            "traced_kb": cur // 1024,
            "peak_kb": peak // 1024,
            "frames": tracemalloc.get_traceback_limit(),
            "top": [{"where": str(s.traceback[0]) if s.traceback else "", "size_kb": s.size // 1024, "count": s.count}
                    for s in snap.statistics("lineno")[:TOP_SITES]],
        }
    else:
        out["tracemalloc"] = None
    return out


async def snapshot(hass: HomeAssistant) -> dict[str, Any]:
    loop = hass.loop
    head = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "uptime_s": int(time.monotonic() - _T0),
        "states": hass.states.async_entity_ids_count(),
        "bus_listeners": dict(sorted(hass.bus.async_listeners().items())),
        "asyncio_tasks": len(asyncio.all_tasks(loop)),
        "loop_timers": len(getattr(loop, "_scheduled", ())),
    }
    return {**head, **await hass.async_add_executor_job(_sync_part)}


_T0 = time.monotonic()


class MemoryDiagView(ManagerView):
    url = "/api/diag/memory"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        # both walk every gc-tracked object: not something a link on any web page may trigger
        if request.headers.get("X-Requested-With") != "fetch":
            return self.json_message("the memory probe walks the whole heap: send the header X-Requested-With: fetch", status_code=400)
        name = request.query.get("refs", "").strip()
        if name:
            if not name.replace(".", "").replace("_", "").isalnum():
                return self.json_message("refs must be a type name", status_code=400)
            return self.json(await self.hass.async_add_executor_job(referrers, name))
        return self.json(await snapshot(self.hass))
