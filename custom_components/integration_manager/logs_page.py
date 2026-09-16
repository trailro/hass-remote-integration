"""Log browser: what the installed integrations (and HA) log, live.

Reads the process log run.py writes on disk (``logbuffer``),
groups loggers by integration (``custom_components.<domain>`` plus the
top-level modules of the integration's requirements, e.g. the requirement
``some-lib`` provides ``some_lib``) and lets the user raise a logger's level
at runtime (the registry's ``quiet_loggers`` start at WARNING)."""

from __future__ import annotations

import functools
import importlib.metadata as md
import os
import json
import logging
import re
from typing import Any

from aiohttp import web

from .ui import load_template, render
from homeassistant.core import HomeAssistant

from .diagnostics import scrub_lines
from .http_util import ManagerView
from homeassistant.loader import async_get_custom_components

try:  # /app is on sys.path (run.py's directory)
    import logbuffer
except ImportError:  # pragma: no cover - running outside the container
    logbuffer = None  # type: ignore[assignment]

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MAX_LIMIT = 2000
# a logger that does not exist yet (a library imported later) may be set ahead, but every name
# creates a permanent logger: dotted identifiers only, and a bounded number of them
_LOGGER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*")
MAX_LOGGER_NAME = 200
MAX_NEW_LOGGERS = 50
_NEW_LOGGERS: set[str] = set()
ROOT_LOGGER = "root"


def _query_masked(handler, **kwargs: Any) -> tuple[list[dict[str, Any]], bool, int]:
    """handler.query with message and traceback masked by the diagnostics
    scrubber, over all the records at once: a PEM block printed line by line
    becomes one record per line, and no record on its own matches it.

    The search runs on the masked text only, so searching for a key returns
    the lines that still say what the user typed, and nothing that only
    matched inside the part now masked.  It runs twice.  Inside the handler,
    on each record masked on its own, before the record takes a place on the
    page: a page of records that match only inside a masked value (records
    holding ``password=needle``) would otherwise come back empty, and a
    follower that advances from the records it is given would ask for that
    same page forever.  Then here, on the page masked as one text, because
    the records next to a record can show it is key material (the END marker
    below a short last line of a body): scrub_lines masks a line that is key
    material on its own for exactly the case where they are not on the page.

    The handler gets no text: its search runs on the raw message, and
    whatever depends on which records the raw search matched - which records
    are masked (the time an answer takes), how far the page reaches (the
    cursor, truncated) - answers a guess at a masked value.  With a search,
    every record that passed the level and logger filters is masked, until
    the page is full, whether its raw text matches or not.

    The third value is the cursor: the newest id this answer has read past
    (shown, asked about and not matching, or left out by the level and logger
    filters), so a follower that continues from it never re-reads a page and
    never skips a record it was not shown.  It does not depend on the search's
    text: a page that is not full ends at the newest record read, a full one
    before the first record that did not fit, which depends only on which
    records match once masked.
    """
    text = str(kwargs.pop("text", "") or "").lower()
    seen = int(kwargs.get("since_id") or 0)

    def keep(rec: dict[str, Any]) -> bool:
        if not text:
            return True
        masked = scrub_lines([str(rec.get("message") or "")])[0]  # also when the logger name matches: the same work for every record
        return text in masked.lower() or text in str(rec.get("logger", "")).lower()

    def cursor(newest: int) -> None:
        nonlocal seen
        seen = max(seen, int(newest or 0))

    recs, truncated = handler.query(**kwargs, keep=keep, cursor=cursor)
    fields = ("message", "exc")
    masked = scrub_lines([str(rec.get(f) or "") for rec in recs for f in fields])
    for i, rec in enumerate(recs):
        for j, field in enumerate(fields):
            if rec.get(field):
                rec[field] = masked[i * len(fields) + j]
    if text:
        recs = [r for r in recs
                if text in str(r.get("message", "")).lower() or text in str(r.get("logger", "")).lower()]
    return recs, truncated, seen

LOGS_HTML = load_template("logs")


_DISTS_CACHE: dict[str, Any] = {"key": None, "dists": {}}


def _dists() -> dict[str, list[str]]:
    """packages_distributions() scans every dist-info in site-packages
    (50-300 ms): computed in the executor and cached until site-packages
    changes (a pip run)."""
    import sysconfig

    sp = sysconfig.get_paths().get("purelib") or ""
    try:
        key = os.stat(sp).st_mtime_ns
    except OSError:
        key = None
    if _DISTS_CACHE["key"] != key or not _DISTS_CACHE["dists"]:
        try:
            _DISTS_CACHE["dists"] = md.packages_distributions()
        except Exception:  # noqa: BLE001
            _DISTS_CACHE["dists"] = {}
        _DISTS_CACHE["key"] = key
    return _DISTS_CACHE["dists"]


def _requirement_modules(requirements: list[str], dists: dict[str, list[str]]) -> list[str]:
    """Top-level modules provided by an integration's pip requirements
    (some-lib -> some_lib, some_lib_extra), i.e. the loggers it really uses."""
    mods: list[str] = []
    for req in requirements:
        name = req.split("==")[0].split(">=")[0].split("[")[0].strip().lower().replace("-", "_")
        for module, owners in dists.items():
            if any(o.lower().replace("-", "_") == name for o in owners) and module not in ("bin", "tests", "bench", "docs", "examples") and not module.startswith("_"):
                mods.append(module)
    return sorted(set(mods))


async def logger_groups(hass: HomeAssistant) -> list[dict[str, Any]]:
    handler = logbuffer.find() if logbuffer else None
    counts = dict(handler.loggers) if handler else {}
    groups: list[dict[str, Any]] = []
    customs = await async_get_custom_components(hass)
    dists = await hass.async_add_executor_job(_dists)
    for domain, integration in sorted(customs.items()):
        loggers = [f"custom_components.{domain}", *_requirement_modules(integration.requirements or [], dists)]
        groups.append(_group(domain, loggers, counts))
    groups.append(_group("homeassistant", ["homeassistant"], counts))
    known = {lg for g in groups for lg in g["loggers"]}
    others = sorted({n.split(".")[0] for n in counts if not n.startswith(tuple(known))})
    if others:
        groups.append(_group("other", others, counts))
    return groups


def _group(name: str, loggers: list[str], counts: dict[str, int]) -> dict[str, Any]:
    per = {lg: sum(c for n, c in counts.items() if n == lg or n.startswith(lg + ".")) for lg in loggers}
    levels = {}
    for lg in loggers:
        obj = logging.getLogger(lg)
        levels[lg] = logging.getLevelName(obj.level) if obj.level else "(inherited)"
    return {"name": name, "loggers": loggers, "counts": per, "count": sum(per.values()), "levels": levels}


class LogsPageView(ManagerView):
    url = "/logs"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(LOGS_HTML, "/logs"), content_type="text/html")


class LogsApiView(ManagerView):
    url = "/api/logs"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            # a search over the whole on-disk buffer: not something a link on any web page may trigger
            return self.json_message("X-Requested-With: fetch required", status_code=400)
        handler = logbuffer.find() if logbuffer else None
        if handler is None:
            return self.json({"records": [], "capacity": 0, "error": "log file handler not installed"})
        q = request.query
        level = q.get("level", "DEBUG").upper()
        if level not in LEVELS:
            return self.json_message("level must be one of " + ", ".join(LEVELS), status_code=400)
        try:
            since_id = int(q.get("since_id", 0) or 0)
            limit = max(1, min(int(q.get("limit", 500) or 500), MAX_LIMIT))
        except ValueError:
            return self.json_message("since_id/limit must be integers", status_code=400)
        recs, truncated, cursor = await self.hass.async_add_executor_job(
            functools.partial(
                _query_masked,
                handler,
                prefixes=tuple(q.getall("prefix", [])),
                min_level=getattr(logging, level, logging.DEBUG),
                text=q.get("q", ""),
                since_id=since_id,
                limit=limit,
            )
        )
        # cursor: where the next follow poll continues (since_id), also when no record on this page survived the search
        return self.json({"records": recs, "capacity": handler.capacity, "path": handler.path, "truncated": truncated,
                          "cursor": cursor})


class LoggersApiView(ManagerView):
    url = "/api/logs/loggers"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        return self.json(await logger_groups(self.hass))


class LogLevelView(ManagerView):
    url = "/api/logs/level"

    async def post(self, request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            return self.json_message("Content-Type must be application/json", status_code=400)
        try:
            body = await request.json()
        except ValueError:
            return self.json_message("invalid JSON", status_code=400)
        if not isinstance(body, dict):
            return self.json_message("JSON object expected", status_code=400)
        name = str(body.get("logger") or "").strip()
        level = body.get("level")
        if not name:
            return self.json_message("logger required", status_code=400)
        if name == ROOT_LOGGER:
            # getLogger("root") is the root logger itself (since 3.9), not a logger called "root":
            # CRITICAL there silences the whole process log, including the line recording the change,
            # and no page lists it to put it back.  Clearing cannot undo it either - NOTSET is not the
            # INFO that run.py sets at boot.  Raising one noisy integration logger names that logger.
            return self.json_message("the root logger cannot be set here: it would silence every logger at once "
                                     "(set the level of the integration's own logger instead)", status_code=400)
        if level is not None and str(level).upper() not in LEVELS:
            return self.json_message("bad level", status_code=400)
        handler = logbuffer.find() if logbuffer else None
        known = name in logging.Logger.manager.loggerDict or name in _NEW_LOGGERS or (handler is not None and name in handler.loggers)
        if not known:
            if len(name) > MAX_LOGGER_NAME or not _LOGGER_NAME.fullmatch(name):
                return self.json_message("logger must be a dotted Python name (letters, digits, _ and -)", status_code=400)
            if len(_NEW_LOGGERS) >= MAX_NEW_LOGGERS:
                return self.json_message(f"at most {MAX_NEW_LOGGERS} loggers that do not exist yet can be set", status_code=400)
            _NEW_LOGGERS.add(name)
        logging.getLogger(name).setLevel(logging.NOTSET if level is None else getattr(logging, str(level).upper()))
        logging.getLogger(__name__).info("log level %s -> %s (from UI)", name, level or "inherited")
        return self.json({"ok": True, "logger": name, "level": level})
