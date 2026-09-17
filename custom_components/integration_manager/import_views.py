"""HTTP endpoints for importing from a Home Assistant backup (ha_import.py)."""

from __future__ import annotations

import asyncio

from typing import Any

import os
import re

from aiohttp import web
from homeassistant.core import HomeAssistant

from . import ha_import
from .diagnostics import scrub
from .http_util import ManagerView, with_body

MAX_UPLOAD = 2 * 1024 * 1024 * 1024  # HA backups with media can be big; .storage is what we read


_IMPORT_LOCK = asyncio.Lock()


class ImportBusy(ValueError):
    """Refused because another import or a manager action holds what this needs: nothing was done, try again."""


async def _locked(make_coro, installer=None):
    """apply / apply_all one at a time: one's cleanup deletes the extracted
    backup the other is still reading, and two applies of one entry race.
    ``installer``: the import also takes the busy flag start and stop take, and
    reads the running integration only under it — an import decides there whether
    the entry is stored enabled, so a stop finishing while it runs would leave an
    enabled entry (set up by Home Assistant) behind a manager that reports the
    integration as stopped."""
    if _IMPORT_LOCK.locked():
        raise ImportBusy("an import is already running: wait for it to finish")
    async with _IMPORT_LOCK:
        if installer is None:
            return await make_coro()
        if installer.busy:
            raise ImportBusy("another action is running (an install, start, stop, import, restore or full rollback): wait for it to finish")
        installer.busy = True
        try:
            return await make_coro()
        finally:
            installer.busy = False


_REBUILD_MSG = "a Home Assistant downgrade with a clean start is scheduled and uses the import area: restart first"


def _rebuild_staged(config_dir: str) -> bool:
    return os.path.isfile(os.path.join(config_dir, ha_import.REBUILD_FILE))


class ImportUploadView(ManagerView):
    url = "/api/import/upload"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        # before the lock: a request refused for the header must not take it, and turn away a real upload meanwhile
        if request.headers.get("X-Requested-With") != "fetch":
            return self.json_message("X-Requested-With: fetch required", status_code=400)
        if _IMPORT_LOCK.locked():
            return self.json({"ok": False, "error": "an import or upload is running: wait for it to finish"})
        async with _IMPORT_LOCK:  # held for the whole upload: an apply starting meanwhile would read files this replaces
            return await self._post(request)

    async def _post(self, request: web.Request) -> web.Response:
        if _rebuild_staged(self.hass.config.config_dir):
            return self.json({"ok": False, "error": _REBUILD_MSG})
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return self.json({"ok": False, "error": "form field 'file' expected"})
        dest = self.hass.config.path(ha_import.IMPORT_TAR)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        size = 0
        done = False
        fh = await self.hass.async_add_executor_job(open, dest + ".tmp", "wb")
        try:
            while chunk := await field.read_chunk(1 << 16):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return self.json({"ok": False, "error": "file too large"})
                await self.hass.async_add_executor_job(fh.write, chunk)
            done = size > 0
        finally:
            await self.hass.async_add_executor_job(fh.close)
            if not done:  # too large, empty, or the client went away mid-upload
                try:
                    os.remove(dest + ".tmp")
                except OSError:
                    pass
        if size == 0:
            return self.json({"ok": False, "error": "empty upload"})
        if _rebuild_staged(self.hass.config.config_dir):  # staged while this streamed: clearing the import area would drop it
            await self.hass.async_add_executor_job(os.remove, dest + ".tmp")
            return self.json({"ok": False, "error": _REBUILD_MSG})
        await self.hass.async_add_executor_job(os.replace, dest + ".tmp", dest)
        await self.hass.async_add_executor_job(ha_import.clear_extracted, self.hass.config.config_dir)
        return self.json({"ok": True, "bytes": size, "name": os.path.basename(field.filename or "backup.tar")})


class ImportInspectView(ManagerView):
    url = "/api/import/inspect"

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        cfg = self.hass.config.config_dir
        summary = await self.hass.async_add_executor_job(ha_import.load_summary, cfg)
        # a GET anyone on the network can make: the backup's passwords and tokens stay
        # masked (the POST inspect answers the user who gave the key in full; an import
        # puts the stored value back wherever it receives "***")
        return self.json({"uploaded": os.path.isfile(os.path.join(cfg, ha_import.IMPORT_TAR)),
                          "summary": scrub(summary) if summary else None})

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        if _IMPORT_LOCK.locked():
            return self.json({"ok": False, "error": "an import or upload is running: wait for it to finish"})
        async with _IMPORT_LOCK:  # the extraction this writes must not change under an apply
            return await self._post(request, body)

    async def _post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        cfg = self.hass.config.config_dir
        if os.path.isfile(os.path.join(cfg, ha_import.REBUILD_FILE)):
            return self.json({"ok": False, "error": "a Home Assistant downgrade with a clean start is scheduled: restart first"})
        if not os.path.isfile(os.path.join(cfg, ha_import.IMPORT_TAR)):
            return self.json({"ok": False, "error": "upload a backup first"})
        password = body.get("password")
        if password is not None and not isinstance(password, str):
            return self.json({"ok": False, "error": "password must be a string"})
        try:
            domains = set(self.installer.state.installed) | ({self.installer.running} if self.installer.running else set())
            summary = await self.hass.async_add_executor_job(ha_import.inspect_backup, cfg, password or None, domains)
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        except Exception as err:  # noqa: BLE001
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json({"ok": True, "summary": summary})


class ImportApplyView(ManagerView):
    url = "/api/import/apply"

    def __init__(self, hass: HomeAssistant, aligner: ha_import.RegistryAligner, installer=None) -> None:
        self.hass = hass
        self.aligner = aligner
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        domain = str(body.get("domain", "")).strip()
        entry_id = str(body.get("entry_id", "")).strip()
        if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[A-Za-z0-9]+", entry_id):
            return self.json({"ok": False, "error": "domain/entry_id required"})
        if _rebuild_staged(self.hass.config.config_dir):
            return self.json({"ok": False, "error": _REBUILD_MSG})
        data, options = body.get("data"), body.get("options")
        if data is not None and not isinstance(data, dict) or options is not None and not isinstance(options, dict):
            return self.json({"ok": False, "error": "data/options must be JSON objects"})

        def do_apply():  # what runs is read inside _locked, under the flag a start or a stop takes
            running = self.installer.running if self.installer else None
            installed = set(self.installer.state.installed) if self.installer else set()
            return ha_import.apply(self.hass, self.aligner, domain, entry_id, data, options,
                                   bool(body.get("align", True)), bool(body.get("copy_storage", False)),
                                   running=(domain == running), installed=(domain in installed))

        try:
            result = await _locked(do_apply, self.installer)
            if result.get("suspended") and self.installer:
                self.installer.mark_suspended(result["entry_id"])
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        except Exception as err:  # noqa: BLE001
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json({"ok": True, **result})


class ImportClearView(ManagerView):
    url = "/api/import/clear"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        if _rebuild_staged(self.hass.config.config_dir):
            return self.json({"ok": False, "error": _REBUILD_MSG})
        if _IMPORT_LOCK.locked():
            return self.json({"ok": False, "error": "an import or upload is running: wait for it to finish"})
        async with _IMPORT_LOCK:
            await self.hass.async_add_executor_job(ha_import.clear, self.hass.config.config_dir)
        return self.json({"ok": True})


class ImportApplyAllView(ManagerView):
    """POST /api/import/apply_all {domains?: [..], align, copy_storage}: every
    config entry of every installed integration in the inspected backup."""

    url = "/api/import/apply_all"

    def __init__(self, hass: HomeAssistant, aligner: ha_import.RegistryAligner, installer) -> None:
        self.hass = hass
        self.aligner = aligner
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        if _rebuild_staged(self.hass.config.config_dir):
            return self.json({"ok": False, "error": _REBUILD_MSG})
        domains = body.get("domains")
        if domains is not None and not (isinstance(domains, list) and all(isinstance(d, str) and re.fullmatch(r"[a-z0-9_]+", d) for d in domains)):
            return self.json({"ok": False, "error": "domains must be a list of domain names"})

        def do_apply_all():
            return ha_import.apply_all(self.hass, self.aligner, domains, bool(body.get("align", True)), bool(body.get("copy_storage", True)),
                                       self.installer.running, set(self.installer.state.installed))

        try:
            result = await _locked(do_apply_all, self.installer)
            for row in result.get("imported", []):
                if row.get("suspended"):
                    self.installer.mark_suspended(row["entry_id"])
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        except Exception as err:  # noqa: BLE001
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json({"ok": True, **result})
