"""Backup & restore endpoints (the logic lives in /app/backupkit.py, shared
with entrypoint.py which applies a scheduled restore before HA starts)."""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from jsonio import fsync_dir, ha_vkey, read_json

from . import events, ha_import
from .http_util import ManagerView, with_body

import backupkit  # /app/backupkit.py (/app is on sys.path)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip\Z")  # \Z: "$" also matches before a trailing newline
_NAME_MAX = 125  # the longest name _NAME_RE takes
MAX_UPLOAD = 200 * 1024 * 1024


def _name_ok(name: str) -> bool:
    if ".." in name:
        return False
    if _NAME_RE.match(name):
        return True
    # a backup made before labels were reduced to ASCII in its file name (backupkit.file_label) kept the letters
    # of any alphabet str.isalnum() takes ("...-înainte.zip"): still downloadable, restorable and deletable
    stem = name[:-len(".zip")]
    return name.endswith(".zip") and len(name) <= _NAME_MAX and stem[:1].isalnum() \
        and all(ch.isalnum() or ch in "._-" for ch in stem)


def _attachment(name: str) -> str:
    """Content-Disposition for a download: an older backup's name may not be ASCII (RFC 6266 filename*)."""
    if name.isascii():
        return f'attachment; filename="{name}"'
    return f"attachment; filename=\"{backupkit.file_label(name[:-len('.zip')])}.zip\"; filename*=UTF-8''{quote(name, safe='')}"


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
        fh = tmp = reserved = None

        def _open() -> tuple[Any, str]:
            os.makedirs(bdir, exist_ok=True)
            fd, path = tempfile.mkstemp(dir=bdir, prefix=".upload-", suffix=".zip.tmp")
            return os.fdopen(fd, "wb"), path

        def _discard() -> None:
            if fh is not None and not fh.closed:  # closed already once the upload was complete
                try:
                    fh.close()  # flushes what a full volume did not take: fails again
                except OSError:
                    pass
            if not ok:
                for leftover in (tmp, reserved):
                    try:
                        if leftover:
                            os.remove(leftover)
                    except OSError:
                        pass

        try:
            fh, tmp = await self.hass.async_add_executor_job(_open)
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
            name = await self.hass.async_add_executor_job(backupkit.reserve_name, bdir, name[:-len(".zip")], _NAME_MAX)
            reserved = os.path.join(bdir, name)
            if not _name_ok(name):
                return self.json({"ok": False, "error": "bad file name"})
            await self.hass.async_add_executor_job(os.replace, tmp, reserved)
            await self.hass.async_add_executor_job(fsync_dir, bdir)
            ok = True
        except OSError as err:  # a full volume: answered with the reason, as a backup that cannot be written
            return self.json({"ok": False, "error": f"the upload could not be stored: {type(err).__name__}: {err}"})
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
        return web.FileResponse(path, headers={"Content-Disposition": _attachment(name)})

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
                    return self.json({"ok": False, "error": "this backup is still needed: it is the way back of a full rollback, of a scheduled or failed Home Assistant version change or clean start, "
                                                        "of a scheduled restore or a restore that could not be put back, or the copy taken before a restore in the last 7 days"})
                await self.hass.async_add_executor_job(os.remove, path)
                return self.json({"ok": True})
            if action == "restore":
                if self.installer.busy:
                    return self.json({"ok": False, "error": "another action is running (an install, start, stop, import, restore or full rollback): try again in a moment"})
                parts = body.get("parts")
                if parts is not None and not (isinstance(parts, list) and all(isinstance(x, str) for x in parts)):
                    return self.json({"ok": False, "error": "parts must be a list"})
                # Home Assistant only migrates a configuration forward: a backup made on another version is
                # restored on the version that boots ("keep", it migrates forward) or together with a switch
                # to the version it was made on ("backup", the only way for a backup of a newer version)
                choice = str(body.get("ha") or "keep")
                if choice not in ("keep", "backup"):
                    return self.json({"ok": False, "error": "ha must be keep or backup"})
                if parts is not None and ("storage" in parts) != ("manager" in parts):
                    # the config entries (.storage) and what the manager runs (state.json) come from one backup together:
                    # the manager of one integration over the entries of another deploys it next to them, both run, and
                    # everything is published under the identity of the one the manager names
                    then = await self.hass.async_add_executor_job(backupkit.backup_domain, cfg, name)
                    now = getattr(self.installer, "running", None)
                    if then is not backupkit.UNKNOWN_DOMAIN and then != now:
                        alone, without = (".storage", "the manager part") if "storage" in parts else ("the manager part", ".storage")
                        return self.json({"ok": False, "error": f"{name} was made while {then or 'no integration'} ran, and {now or 'no integration'} runs now: "
                                                                f"{alone} without {without} would leave the configuration of one and the manager of the other. "
                                                                "Restore .storage + manager, or everything"})
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
                    if parts is not None and (not parts or any(p not in backupkit.PARTS for p in parts)):
                        return self.json({"ok": False, "error": f"parts must be a non-empty subset of {', '.join(backupkit.PARTS)}"})
                cancel_switch = choice == "backup" and bool(made_on) and made_on != boot
                # checked and scheduled as one step, like a version change (views.async_change_ha_version): under its
                # lock, with busy reserved.  The archive's own lock covers each write only: a change prepared between
                # this restore's checks and its schedule replaced it, or was replaced by it, and both answered ok.
                # Refused, never waited for: what holds these is short (a schedule) or long and unrelated (an install).
                from .views import _HA_CHANGE_LOCK, _ha_change_lock_taken

                if _ha_change_lock_taken() or self.installer.busy:
                    return self.json({"ok": False, "error": "a Home Assistant version change or another action is running (an install, start, stop, import, restore or full rollback): try again in a moment"})
                async with _HA_CHANGE_LOCK:
                    self.installer.busy = True
                    try:
                        # the version checked above is still the one that boots next (a change prepared meanwhile)
                        if (await self.hass.async_add_executor_job(backupkit.boot_version, cfg) or HA_VERSION) != boot:
                            return self.json({"ok": False, "error": "the Home Assistant version that boots next changed meanwhile: check System and try again"})
                        # a full rollback already recorded the previous version: another restore in place of its own would
                        # boot that older code on the configuration the newer version migrated
                        rollback = getattr(getattr(self.installer, "state", None), "rollback_backup", None)
                        if rollback and (await self.hass.async_add_executor_job(backupkit._pending_meta, cfg) or {}).get("name") == rollback:  # noqa: SLF001
                            return self.json({"ok": False, "error": f"a full rollback restores {rollback} at the next restart: restart to finish it before restoring another backup"})
                        if cancel_switch:
                            await self.hass.async_add_executor_job(backupkit.validate, path)
                            await self.hass.async_add_executor_job(self.updater.cancel_config_change)
                            await self.hass.async_add_executor_job(self.updater.set_desired, HA_VERSION)
                        elif self.updater is not None:
                            # a restore by hand would take the place of the restore or clean start a scheduled switch needs
                            change = (await self.hass.async_add_executor_job(self.updater._read)).get("change")  # noqa: SLF001
                            if isinstance(change, dict) and change.get("mode") in ("restore", "rebuild") and change.get("to") != HA_VERSION:
                                return self.json({"ok": False, "error": f"a switch to Home Assistant {change.get('to')} with a {'configuration restore' if change.get('mode') == 'restore' else 'clean start'} "
                                                                        "is scheduled: cancel it on System (choose the running version) before restoring a backup"})
                        await self.hass.async_add_executor_job(backupkit.schedule_restore, cfg, name, parts, None, force)
                        await self.hass.async_add_executor_job(ha_import.drop_rebuild, cfg)  # the restore replaces a scheduled clean start
                    finally:
                        self.installer.busy = False
                if cancel_switch:
                    events.emit("restore", f"{name} scheduled for the next restart ({', '.join(parts) if parts else 'everything'}); "
                                f"the scheduled switch to Home Assistant {boot} was cancelled", backup=name)
                    return self.json({"ok": True, "parts": parts or list(backupkit.PARTS), "cancelled_switch": boot,
                                      "note": "restore is applied by the entrypoint at the next process restart"})
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

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        # under the version-change lock with busy reserved, like a restore by hand: a version change schedules its
        # restore before ha.json records the change, and a cancel in between took that restore as one made by hand
        # (the switch was then dropped at the boot as a restore that did not happen).  Refused, never waited for.
        from .views import _HA_CHANGE_LOCK, _ha_change_lock_taken

        if _ha_change_lock_taken() or self.installer.busy:
            return self.json({"ok": False, "error": "a Home Assistant version change or another action is running (an install, start, stop, import, restore or full rollback): try again in a moment"})
        async with _HA_CHANGE_LOCK:
            self.installer.busy = True
            try:
                # a full rollback already selected the version it goes back to: cancelled alone, its restore would leave
                # that code to boot on the config entries the version it left migrated (migration_error)
                rollback = self.installer.state.rollback_backup
                meta = await self.hass.async_add_executor_job(backupkit._pending_meta, self.hass.config.config_dir) or {}  # noqa: SLF001
                if rollback and meta.get("name") == rollback:
                    undo = getattr(self.installer, "_rollback_undo", None)
                    undo = undo if undo and undo[2] == meta.get("zip") else None
                    target = " ".join(x for x in (getattr(self.installer.state, "domain", None), getattr(self.installer, "running_tag", None)) if x)
                    return self.json({"ok": False, "rollback": rollback,
                                      "error": f"this restore belongs to a full rollback, which already selected {target or 'the version it goes back to'}"
                                               f"{', the version it goes back to' if target else ''}: restart to finish it"
                                               + (f", or start {undo[0]} {undo[1]} again on Integration to undo the rollback (that drops this restore)" if undo else "")})
                cancelled, for_version, _name = await self.hass.async_add_executor_job(_cancel_restore_by_hand, self.hass.config.config_dir)
                if for_version:
                    return self.json({"ok": False, "for_version": for_version,
                                      "error": f"this restore belongs to the scheduled switch to Home Assistant {for_version}: cancel that switch on System "
                                               "(choose the running version), which drops its restore too"})
            finally:
                self.installer.busy = False
        return self.json({"ok": True, "cancelled": cancelled})


def _cancel_restore_by_hand(cfg: str) -> tuple[bool, str | None, str | None]:
    """Blocking: (cancelled, the version change the restore belongs to, the backup that was scheduled).  A version
    change's own restore stays: without it that switch is cancelled by the entrypoint one boot later (its restore
    "did not happen"), while ha.json still shows it scheduled; it goes with the switch
    (HaUpdater.cancel_config_change).  backupkit checks and cancels under the schedule lock, so a restore scheduled
    in between is never taken for this one, and only the schedule whose backup is named here is cancelled."""
    meta = backupkit._pending_meta(cfg) or {}  # noqa: SLF001
    try:
        return backupkit.cancel_restore(cfg, only_zip=meta.get("zip"), by_hand=True), None, meta.get("name")
    except backupkit.BelongsToVersionChange as err:
        return False, str(err.for_version), meta.get("name")


def _last_restore(hass: HomeAssistant) -> dict[str, Any] | None:
    import json

    try:
        with open(hass.config.path(backupkit.STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            return json.load(fh).get("last_restore")
    except (OSError, ValueError):
        return None
