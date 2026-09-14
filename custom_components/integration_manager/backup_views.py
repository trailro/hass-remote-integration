"""Backup & restore endpoints (the logic lives in /app/backupkit.py, shared
with entrypoint.py which applies a scheduled restore before HA starts)."""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from . import events, ha_import
from .http_util import ManagerView, with_body

import backupkit  # /app/backupkit.py (/app is on sys.path)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip$")
MAX_UPLOAD = 200 * 1024 * 1024


def _name_ok(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and ".." not in name


class BackupsView(ManagerView):
    url = "/api/backups"

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        cfg = self.hass.config.config_dir
        items, pending, parts, last = await self.hass.async_add_executor_job(
            lambda: (backupkit.list_backups(cfg), backupkit.pending(cfg), backupkit.pending_parts(cfg) if backupkit.pending(cfg) else None,
                     _last_restore(self.hass)))
        return self.json({"backups": items, "pending_restore": pending, "keep": self.installer.settings.backup_keep,
                          "parts": list(backupkit.PARTS), "pending_parts": parts, "last_restore": last})


class BackupCreateView(ManagerView):
    url = "/api/backups/create"

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        cfg = self.hass.config.config_dir
        if self.installer.busy:
            return self.json({"ok": False, "error": "an install/start is running: try again in a moment"})
        try:
            rec = await self.installer.async_backup(str(body.get("label") or ""))
            removed = await self.hass.async_add_executor_job(backupkit.prune, cfg, self.installer.settings.backup_keep,
                                                             self.installer.protected_backups())
        except Exception as err:  # noqa: BLE001
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json({"ok": True, "backup": rec, "pruned": removed})


class BackupUploadView(ManagerView):
    """multipart/form-data upload; the custom header forces a CORS preflight
    (multipart alone would be a 'simple' cross-site request)."""

    url = "/api/backups/upload"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            return self.json_message("X-Requested-With: fetch required", status_code=400)
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return self.json({"ok": False, "error": "form field 'file' expected"})
        raw = os.path.basename(field.filename or "upload.zip")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
        if not name.endswith(".zip"):
            name += ".zip"
        name = "upload-" + name
        if not _name_ok(name):
            return self.json({"ok": False, "error": "bad file name"})
        bdir = os.path.join(self.hass.config.config_dir, backupkit.BACKUP_DIR)
        os.makedirs(bdir, exist_ok=True)
        dest = os.path.join(bdir, name)
        size = 0
        ok = False
        fd, tmp = await self.hass.async_add_executor_job(lambda: tempfile.mkstemp(dir=bdir, suffix=".zip.tmp"))
        fh = await self.hass.async_add_executor_job(os.fdopen, fd, "wb")
        try:
            while chunk := await field.read_chunk(1 << 16):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return self.json({"ok": False, "error": "file too large"})
                await self.hass.async_add_executor_job(fh.write, chunk)
            await self.hass.async_add_executor_job(fh.close)
            try:
                info = await self.hass.async_add_executor_job(backupkit.validate, tmp)
            except ValueError as err:
                return self.json({"ok": False, "error": str(err)})
            await self.hass.async_add_executor_job(os.replace, tmp, dest)
            ok = True
        finally:
            await self.hass.async_add_executor_job(fh.close)
            if not ok:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return self.json({"ok": True, "name": name, "bytes": size, "info": info})


class BackupActionView(ManagerView):
    url = "/api/backups/{name}/{action}"

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request, name: str, action: str) -> web.Response:
        if action != "download" or not _name_ok(name):
            return self.json_message("not found", status_code=404)
        path = os.path.join(self.hass.config.config_dir, backupkit.BACKUP_DIR, name)
        if not os.path.isfile(path):
            return self.json_message("not found", status_code=404)
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], name: str, action: str) -> web.Response:
        if not _name_ok(name):
            return self.json({"ok": False, "error": "bad name"})
        cfg = self.hass.config.config_dir
        path = os.path.join(cfg, backupkit.BACKUP_DIR, name)
        if not os.path.isfile(path):
            return self.json({"ok": False, "error": "no such backup"})
        try:
            if action == "delete":
                if name in self.installer.protected_backups():
                    return self.json({"ok": False, "error": "a scheduled Home Assistant version change or a full rollback needs this backup"})
                os.remove(path)
                return self.json({"ok": True})
            if action == "restore":
                if self.installer.busy:
                    return self.json({"ok": False, "error": "an install/start is running: try again in a moment"})
                parts = body.get("parts")
                if parts is not None and not (isinstance(parts, list) and all(isinstance(x, str) for x in parts)):
                    return self.json({"ok": False, "error": "parts must be a list"})
                await self.hass.async_add_executor_job(backupkit.schedule_restore, cfg, name, parts)
                await self.hass.async_add_executor_job(ha_import.drop_rebuild, cfg)  # the restore replaces a scheduled clean start
                events.emit("restore", f"{name} scheduled for the next restart ({', '.join(parts) if parts else 'everything'})", backup=name)
                return self.json({"ok": True, "parts": parts or list(backupkit.PARTS),
                                  "note": "restore is applied by the entrypoint at the next process restart"})
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        return self.json_message("unknown action", status_code=400)


class RestoreCancelView(ManagerView):
    url = "/api/backups/restore/cancel"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        cancelled = await self.hass.async_add_executor_job(backupkit.cancel_restore, self.hass.config.config_dir)
        return self.json({"ok": True, "cancelled": cancelled})


def _last_restore(hass: HomeAssistant) -> dict[str, Any] | None:
    import json

    try:
        with open(hass.config.path(backupkit.STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            return json.load(fh).get("last_restore")
    except (OSError, ValueError):
        return None
