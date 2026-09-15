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
from typing import Any

from aiohttp import web

from .ui import load_template, render
from homeassistant.core import HomeAssistant

from .http_util import ManagerView
from homeassistant.loader import async_get_custom_components

try:  # /app is on sys.path (run.py's directory)
    import logbuffer
except ImportError:  # pragma: no cover - running outside the container
    logbuffer = None  # type: ignore[assignment]

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

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
            limit = min(int(q.get("limit", 500) or 500), 2000)
        except ValueError:
            return self.json_message("since_id/limit must be integers", status_code=400)
        recs, truncated = await self.hass.async_add_executor_job(
            functools.partial(
                handler.query,
                prefixes=tuple(q.getall("prefix", [])),
                min_level=getattr(logging, level, logging.DEBUG),
                text=q.get("q", ""),
                since_id=since_id,
                limit=limit,
            )
        )
        return self.json({"records": recs, "capacity": handler.capacity, "path": handler.path, "truncated": truncated})


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
        if level is not None and str(level).upper() not in LEVELS:
            return self.json_message("bad level", status_code=400)
        logging.getLogger(name).setLevel(logging.NOTSET if level is None else getattr(logging, str(level).upper()))
        logging.getLogger(__name__).info("log level %s -> %s (from UI)", name, level or "inherited")
        return self.json({"ok": True, "logger": name, "level": level})
