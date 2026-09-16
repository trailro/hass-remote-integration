"""Backup & restore endpoints (the logic lives in /app/backupkit.py, shared
with entrypoint.py which applies a scheduled restore before HA starts)."""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from aiohttp import web
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from jsonio import fsync_dir, ha_vkey, read_json

from . import events, ha_import
from .http_util import ManagerView, with_body

import backupkit  # /app/backupkit.py (/app is on sys.path)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip\Z")  # \Z: "$" also matches before a trailing newline
MAX_UPLOAD = 200 * 1024 * 1024


def _name_ok(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and ".." not in name


class BackupsView(ManagerView):
    url = "/api/backups"

    def __init__(self, hass: HomeAssistant, installer, updater=None) -> None:
        self.hass = hass
        self.installer = installer
        self.updater = updater  # HaUpdater: installed venvs, versions a restore may switch to

    async def get(self, request: web.Request) -> web.Response:
        cfg = self.hass.config.config_dir
        items, pending, parts, last = await self.hass.async_add_executor_job(
            lambda: (backupkit.list_backups(cfg), backupkit.pending(cfg), backupkit.pending_parts(cfg) if backupkit.pending(cfg) else None,
                     _last_restore(self.hass)))
        boot, venvs, ha_state = await self.hass.async_add_executor_job(
            lambda: (backupkit.boot_version(cfg), self.updater._installed_venvs() if self.updater else [],  # noqa: SLF001
                     self.updater._read() if self.updater else {}))  # noqa: SLF001
        return self.json({"backups": items, "pending_restore": pending, "keep": self.installer.settings.backup_keep,
                          "parts": list(backupkit.PARTS), "pending_parts": parts, "last_restore": last,
                          "ha_current": HA_VERSION, "ha_boot": boot or HA_VERSION, "ha_installed": venvs,
                          "change": ha_state.get("change") if isinstance(ha_state, dict) else None})


class BackupCreateView(ManagerView):
    url = "/api/backups/create"

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        cfg = self.hass.config.config_dir
        try:
            rec = await self.installer.async_backup_exclusive(str(body.get("label") or ""))
            removed = await self.hass.async_add_executor_job(backupkit.prune, cfg, self.installer.settings.backup_keep,
                                                             self.installer.protected_backups() | {rec["name"]})
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
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
        size = 0
        ok = False

        def _open() -> tuple[Any, str]:
            os.makedirs(bdir, exist_ok=True)
            fd, path = tempfile.mkstemp(dir=bdir, prefix=".upload-", suffix=".zip.tmp")
            return os.fdopen(fd, "wb"), path

        def _discard() -> None:
            if not fh.closed:  # closed already once the upload was complete
                fh.close()
            if not ok:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        fh, tmp = await self.hass.async_add_executor_job(_open)
        try:
            while chunk := await field.read_chunk(1 << 16):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return self.json({"ok": False, "error": "file too large"})
                await self.hass.async_add_executor_job(fh.write, chunk)
            await self.hass.async_add_executor_job(fh.flush)
            await self.hass.async_add_executor_job(os.fsync, fh.fileno())  # a torn upload after a power loss is a torn restore source
            await self.hass.async_add_executor_job(fh.close)
            try:
                info = await self.hass.async_add_executor_job(backupkit.validate, tmp)
            except ValueError as err:
                return self.json({"ok": False, "error": str(err)})
            # a free name (upload-x-2.zip ...): an existing backup, a protected one included, is never replaced
            name = await self.hass.async_add_executor_job(backupkit.reserve_name, bdir, name[:-len(".zip")])
            if not _name_ok(name):
                await self.hass.async_add_executor_job(os.remove, os.path.join(bdir, name))
                return self.json({"ok": False, "error": "bad file name"})
            await self.hass.async_add_executor_job(os.replace, tmp, os.path.join(bdir, name))
            await self.hass.async_add_executor_job(fsync_dir, bdir)
            ok = True
        finally:
            await self.hass.async_add_executor_job(_discard)
        return self.json({"ok": True, "name": name, "bytes": size, "info": info})


class BackupActionView(ManagerView):
    url = "/api/backups/{name}/{action}"

    def __init__(self, hass: HomeAssistant, installer, updater=None) -> None:
        self.hass = hass
        self.installer = installer
        self.updater = updater  # HaUpdater: installed venvs, versions a restore may switch to

    async def get(self, request: web.Request, name: str, action: str) -> web.Response:
        if action != "download" or not _name_ok(name):
            return self.json_message("not found", status_code=404)
        path = os.path.join(self.hass.config.config_dir, backupkit.BACKUP_DIR, name)
        if not await self.hass.async_add_executor_job(os.path.isfile, path):
            return self.json_message("not found", status_code=404)
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], name: str, action: str) -> web.Response:
        if not _name_ok(name):
            return self.json({"ok": False, "error": "bad name"})
        cfg = self.hass.config.config_dir
        path = os.path.join(cfg, backupkit.BACKUP_DIR, name)
        if not await self.hass.async_add_executor_job(os.path.isfile, path):
            return self.json({"ok": False, "error": "no such backup"})
        try:
            if action == "delete":
                if name in self.installer.protected_backups() | await self.hass.async_add_executor_job(backupkit.restore_needs, cfg):
                    return self.json({"ok": False, "error": "a scheduled Home Assistant version change, a full rollback or a restore from the last 7 days needs this backup"})
                await self.hass.async_add_executor_job(os.remove, path)
                return self.json({"ok": True})
            if action == "restore":
                if self.installer.busy:
                    return self.json({"ok": False, "error": "an install/start is running: try again in a moment"})
                parts = body.get("parts")
                if parts is not None and not (isinstance(parts, list) and all(isinstance(x, str) for x in parts)):
                    return self.json({"ok": False, "error": "parts must be a list"})
                # Home Assistant only migrates a configuration forward: a backup made on another version is
                # restored on the version that boots ("keep", it migrates forward) or together with a switch
                # to the version it was made on ("backup", the only way for a backup of a newer version)
                choice = str(body.get("ha") or "keep")
                if choice not in ("keep", "backup"):
                    return self.json({"ok": False, "error": "ha must be keep or backup"})
                made_on = (await self.hass.async_add_executor_job(backupkit.describe, cfg, name)).get("ha_version")
                boot = await self.hass.async_add_executor_job(backupkit.boot_version, cfg) or HA_VERSION
                touches_storage = parts is None or "storage" in parts
                force = body.get("force") is True
                if not made_on and touches_storage and not force:
                    return self.json({"ok": False, "needs_force": True,
                                      "error": "this backup does not record the Home Assistant version it was made on: if that was a newer version than "
                                               f"{boot}, its .storage cannot be read after the restore. Restore anyway only if you know it was made on {boot} or older"})
                if choice == "keep" and made_on and touches_storage and ha_vkey(made_on) > ha_vkey(boot):
                    return self.json({"ok": False, "needs_ha": made_on,
                                      "error": f"backup was made on Home Assistant {made_on}, newer than {boot}: Home Assistant cannot read a newer "
                                               f"configuration, restore it together with a switch to {made_on}"})
                if choice == "backup" and made_on and made_on != boot:
                    if self.updater is None:
                        return self.json({"ok": False, "error": "Home Assistant version changes are not available here"})
                    if made_on != HA_VERSION:
                        from .views import async_change_ha_version

                        await self.updater.validate(made_on)
                        result = await async_change_ha_version(self.installer, self.updater, made_on, "restore", "restore",
                                                               restore_backup=name, parts=parts)
                        events.emit("restore", f"{name} scheduled with Home Assistant {made_on}, the version it was made on "
                                    f"({', '.join(parts) if parts else 'everything'}); backup {result['backup']} first", backup=name, version=made_on)
                        return self.json({"ok": True, "parts": parts or list(backupkit.PARTS), "ha": made_on, "pre_change_backup": result["backup"],
                                          "note": f"Home Assistant {made_on} is installed if needed and the backup restored at the next restart"})
                    # made on the running version while a switch to another one is scheduled: that switch goes,
                    # once the backup is known to be restorable and no other change is being prepared
                    from .views import _HA_CHANGE_LOCK

                    if parts is not None and (not parts or any(p not in backupkit.PARTS for p in parts)):
                        return self.json({"ok": False, "error": f"parts must be a non-empty subset of {', '.join(backupkit.PARTS)}"})
                    if _HA_CHANGE_LOCK.locked() or self.installer.busy:
                        return self.json({"ok": False, "error": "a Home Assistant version change or an install is running: try again in a moment"})
                    async with _HA_CHANGE_LOCK:
                        self.installer.busy = True
                        try:
                            await self.hass.async_add_executor_job(backupkit.validate, path)
                            await self.hass.async_add_executor_job(self.updater.cancel_config_change)
                            await self.hass.async_add_executor_job(self.updater.set_desired, HA_VERSION)
                            await self.hass.async_add_executor_job(backupkit.schedule_restore, cfg, name, parts, None, force)
                            await self.hass.async_add_executor_job(ha_import.drop_rebuild, cfg)
                        finally:
                            self.installer.busy = False
                    events.emit("restore", f"{name} scheduled for the next restart ({', '.join(parts) if parts else 'everything'}); "
                                f"the scheduled switch to Home Assistant {boot} was cancelled", backup=name)
                    return self.json({"ok": True, "parts": parts or list(backupkit.PARTS), "cancelled_switch": boot,
                                      "note": "restore is applied by the entrypoint at the next process restart"})
                elif self.updater is not None:
                    # a restore by hand would take the place of the restore or clean start a scheduled switch needs
                    change = (await self.hass.async_add_executor_job(self.updater._read)).get("change")  # noqa: SLF001
                    if isinstance(change, dict) and change.get("mode") in ("restore", "rebuild") and change.get("to") != HA_VERSION:
                        return self.json({"ok": False, "error": f"a switch to Home Assistant {change.get('to')} with a {'configuration restore' if change.get('mode') == 'restore' else 'clean start'} "
                                                                "is scheduled: cancel it on System (choose the running version) before restoring a backup"})
                await self.hass.async_add_executor_job(backupkit.schedule_restore, cfg, name, parts, None, force)
                await self.hass.async_add_executor_job(ha_import.drop_rebuild, cfg)  # the restore replaces a scheduled clean start
                events.emit("restore", f"{name} scheduled for the next restart ({', '.join(parts) if parts else 'everything'})", backup=name)
                return self.json({"ok": True, "parts": parts or list(backupkit.PARTS),
                                  "note": "restore is applied by the entrypoint at the next process restart"})
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        except OSError as err:  # a full disk while backing up, an unreadable backup
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json_message("unknown action", status_code=400)


class RestoreCancelView(ManagerView):
    url = "/api/backups/restore/cancel"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        cancelled, for_version = await self.hass.async_add_executor_job(_cancel_restore_by_hand, self.hass.config.config_dir)
        if for_version:
            return self.json({"ok": False, "for_version": for_version,
                              "error": f"this restore belongs to the scheduled switch to Home Assistant {for_version}: cancel that switch on System "
                                       "(choose the running version), which drops its restore too"})
        return self.json({"ok": True, "cancelled": cancelled})


def _cancel_restore_by_hand(cfg: str) -> tuple[bool, str | None]:
    """Blocking: (cancelled, the version change the restore belongs to).  A version change's own restore stays:
    without it that switch is cancelled by the entrypoint one boot later (its restore "did not happen"), while
    ha.json still shows it scheduled; it goes with the switch (HaUpdater.cancel_config_change).  A leftover
    for a switch that is no longer in ha.json is cancelled like any other.  The schedule is read once and
    cancelled only while it is still that archive's: one scheduled in between is never taken for this one."""
    meta = backupkit._pending_meta(cfg) or {}  # noqa: SLF001 - its archive and its version from one read
    for_version = meta.get("for_version")
    if for_version and backupkit.pending(cfg):
        ha_state = read_json(os.path.join(cfg, backupkit.STATE_DIR, "ha.json"), {})
        change = ha_state.get("change") if isinstance(ha_state, dict) else None
        if isinstance(change, dict) and change.get("to") == for_version:
            return False, str(for_version)
    return backupkit.cancel_restore(cfg, only_zip=meta.get("zip")), None


def _last_restore(hass: HomeAssistant) -> dict[str, Any] | None:
    import json

    try:
        with open(hass.config.path(backupkit.STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            return json.load(fh).get("last_restore")
    except (OSError, ValueError):
        return None
