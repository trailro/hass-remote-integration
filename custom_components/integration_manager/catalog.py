"""The default list of HACS custom integrations, searchable from the Install
page: ``GET /api/catalog?q=``.

The data is what HACS itself shows (https://data-v2.hacs.xyz), cached in
memory for CACHE_S and on the volume (``integration_manager/hacs_catalog.json``)
so a restart or an unreachable server still has a list.  Searching never
installs anything: the page adds a hit to the registry and selects it in the
environment builder, where Check shows what installing it would do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from jsonio import read_json, write_json

from .http_util import ManagerView
from .installer import read_capped

_LOGGER = logging.getLogger(__name__)

DATA_URL = "https://data-v2.hacs.xyz/integration/data.json"
MAX_BYTES = 32 * 1024 * 1024  # about 2 MB in 2026
CACHE_S = 12 * 3600
RETRY_S = 120
MAX_RESULTS = 40


class Catalog:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.path = hass.config.path("integration_manager", "hacs_catalog.json")
        self.error = ""
        self.fetched_at: str | None = None
        self._rows: list[dict[str, Any]] | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def rows(self) -> list[dict[str, Any]]:
        async with self._lock:
            if self._rows is not None and time.monotonic() - self._at < CACHE_S:
                return self._rows
            try:
                session = async_get_clientsession(self.hass)
                async with session.get(DATA_URL, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    resp.raise_for_status()
                    raw = await read_capped(resp, "HACS catalog", MAX_BYTES)
                rows = await self.hass.async_add_executor_job(self._parse_and_store, raw)
                self.error = ""
            except Exception as err:  # noqa: BLE001 - offline, HACS down: the cached list still works
                self.error = f"{type(err).__name__}: {err}"
                _LOGGER.warning("HACS catalog not refreshed: %s", self.error)
                rows = self._rows if self._rows is not None else await self.hass.async_add_executor_job(self._load_cached)
                self._rows, self._at = rows, time.monotonic() - CACHE_S + RETRY_S  # try again soon, not in 12 hours
                return rows
            self._rows, self._at = rows, time.monotonic()
            return rows

    def _parse_and_store(self, raw: bytes) -> list[dict[str, Any]]:
        rows = []
        for item in (json.loads(raw) or {}).values():
            if not isinstance(item, dict) or not item.get("full_name") or not item.get("domain"):
                continue
            manifest = item.get("manifest") if isinstance(item.get("manifest"), dict) else {}
            rows.append({
                "domain": str(item["domain"]),
                "repo": str(item["full_name"]),
                "name": str(item.get("manifest_name") or manifest.get("name") or item["domain"]),
                "description": str(item.get("description") or "")[:300],
                "last_version": str(item.get("last_version") or "") or None,
                "last_updated": str(item.get("last_updated") or "")[:10],
                "topics": [str(t) for t in (item.get("topics") or [])][:10],
            })
        self.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        write_json(self.path, {"fetched_at": self.fetched_at, "rows": rows}, fsync=False)
        return rows

    def _load_cached(self) -> list[dict[str, Any]]:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            return []
        self.fetched_at = data.get("fetched_at")
        return data.get("rows") if isinstance(data.get("rows"), list) else []


def search(rows: list[dict[str, Any]], query: str, registry: dict[str, Any], installed: set[str]) -> tuple[list[dict[str, Any]], int]:
    """Every word must appear in the name, domain, repository, description or
    topics; an exact domain, then a name or domain starting with the query
    rank first, then the most recently updated."""
    q = query.strip().lower()
    if len(q) < 2:
        return [], 0
    words = q.split()
    hits = []
    for r in rows:
        name = r["name"].lower()
        haystack = " ".join((r["domain"], r["repo"].lower(), name, r["description"].lower(), " ".join(r["topics"]).lower()))
        if not all(w in haystack for w in words):
            continue
        score = (100 if r["domain"] == q else 0) + (50 if name.startswith(q) or r["domain"].startswith(q) else 0) + (20 if q in name else 0)
        hits.append((score, r))
    hits.sort(key=lambda h: h[1]["last_updated"], reverse=True)
    hits.sort(key=lambda h: h[0], reverse=True)
    out = []
    for _score, r in hits[:MAX_RESULTS]:
        spec = registry.get(r["domain"]) or {}
        out.append({**r, "in_registry": bool(spec), "registry_repo": spec.get("repo"), "installed": r["domain"] in installed})
    return out, len(hits)


class CatalogView(ManagerView):
    url = "/api/catalog"

    def __init__(self, catalog: Catalog, installer: Any) -> None:
        self.catalog = catalog
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            return self.json_message("X-Requested-With: fetch required (the catalog is downloaded on first use)", status_code=400)
        rows = await self.catalog.rows()
        results, total = search(rows, request.query.get("q", "")[:80], self.installer.registry(), set(self.installer.state.installed))
        return self.json({"results": results, "total": total, "catalog_size": len(rows), "fetched_at": self.catalog.fetched_at,
                          "error": self.catalog.error})
