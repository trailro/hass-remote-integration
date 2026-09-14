"""Endpoints for the version store / running-integration model, release
previews and user patches."""

from __future__ import annotations

from typing import Any

import os
import re

from aiohttp import web
from homeassistant.core import HomeAssistant

from . import events, patches
from .installer import Installer
from .logfiles_page import clean_log_format
from .settings import DEFAULTS, HEALTH_MODES
from .mqtt_publisher import MqttPublisher
from .http_util import ManagerView, with_body

_DOMAIN_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
MAX_PATCH = 2 * 1024 * 1024


def _tag_ok(tag: str) -> bool:
    return bool(_TAG_RE.match(tag)) and ".." not in tag


class RunView(ManagerView):
    """POST /api/run/start {domain, tag?} | POST /api/run/stop: the ONE
    running integration.  The MQTT publisher follows the identity."""

    url = "/api/run/{action}"

    def __init__(self, installer: Installer, publisher: MqttPublisher) -> None:
        self.installer = installer
        self.publisher = publisher

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], action: str) -> web.Response:
        if action == "start":
            domain = str(body.get("domain", "")).strip()
            tag = body.get("tag")
            if not _DOMAIN_RE.match(domain) or (tag is not None and not (isinstance(tag, str) and _tag_ok(tag))):
                return self.json({"ok": False, "error": "domain (and optional tag) required"})
            res = await self.installer.start(domain, tag)
        elif action == "stop":
            res = await self.installer.stop()
        elif action == "cancel_pending_start":
            return self.json({"ok": True, "cancelled": self.installer.cancel_pending_start()})
        else:
            return self.json_message("unknown action", status_code=400)
        if res.get("ok"):
            if action == "start":
                await self.publisher.async_after_start(res)
            else:
                await self.publisher.async_reconnect()
            res["mqtt"] = {"connected": self.publisher.stats.get("connected"), "base_topic": self.publisher.base_topic,
                           "connect_error": self.publisher.stats.get("connect_error")}
        return self.json(res)


class InstalledActionView(ManagerView):
    """POST /api/installed/{domain}/{uninstall|rollback_full|remove_version}"""

    url = "/api/installed/{domain}/{action}"

    def __init__(self, installer: Installer, publisher: MqttPublisher) -> None:
        self.installer = installer
        self.publisher = publisher

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], domain: str, action: str) -> web.Response:
        if not _DOMAIN_RE.match(domain):
            return self.json({"ok": False, "error": "bad domain"})
        if action == "uninstall":
            was_running = domain == self.installer.running
            res = await self.installer.uninstall(domain)
            if res.get("ok"):
                if was_running:
                    await self.publisher.async_reconnect()  # disconnects (no identity), retained offline
                from .installer import instance_key

                res["retained_cleared"] = await self.publisher.async_clear_identity(instance_key(domain) or "")
            return self.json(res)
        if action == "rollback_full":
            return self.json(await self.installer.rollback_full(domain))
        if action == "remove_version":
            tag = str(body.get("tag", ""))
            if not _tag_ok(tag):
                return self.json({"ok": False, "error": "tag required"})
            return self.json(await self.installer.remove_version(domain, tag))
        return self.json_message("unknown action", status_code=400)


class ReleasePreviewView(ManagerView):
    url = "/api/releases/preview"

    def __init__(self, installer: Installer) -> None:
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        domain = request.query.get("domain", "")
        tag = request.query.get("tag", "")
        if not _DOMAIN_RE.match(domain) or not _tag_ok(tag):
            return self.json({"ok": False, "error": "domain and tag required"})
        try:
            return self.json({"ok": True, **await self.installer.preview(domain, tag)})
        except Exception as err:  # noqa: BLE001
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})


class YamlView(ManagerView):
    """GET/POST /api/yaml/{domain}: the integration's YAML section
    (integration_manager/yaml/<domain>.yaml), applied at boot to the running
    integration like a configuration.yaml section."""

    url = "/api/yaml/{domain}"

    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request, domain: str) -> web.Response:
        if not _DOMAIN_RE.match(domain):
            return self.json({"ok": False, "error": "bad domain"})
        text = await self.hass.async_add_executor_job(self.installer.yaml_read, domain)
        return self.json({"ok": True, "domain": domain, "text": text, "applied_at_boot": domain == self.installer.running,
                          "loaded_config": domain in self.hass.config.components})

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], domain: str) -> web.Response:
        if not _DOMAIN_RE.match(domain):
            return self.json({"ok": False, "error": "bad domain"})
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > 512 * 1024:
            return self.json({"ok": False, "error": "text must be a string under 512 KB"})
        try:
            res = await self.hass.async_add_executor_job(self.installer.yaml_write, domain, text)
        except Exception as err:  # noqa: BLE001 - yaml errors of every flavour go to the UI
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        if domain == self.installer.running:
            self.installer.state.restart_required = True
            self.installer.state.last_action = f"YAML config of {domain} saved; applies at the next restart"
            self.installer._save_state()
        events.emit("yaml", f"YAML config of {domain} " + ("removed" if res.get("removed") else f"saved ({res.get('keys', 0)} keys)")
                    + ("; restart required" if domain == self.installer.running else ""), domain=domain)
        return self.json({"ok": True, **res, "restart_required": domain == self.installer.running})


class PatchesView(ManagerView):
    url = "/api/patches/{domain}"

    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request, domain: str) -> web.Response:
        if not _DOMAIN_RE.match(domain):
            return self.json({"ok": False, "error": "bad domain"})
        cfg = self.hass.config.config_dir
        rec = self.installer.state.installed.get(domain, {})
        tag = rec.get("running_tag") if domain == self.installer.running else None
        st = await self.hass.async_add_executor_job(
            patches.status, cfg, domain, self.installer.site_packages_for(domain), self.installer.component_dir(domain), tag)
        return self.json({"ok": True, "domain": domain, "patches": st, "bundled_dir": patches.bundled_dir(domain),
                          "running_tag": tag, "versions": sorted(rec.get("versions", {})), "dir": patches.patch_dir(cfg, domain)})


class PatchUploadView(ManagerView):
    url = "/api/patches/{domain}/upload"

    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer

    async def post(self, request: web.Request, domain: str) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            return self.json_message("X-Requested-With: fetch required", status_code=400)
        if not _DOMAIN_RE.match(domain):
            return self.json({"ok": False, "error": "bad domain"})
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return self.json({"ok": False, "error": "form field 'file' expected"})
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(field.filename or ""))
        if not patches.valid_name(name):
            return self.json({"ok": False, "error": "file must be <name>.py or <name>.patch"})
        data = b""
        while chunk := await field.read_chunk(1 << 16):
            data += chunk
            if len(data) > MAX_PATCH:
                return self.json({"ok": False, "error": "patch too large"})
        text = data.decode("utf-8", errors="replace")
        if (err := patches.validate(name, text)):
            return self.json({"ok": False, "error": err})
        d = patches.patch_dir(self.hass.config.config_dir, domain)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(data)
        return self.json({"ok": True, "name": name, "scope": patches.version_scope(text)})


class PatchReadView(ManagerView):
    """GET /api/patch_editor/<domain>?name=: a patch's text for the editor
    (a bundled one too: saving it stores the user copy that overrides it)."""

    url = "/api/patch_editor/{domain}"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, domain: str) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            return self.json_message("X-Requested-With: fetch required", status_code=400)
        name = request.query.get("name", "")
        if not _DOMAIN_RE.match(domain) or not patches.valid_name(name):
            return self.json({"ok": False, "error": "bad domain or patch name"})
        cfg = self.hass.config.config_dir

        def _read() -> str:
            with open(patches.patch_path(cfg, domain, name), encoding="utf-8", errors="replace") as fh:
                return fh.read(MAX_PATCH)

        try:
            text = await self.hass.async_add_executor_job(_read)
        except OSError:
            return self.json({"ok": False, "error": "no such patch"})
        return self.json({"ok": True, "name": name, "text": text, "bundled": patches.is_bundled(cfg, domain, name)})


class PatchEditView(ManagerView):
    """POST /api/patch_editor/<domain>/check: dry run of {name, text}
    against the deployed code (patches.check), nothing written.
    POST /api/patch_editor/<domain>/save: validated like an upload, stored
    in the user patch directory."""

    url = "/api/patch_editor/{domain}/{op}"

    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], domain: str, op: str) -> web.Response:
        name, text = body.get("name"), body.get("text")
        if not _DOMAIN_RE.match(domain) or not isinstance(name, str) or not isinstance(text, str):
            return self.json({"ok": False, "error": "domain, name and text required"})
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            return self.json({"ok": False, "error": "text is not valid UTF-8"})
        if size > MAX_PATCH:
            return self.json({"ok": False, "error": "patch too large"})
        name = name.strip()
        cfg = self.hass.config.config_dir
        if op == "check":
            tag = self.installer.running_tag if domain == self.installer.running else None
            res = await self.hass.async_add_executor_job(patches.check, cfg, domain, self.installer.site_packages_for(domain),
                                                         self.installer.component_dir(domain), tag, name, text)
            return self.json(res)
        if op != "save":
            return self.json_message("unknown operation", status_code=400)
        if (err := patches.validate(name, text)):
            return self.json({"ok": False, "error": err})
        overrides = patches.is_bundled(cfg, domain, name)
        if body.get("create") and os.path.isfile(os.path.join(patches.patch_dir(cfg, domain), name)):
            return self.json({"ok": False, "error": f"a patch named {name} exists already: choose another name or edit that one"})

        def _write() -> None:
            d = patches.patch_dir(cfg, domain)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name + ".tmp"), "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(os.path.join(d, name + ".tmp"), os.path.join(d, name))

        await self.hass.async_add_executor_job(_write)
        return self.json({"ok": True, "name": name, "scope": patches.version_scope(text), "overrides_bundled": overrides})


class PatchActionView(ManagerView):
    url = "/api/patches/{domain}/{name}/{action}"

    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], domain: str, name: str, action: str) -> web.Response:
        if not _DOMAIN_RE.match(domain):
            return self.json({"ok": False, "error": "bad domain"})
        cfg = self.hass.config.config_dir
        if action == "apply":
            if domain != self.installer.running:
                return self.json({"ok": False, "error": f"{domain} is not running: patches are applied when it starts"})
            if self.installer.busy:
                return self.json({"ok": False, "error": "another action is running"})
            self.installer.busy = True  # a start must not redeploy files while patches are written
            try:
                out = await self.hass.async_add_executor_job(self.installer._apply_patches, domain)
            finally:
                self.installer.busy = False
            changed = any(part.strip().endswith(": applied") for part in out.split(";"))
            if changed and domain in self.hass.config.components:
                # the module is already imported: the patched file takes effect at the restart
                self.installer.state.restart_required = True
                self.installer._save_state()
            return self.json({"ok": True, "result": out, "restart_required": changed and domain in self.hass.config.components})
        if not patches.valid_name(name):
            return self.json({"ok": False, "error": "bad name"})
        if patches.is_bundled(cfg, domain, name):
            return self.json({"ok": False, "error": "bundled with the image: cannot be deleted (a user patch of the same name overrides it)"})
        path = os.path.join(patches.patch_dir(cfg, domain), name)
        if not os.path.isfile(path):
            return self.json({"ok": False, "error": "no such patch"})
        if action == "delete":
            if patches.is_bundled(cfg, domain, name):
                return self.json({"ok": False, "error": "bundled with the image: cannot be deleted (a user patch of the same name overrides it)"})
            os.remove(path)
            if domain == self.installer.running:
                rows = await self.hass.async_add_executor_job(self.installer._patch_rows, domain)
                self.installer._notify_patches(domain, rows)
            else:
                self.installer.dismiss_patch_notification(domain)
            return self.json({"ok": True, "note": "the file is gone; code already patched stays patched until the version is redeployed"})
        return self.json_message("unknown action", status_code=400)


class SettingsView(ManagerView):
    """GET/POST /api/settings: backup retention and the GitHub token
    (write-only; ``github_token: ""`` clears it, absent = unchanged).  A new
    token is checked against api.github.com before it is stored."""

    url = "/api/settings"

    def __init__(self, installer: Installer) -> None:
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        return self.json(self.installer.settings.public())

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        st = self.installer.settings
        note = ""
        new: dict = {}
        if "backup_keep" in body:
            try:
                keep = int(body["backup_keep"])
            except (TypeError, ValueError):
                return self.json({"ok": False, "error": "backup_keep must be an integer (0 = keep all)"})
            if keep < 0:
                return self.json({"ok": False, "error": "backup_keep must be >= 0"})
            new["backup_keep"] = keep
        for key, lo, hi in (("smoke_test_s", 0, 86400), ("backup_daily_hour", 0, 23), ("health_stale_s", 60, 86400), ("health_unavailable_pct", 1, 100)):
            if key in body:
                try:
                    new[key] = min(hi, max(lo, int(body[key])))
                except (TypeError, ValueError):
                    return self.json({"ok": False, "error": f"{key} must be an integer"})
        for key in ("auto_rollback", "release_check", "backup_daily"):
            if key in body:
                if not isinstance(body[key], bool):
                    return self.json({"ok": False, "error": f"{key} must be true/false"})
                new[key] = body[key]
        if "health" in body:
            h = body["health"]
            if not isinstance(h, dict) or len(h) > 50:
                return self.json({"ok": False, "error": "health must be an object keyed by domain"})
            clean: dict[str, dict[str, Any]] = {}
            for dom, rules in h.items():
                if not _DOMAIN_RE.match(str(dom)) or not isinstance(rules, dict):
                    return self.json({"ok": False, "error": f"health: bad entry for {dom!r}"})
                r: dict[str, Any] = {}
                try:
                    if rules.get("stale_s") not in (None, ""):
                        r["stale_s"] = min(86400, max(60, int(rules["stale_s"])))
                    if rules.get("unavailable_pct") not in (None, ""):
                        r["unavailable_pct"] = min(100, max(1, int(rules["unavailable_pct"])))
                except (TypeError, ValueError):
                    return self.json({"ok": False, "error": f"health.{dom}: stale_s / unavailable_pct must be integers"})
                if rules.get("mode"):
                    if rules["mode"] not in HEALTH_MODES:
                        return self.json({"ok": False, "error": f"health.{dom}.mode must be one of {', '.join(HEALTH_MODES)}"})
                    r["mode"] = rules["mode"]
                if r:
                    clean[str(dom)] = r
            new["health"] = clean
        if "log_format" in body:
            fmt, err = clean_log_format(body["log_format"])
            if err:
                return self.json({"ok": False, "error": f"log_format: {err}"})
            new["log_format"] = fmt
        if "dev_source_dir" in body:
            d = str(body["dev_source_dir"] or "").strip()
            if d and (not d.startswith("/") or ".." in d or len(d) > 200):
                return self.json({"ok": False, "error": "dev_source_dir must be an absolute path inside the container"})
            new["dev_source_dir"] = d or DEFAULTS["dev_source_dir"]
        if "allowed_hosts" in body:
            ah = str(body["allowed_hosts"] or "")
            if len(ah) > 500 or not re.fullmatch(r"[A-Za-z0-9.,:\-\s]*", ah):
                return self.json({"ok": False, "error": "allowed_hosts: comma-separated host names"})
            new["allowed_hosts"] = ah
        if "parent_ha_url" in body:
            url = str(body["parent_ha_url"] or "").strip().rstrip("/")
            if url and not re.match(r"^https?://[^\s/]+(:\d+)?$", url):
                return self.json({"ok": False, "error": "parent_ha_url must look like http://host:8123 (no path)"})
            new["parent_ha_url"] = url
            if url != str(self.installer.settings.data.get("parent_ha_url") or "") and "parent_ha_token" not in body:
                new["parent_ha_token"] = ""  # a stored token is only ever sent to the address it was saved for
        if "parent_ha_token" in body:
            tok = body["parent_ha_token"]
            if not isinstance(tok, str) or len(tok) > 400 or any(ch.isspace() for ch in tok.strip()):
                return self.json({"ok": False, "error": "bad token"})
            new["parent_ha_token"] = tok.strip()
        if "github_token" in body:
            token = body["github_token"]
            if not isinstance(token, str) or any(ch.isspace() for ch in token.strip()) or len(token) > 300:
                return self.json({"ok": False, "error": "bad token"})
            token = token.strip()
            if token:
                from homeassistant.helpers.aiohttp_client import async_get_clientsession

                session = async_get_clientsession(self.installer.hass)
                try:
                    async with session.get("https://api.github.com/user", timeout=20,
                                           headers={**st.github_headers(), "Authorization": f"Bearer {token}"}) as resp:
                        if resp.status == 401:
                            return self.json({"ok": False, "error": "GitHub rejected the token (401)"})
                        info = await resp.json() if resp.status == 200 else {}
                        note = (f"token accepted, GitHub user {info.get('login', '?')}" if resp.status == 200
                                else f"token stored (GitHub answered {resp.status} on /user, which fine-grained tokens without user scope do)")
                except Exception as err:  # noqa: BLE001
                    return self.json({"ok": False, "error": f"could not reach GitHub: {type(err).__name__}: {err}"})
            new["github_token"] = token
        st.data.update(new)
        await self.installer.hass.async_add_executor_job(st.save)
        self.installer._releases_cache.clear()
        if getattr(self.installer, "scheduler", None) is not None:
            self.installer.scheduler.rearm()
        return self.json({"ok": True, "note": note, **st.public()})


class EventsView(ManagerView):
    """GET /api/events?limit=100&kind=a,b: the instance's timeline, newest
    ``limit`` events in chronological order."""

    url = "/api/events"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        try:
            limit = min(500, max(1, int(request.query.get("limit", 100) or 100)))
        except ValueError:
            return self.json_message("limit must be an integer", status_code=400)
        kinds = tuple(k for k in request.query.get("kind", "").split(",") if k in events.KINDS)
        store = events.EVENTS
        rows = await self.hass.async_add_executor_job(store.recent, limit, kinds) if store else []
        return self.json({"events": rows, "kinds": list(events.KINDS), "path": store.path if store else None})


class ReleaseCheckView(ManagerView):
    """POST /api/updates/check: refresh the update badges now."""

    url = "/api/updates/check"

    def __init__(self, installer: Installer) -> None:
        self.installer = installer

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        found = await self.installer.check_updates(force=True)
        return self.json({"ok": True, "updates": found, "checked_at": self.installer.updates_checked_at})
