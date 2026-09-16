"""HTTP views for the manager UI (served by HA's own aiohttp on :8087).

LAN-only admin surface: ``requires_auth = False`` deliberately, the
same way the onboarding views do it.  Add a token later if needed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import re

from aiohttp import web

import backupkit
from homeassistant import data_entry_flow
from homeassistant.const import __version__ as HA_VERSION
from jsonio import ha_vkey
from homeassistant.helpers.http import HomeAssistantView

from .http_util import BadRequest, ManagerView, with_body, _json_object

from . import events, ha_import, notifications
from .flow_page import FLOW_HTML
from .ui import version_info
from .flows import FlowDriver
from .ha_updater import HaUpdater
from .installer import _DOMAIN_RE, Installer
from .mqtt_publisher import MqttPublisher
from .mqtt_rules import FIELDS
from .ui import load_template, render

_HTML = load_template("index")
_SYSTEM_HTML = load_template("system")
_MQTT_HTML = load_template("mqtt")


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")  # \Z: "$" also matches before a trailing newline
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+@-]{0,100}\Z")


class IndexView(ManagerView):
    url = "/"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(_HTML, "/"), content_type="text/html")


class SystemPageView(ManagerView):
    url = "/system"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(_SYSTEM_HTML, "/system"), content_type="text/html")


class MqttPageView(ManagerView):
    url = "/mqtt"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(_MQTT_HTML, "/mqtt"), content_type="text/html")


STATUS_CACHE_S = 10


class StatusView(ManagerView):
    """GET /api/status.  Building it runs the status(ctx) of .py patches and reads files: the UI
    (X-Requested-With: fetch) gets a fresh one; any other caller (a monitor, verify.sh, a link on any
    web page) gets one built at most every STATUS_CACHE_S seconds, one build at a time."""

    url = "/api/status"

    def __init__(self, installer: Installer) -> None:
        self.installer = installer
        self._lock = asyncio.Lock()
        self._cache: tuple[float, dict[str, Any]] | None = None

    async def _build(self) -> dict[str, Any]:
        data = await self.installer.status()
        data["components"] = sorted(self.installer.hass.config.components)
        v = version_info()
        data["manager_version"], data["manager_build"] = v["version"], v["build"]
        self._cache = (time.monotonic(), data)
        return data

    async def get(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") == "fetch":
            return self.json(await self._build())
        async with self._lock:
            if self._cache is None or time.monotonic() - self._cache[0] >= STATUS_CACHE_S:
                await self._build()
            return self.json(self._cache[1])


class SummaryView(ManagerView):
    """GET /api/summary: the few fields the top bar shows on every page
    (no patch status, no store walk, no registry read)."""

    url = "/api/summary"

    def __init__(self, installer: Installer, publisher: MqttPublisher) -> None:
        self.installer = installer
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        d = self.installer.running
        h = self.publisher._health_last or {}
        return self.json({
            "running": {"domain": d, "running_tag": self.installer.running_tag, "loaded": bool(d and d in self.installer.hass.config.components)} if d else None,
            "restart_required": self.installer.state.restart_required,
            "health": h.get("state") or ("stopped" if not d else None),
            "mqtt": {"enabled": self.publisher.config.enabled, "connected": bool(self.publisher.stats.get("connected"))},
            "notifications": notifications.count(self.installer.hass),
            "auth": bool(getattr(self.installer.hass.data.get("integration_manager_auth"), "enabled", False)),
            "manager": version_info(),
            # releases newer than this container (the banner under the top bar), from the manager's last GitHub check
            "manager_update": {"installed": version_info()["version"],
                               "releases": manager.newer_manager_releases() if (manager := getattr(self.publisher, "manager", None)) else []},
        })


class ReleasesView(ManagerView):
    url = "/api/releases"

    def __init__(self, installer: Installer) -> None:
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        # only from the UI: a GET any web page can trigger must not burn the GitHub rate limit
        force = request.query.get("refresh") == "1" and request.headers.get("X-Requested-With") == "fetch"
        domain = request.query.get("domain") or None
        with_notes = request.query.get("notes") == "1"
        try:
            rels = await self.installer.releases(domain=domain, force=force)
            return self.json(rels if with_notes else [{k: v for k, v in r.items() if k != "notes"} for r in rels])
        except Exception as err:  # noqa: BLE001 - GitHub errors surface in the UI
            return self.json_message(f"{type(err).__name__}: {err}", status_code=502)


class RegistryView(ManagerView):
    url = "/api/registry"

    def __init__(self, installer: Installer) -> None:
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        return self.json(self.installer.registry())

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        repo = str(body.get("repo", "")).strip().strip("/")
        if not _REPO_RE.match(repo) or ".." in repo:
            return self.json({"ok": False, "error": "repo must be owner/name"})
        try:
            spec = self.installer.add_to_registry(str(body.get("domain", "")), repo, str(body.get("name") or "")[:80] or None)
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        except OSError as err:  # a full volume: the UI shows the reason instead of aiohttp's HTML 500
            return self.json({"ok": False, "error": f"registry.json could not be written: {type(err).__name__}: {err}"})
        return self.json({"ok": True, "domain": str(body.get("domain", "")).strip().lower(), "spec": spec})


class HaStatusView(ManagerView):
    url = "/api/ha"

    def __init__(self, updater: HaUpdater) -> None:
        self.updater = updater

    async def get(self, request: web.Request) -> web.Response:
        return self.json(await self.updater.status(force=request.query.get("refresh") == "1" and request.headers.get("X-Requested-With") == "fetch"))


_HA_CHANGE_LOCK = asyncio.Lock()


def _ha_change_lock_taken() -> bool:
    """_HA_CHANGE_LOCK cannot be taken without waiting: held, or released to a caller waiting for it that has not
    resumed yet (asyncio's lock goes to that caller first, while ``locked()`` already says False).  What takes the
    lock checks this and busy, then takes it with no await in between: refused, never queued behind another."""
    return _HA_CHANGE_LOCK.locked() or any(not w.cancelled() for w in (_HA_CHANGE_LOCK._waiters or ()))  # noqa: SLF001


def _manual_restore_pending(config_dir: str) -> bool:
    return backupkit.pending(config_dir) and not backupkit.pending_for_version(config_dir)


async def async_change_ha_version(installer: Installer, updater: HaUpdater, target: str, mode: str, source: str,
                                  restore_backup: str | None = None, parts: list[str] | None = None) -> dict[str, Any]:
    """The one way to schedule a Home Assistant version change (System page,
    environment builder, a restore with its backup's version): a backup
    first, then what the target starts with (keep, restore, rebuild: see
    HaActionView.post), recorded in ha.json so the entrypoint applies it only
    to that version and can fall back.  ``restore_backup`` restores exactly
    that backup (``parts`` of it, everything by default) when ``target``
    boots, in either direction.  Raises ValueError; OSError passes through."""
    hass = installer.hass
    cfg = hass.config.config_dir
    if _ha_change_lock_taken():
        raise ValueError("a Home Assistant version change, a restore or a full rollback is being prepared: try again in a moment")
    async with _HA_CHANGE_LOCK:
        if mode not in ("keep", "restore", "rebuild"):
            raise ValueError("config must be keep, restore or rebuild")
        if target == HA_VERSION:
            raise ValueError(f"Home Assistant {target} is already running")
        if restore_backup is not None and mode != "restore":
            raise ValueError("a chosen backup is restored with config=restore")
        if mode != "keep" and restore_backup is None and ha_vkey(target) >= ha_vkey(HA_VERSION):
            raise ValueError(f"config={mode} only applies to a downgrade")
        if restore_backup is not None and parts is not None and (not parts or "storage" not in parts):
            # the switch is done only when .storage came back with it (entrypoint.apply_config_changes)
            raise ValueError(f"switching to Home Assistant {target} with a backup needs its .storage: restore everything, or a selection with .storage")
        if installer.busy:
            raise ValueError("another action is running (an install, start, stop, import, restore or full rollback): try again in a moment")
        installer.busy = True  # right away: nothing may start an install while this change is prepared
        try:
            if mode != "keep" and await hass.async_add_executor_job(_manual_restore_pending, cfg):
                raise ValueError("a restore scheduled on System is waiting for the restart: cancel it first")
            restore = None
            if mode == "restore" and restore_backup is not None:
                restore = await hass.async_add_executor_job(backupkit.describe, cfg, restore_backup)
            elif mode == "restore":
                restore = await hass.async_add_executor_job(updater.config_backup_for, target)
                if restore is None:
                    raise ValueError(f"no backup made on Home Assistant {target} or older: choose rebuild or keep")
            if mode == "rebuild":
                from .import_views import _IMPORT_LOCK

                if _IMPORT_LOCK.locked():  # an upload or import writes the same area (no summary yet while an upload streams)
                    raise ValueError("an import or upload from a Home Assistant backup is running: wait for it to finish")
                pending_import = await hass.async_add_executor_job(ha_import.load_summary, cfg)
                if pending_import and pending_import.get("type") != ha_import.REBUILD_TYPE:
                    raise ValueError("an import from a Home Assistant backup is waiting on System: apply or clear it first")
            restore_parts = (parts or list(backupkit.PARTS)) if restore_backup is not None else ["storage"]
            backup = await installer.async_backup(label=f"pre-ha-{target}")
            rebuild = None
            if restore is not None:
                # scheduled (and validated) before anything of an older change is dropped
                await hass.async_add_executor_job(backupkit.schedule_restore, cfg, restore["name"], restore_parts, target)
                await hass.async_add_executor_job(ha_import.drop_rebuild, cfg)
            elif mode == "rebuild":
                if _IMPORT_LOCK.locked():  # started during the backup; checked and taken with no await in between
                    raise ValueError("an import or upload from a Home Assistant backup is running: wait for it to finish")
                async with _IMPORT_LOCK:
                    await hass.async_add_executor_job(updater.cancel_config_change)  # an older change's preparations
                    rebuild = await hass.async_add_executor_job(ha_import.stage_rebuild, cfg, backup["name"], installer.running, HA_VERSION, target)
            else:
                await hass.async_add_executor_job(updater.cancel_config_change)  # an older change's preparations
            state = await hass.async_add_executor_job(updater.set_desired, target, {"to": target, "mode": mode, "backup": backup["name"],
                                                                                   "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                                                   **({"parts": restore_parts} if restore is not None else {})})
            await hass.async_add_executor_job(backupkit.prune, cfg, installer.settings.backup_keep,
                                              installer.protected_backups() | {backup["name"]})
        finally:
            installer.busy = False
    warnings: list[str] = []
    running_min = installer.min_ha_of(installer.running, installer.running_tag) if installer.running else None
    if running_min and ha_vkey(target) < ha_vkey(running_min):
        warnings.append(f"{installer.running} {installer.running_tag} declares Home Assistant {running_min} or newer (hacs.json); "
                        f"{target} is older: it may not load. Switch the integration to a version that supports {target} first")
    if mode == "keep" and ha_vkey(target) < ha_vkey(HA_VERSION):
        warnings.append(f"Home Assistant only migrates its configuration forward: if {HA_VERSION} changed a storage format, {target} "
                        "cannot start and the container falls back after 3 attempts (restore or rebuild avoid that)")
    note = {"keep": "", "restore": f"; configuration restored from {restore['name'] if restore else ''}",
            "rebuild": f"; clean start, {installer.running or 'no integration'} rebuilt after the boot"}[mode]
    events.emit("ha", f"Home Assistant {target} wanted ({source}), backup {backup['name']}{note}; applied at the next restart",
                version=target, backup=backup["name"], config=mode)
    return {"desired": state.get("desired"), "backup": backup["name"], "config": mode,
            "restore": restore["name"] if restore else None, "rebuild": rebuild, "warnings": warnings}


class HaActionView(ManagerView):
    url = "/api/ha/{action}"

    def __init__(self, updater: HaUpdater, installer: Installer) -> None:
        self.updater = updater
        self.installer = installer

    async def post(self, request: web.Request, action: str) -> web.Response:
        """Switch the Home Assistant version at the next restart.  A backup
        of the current configuration is always taken first.  Home Assistant
        migrates its storage forward only, so on a downgrade ``config`` says
        what the older version starts with:

        * ``restore``: ``.storage`` from the newest backup made on the target
          version or an older one;
        * ``rebuild``: an empty ``.storage``, then the running integration's
          config entries, store files and registry customisations are
          imported again from the backup taken now;
        * ``keep`` (the only choice on an upgrade): the configuration as it is.

        What a change prepares applies only when its version boots.  Asking
        for the running version cancels a scheduled change.
        """
        if request.content_type != "application/json":
            return self.json_message("Content-Type must be application/json", status_code=400)
        if action not in ("update", "rollback"):
            return self.json_message("unknown action", status_code=400)
        hass = self.installer.hass
        try:
            body = await _json_object(request)
            if action == "update":
                target = str(body.get("version", "")).strip()
                if target != HA_VERSION:
                    await self.updater.validate(target)
            else:
                target = await hass.async_add_executor_job(self.updater.previous_version)
            if target == HA_VERSION:
                desired = (await hass.async_add_executor_job(self.updater._read)).get("desired")
                if not desired or desired == HA_VERSION:
                    raise ValueError(f"Home Assistant {target} is already running")
                if self.installer.busy or _ha_change_lock_taken():
                    raise ValueError("an install/start or a version change is running: try again in a moment")
                async with _HA_CHANGE_LOCK:  # held across both writes: a change scheduled in between would be overwritten
                    dropped = await hass.async_add_executor_job(self.updater.cancel_config_change)
                    await hass.async_add_executor_job(self.updater.set_desired, HA_VERSION)
                events.emit("ha", f"scheduled switch to Home Assistant {desired} cancelled" + (f"; dropped: {', '.join(dropped)}" if dropped else ""),
                            version=HA_VERSION)
                return self.json({"ok": True, "desired": HA_VERSION, "cancelled": desired, "dropped": dropped})
            result = await async_change_ha_version(self.installer, self.updater, target, str(body.get("config") or "keep"), action)
        except (ValueError, BadRequest) as err:
            return self.json({"ok": False, "error": str(err)})
        except OSError as err:  # a full disk while backing up, an unreadable backup
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json({"ok": True, **result})


class InstallView(ManagerView):
    url = "/api/install"

    def __init__(self, installer: Installer, publisher: MqttPublisher | None = None) -> None:
        self.installer = installer
        self.publisher = publisher

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        tag = str(body.get("tag", "")).strip()
        domain = body.get("domain") or None
        if not tag or not _TAG_RE.match(tag) or ".." in tag:
            return self.json({"ok": False, "error": f"invalid tag {tag[:80]!r}"})
        if domain is not None and not (isinstance(domain, str) and _DOMAIN_RE.match(domain)):
            return self.json({"ok": False, "error": "invalid domain"})
        res = await self.installer.install(tag, domain=domain, replace=bool(body.get("replace")))
        if res.get("ok") and res.get("replaced") and self.publisher is not None:
            await self.publisher.async_reconnect()  # the MQTT identity follows the new integration
        return self.json(res)


class RestartView(ManagerView):
    url = "/api/restart"

    def __init__(self, installer: Installer) -> None:
        self.installer = installer

    async def post(self, request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            return self.json_message("Content-Type must be application/json", status_code=400)
        return self.json(await self.installer.restart())  # refuses while an install/start runs


# ----- config / options flows ---------------------------------------------


class FlowPageView(ManagerView):
    """The per-integration Config page.  ``/flow`` is kept as an alias."""

    url = "/config"
    extra_urls = ["/flow"]

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(FLOW_HTML, "/config"), content_type="text/html")


class FlowStartView(ManagerView):
    url = "/api/flow/start"

    def __init__(self, flows: FlowDriver, installer: Installer) -> None:
        self.flows = flows
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        domain = str(body.get("domain", "")).strip()
        if not re.fullmatch(r"[a-z0-9_]+", domain):
            return self.json_message("domain required", status_code=400)
        installer = self.installer
        if domain in installer.state.installed and domain != installer.running:
            # only the running version's code is deployed and importable: a
            # flow always belongs to the version that runs
            return self.json_message(f"{domain} is not running: start it first, the config flow is the running version's", status_code=409)
        source, entry_id = str(body.get("source") or "user"), str(body.get("entry_id") or "")
        if source not in ("user", "reconfigure") or (source == "reconfigure" and not entry_id):
            return self.json_message("source must be user, or reconfigure with an entry_id", status_code=400)
        manifest = installer.installed_manifest(domain) if domain == installer.running else None
        if manifest is not None and not manifest.get("config_flow"):
            return self.json_message(f"{domain} {installer.running_tag or ''} has no config flow: it is configured in YAML (Integration page)".replace("  ", " "), status_code=400)
        try:
            return self.json(await self.flows.start(domain, source, entry_id or None))
        except data_entry_flow.UnknownHandler:
            return self.json_message(f"{domain} has no config flow", status_code=400)
        except Exception as err:  # noqa: BLE001 - surfaced to the UI
            return self.json_message(f"{type(err).__name__}: {err}", status_code=500)


class FlowProgressView(ManagerView):
    url = "/api/flow/progress"

    def __init__(self, flows: FlowDriver) -> None:
        self.flows = flows

    async def get(self, request: web.Request) -> web.Response:
        return self.json(self.flows.in_progress())


class FlowResourceView(ManagerView):
    url = "/api/flow/{flow_id}"

    def __init__(self, flows: FlowDriver) -> None:
        self.flows = flows

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], flow_id: str) -> web.Response:
        try:
            return self.json(await self.flows.configure(flow_id, body.get("user_input")))
        except data_entry_flow.UnknownFlow:
            return self.json_message("unknown flow (finished or aborted)", status_code=404)
        except data_entry_flow.InvalidData as err:  # per-field errors, as HA's own flow view answers
            return self.json({"type": "invalid_data", "errors": err.schema_errors}, status_code=400)
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"{type(err).__name__}: {err}", status_code=500)

    async def delete(self, request: web.Request, flow_id: str) -> web.Response:
        try:
            self.flows.abort(flow_id)
        except data_entry_flow.UnknownFlow:
            return self.json_message("unknown flow", status_code=404)
        return self.json({"ok": True})


class OptionsResourceView(ManagerView):
    url = "/api/options/{flow_id}"

    def __init__(self, flows: FlowDriver) -> None:
        self.flows = flows

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], flow_id: str) -> web.Response:
        try:
            return self.json(
                await self.flows.options_configure(flow_id, body.get("user_input"))
            )
        except data_entry_flow.UnknownFlow:
            return self.json_message("unknown flow (finished or aborted)", status_code=404)
        except data_entry_flow.InvalidData as err:  # per-field errors, as the config flow answers
            return self.json({"type": "invalid_data", "errors": err.schema_errors}, status_code=400)
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"{type(err).__name__}: {err}", status_code=500)

    async def delete(self, request: web.Request, flow_id: str) -> web.Response:
        try:
            self.flows.options_abort(flow_id)
        except data_entry_flow.UnknownFlow:
            return self.json_message("unknown flow", status_code=404)
        return self.json({"ok": True})


class EntriesView(ManagerView):
    url = "/api/entries"

    def __init__(self, flows: FlowDriver) -> None:
        self.flows = flows

    async def get(self, request: web.Request) -> web.Response:
        domain = request.query.get("domain") or None
        return self.json(self.flows.entries(domain))


class EntryActionView(ManagerView):
    url = "/api/entries/{entry_id}/{action}"

    def __init__(self, flows: FlowDriver) -> None:
        self.flows = flows

    async def post(self, request: web.Request, entry_id: str, action: str) -> web.Response:
        if request.content_type != "application/json":
            return self.json_message("Content-Type must be application/json", status_code=400)
        try:
            if action == "options":
                try:
                    return self.json(await self.flows.options_start(entry_id))
                except data_entry_flow.UnknownHandler:
                    entry = self.flows.hass.config_entries.async_get_entry(entry_id)
                    state = entry.state.value if entry else "unknown entry"
                    return self.json_message(f"this entry has no options flow right now ({state})", status_code=409)
            if action == "reload":
                return self.json({"ok": await self.flows.reload_entry(entry_id)})
            if action == "delete":
                return self.json(await self.flows.remove_entry(entry_id))
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"{type(err).__name__}: {err}", status_code=500)
        return self.json_message("unknown action", status_code=400)


# ----- MQTT publisher ----------------------------------------------


class MqttConfigView(ManagerView):
    url = "/api/mqtt/config"

    def __init__(self, publisher: MqttPublisher) -> None:
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        return self.json(self.publisher.public_config())

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await _json_object(request)
            await self.publisher.async_save(body)
        except (BadRequest, ValueError) as err:
            return self.json({"ok": False, "error": str(err)})
        await self.publisher.async_reconnect()
        return self.json({"ok": True, "config": self.publisher.public_config()})


class MqttDiscoveryPreviewView(ManagerView):
    """Dry run of the device-based discovery payloads (what the consuming
    HA would receive), regardless of discovery_enabled."""

    url = "/api/mqtt/discovery"

    def __init__(self, publisher: MqttPublisher) -> None:
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        return self.json(self.publisher.discovery_preview())


class MqttRulesView(ManagerView):
    """GET/POST /api/mqtt/rules: the whole rules object (see mqtt_rules.py)."""

    url = "/api/mqtt/rules"

    def __init__(self, publisher: MqttPublisher) -> None:
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        return self.json({"rules": self.publisher.rules.rules, "fields": list(FIELDS)})

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        try:
            self.publisher.rules.replace_all(body.get("rules") or {})  # on the loop: readers live here
            await self.publisher.rules.async_save()
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        res = await self.publisher.async_apply_rules()
        return self.json({"ok": True, "rules": self.publisher.rules.rules, **res})


class MqttCommandsView(ManagerView):
    """GET /api/mqtt/commands: the last 200 commands and service calls
    received over MQTT with their outcome, duration and dedup verdict."""

    url = "/api/mqtt/commands"

    def __init__(self, publisher: MqttPublisher) -> None:
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        return self.json({"commands": self.publisher.recent_commands(200), "dedup_window_s": 300})


class MqttStatusView(ManagerView):
    url = "/api/mqtt/status"

    def __init__(self, publisher: MqttPublisher) -> None:
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        return self.json(self.publisher.status())


class MqttActionView(ManagerView):
    url = "/api/mqtt/{action}"

    def __init__(self, publisher: MqttPublisher) -> None:
        self.publisher = publisher

    async def post(self, request: web.Request, action: str) -> web.Response:
        if request.content_type != "application/json":
            return self.json_message("Content-Type must be application/json", status_code=400)
        if action == "republish":
            return self.json({"ok": True, "published": await self.publisher.async_republish_all()})
        if action == "reconnect":
            await self.publisher.async_reconnect()
            return self.json({"ok": True, "status": self.publisher.status()})
        return self.json_message("unknown action", status_code=400)
