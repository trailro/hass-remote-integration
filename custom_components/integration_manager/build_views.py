"""Preflight, dev mode and the environment builder.

* ``POST /api/releases/preflight`` (JSON body ``domain``, ``tag``, optional
  ``ha``): the full report of :mod:`preflight` (nothing installed, nothing
  changed).
* ``GET /api/dev`` / ``POST /api/dev/install``: what the bind-mounted dev
  source directory offers and installing one of its integrations as the
  ``local`` version (dev mode; ``HRI_DEBUGPY`` state included).
* ``/install``, ``GET /api/build/options``, ``POST /api/build/check``,
  ``POST /api/build/prepare``: choose integration (registry or a new
  owner/repo), a release/tag/branch/commit and a Home Assistant version;
  check the combination (preflight + HA validation), then prepare it:
  install the ref into the store, pin the HA version, optionally start.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from aiohttp import web
from homeassistant.const import __version__ as HA_VERSION
from jsonio import ha_vkey
from homeassistant.core import HomeAssistant

from . import events, preflight
from .diagnostics import scrub_text
from .ha_updater import HaUpdater
from .http_util import ManagerView, with_body
from .installer import _DOMAIN_RE, _REPO_RE, METADATA_MAX_BYTES, Installer, manager_domain_error, read_capped
from .installer import tag_ok as _ok_ref
from .ui import load_template, render

_HA_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d+(b\d+)?\Z")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}\Z")

INSTALL_HTML = load_template("install")


CHECK_TTL_S = 3600


def _check_token(domain: str, ref: str, ha: str, commit: str = "") -> str:
    return hashlib.sha1(f"{domain}|{ref}|{ha}|{commit}".encode()).hexdigest()[:16]


async def _commit_of(hass: HomeAssistant, installer: Installer, domain: str, ref: str, repo: str = "") -> str:
    """The commit a tag, branch or SHA points at now ("" when GitHub cannot say): a branch that moves
    between Check and Prepare must not install code the check never saw.  ``repo`` names the repository
    when it is not (yet) in the registry: Check must not have to register one to look it up."""
    import aiohttp
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    repo = repo or (installer.spec(domain) or {}).get("repo") or ""
    if not repo:
        return ""
    try:
        async with async_get_clientsession(hass).get(f"https://api.github.com/repos/{repo}/commits/{ref}",
                                                     headers=installer.settings.github_headers(),
                                                     timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return ""
            data = json.loads(await read_capped(resp, f"{repo}@{ref} commit", METADATA_MAX_BYTES))
            return str((data.get("sha") if isinstance(data, dict) else "") or "")
    except Exception:  # noqa: BLE001
        return ""


class PreflightView(ManagerView):
    url = "/api/releases/preflight"

    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer
        self._lock = preflight.LOCK  # one pip resolution at a time

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        # a POST with a JSON body: a GET could be triggered by any web page (it downloads a ref and runs pip on it)
        domain, ref, ha = str(body.get("domain") or ""), str(body.get("tag") or ""), str(body.get("ha") or "").strip()
        if not _DOMAIN_RE.match(domain) or not _ok_ref(ref):
            return self.json({"ok": False, "error": "domain and tag required"})
        if ha and not _HA_RE.match(ha):
            return self.json({"ok": False, "error": "bad ha version"})
        async with self._lock:
            try:
                report = await preflight.run(self.hass, self.installer, domain, ref, ha or None)
                return self.json({"ok": True, "report": report})
            except Exception as err:  # noqa: BLE001 - aiohttp text: a redirected download's URL carries a token
                return self.json({"ok": False, "error": scrub_text(f"{type(err).__name__}: {err}")})


class DevView(ManagerView):
    url = "/api/dev"

    def __init__(self, hass: HomeAssistant, installer: Installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        info = await self.hass.async_add_executor_job(self.installer.dev_candidates)
        info["debugpy"] = self.hass.data.get("hri_debugpy") or {"enabled": False}
        info["local_tag"] = self.installer.LOCAL_TAG
        return self.json({"ok": True, **info})


class DevInstallView(ManagerView):
    url = "/api/dev/install"

    def __init__(self, hass: HomeAssistant, installer: Installer, publisher: Any = None) -> None:
        self.hass = hass
        self.installer = installer
        self.publisher = publisher

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        domain = str(body.get("domain", "")).strip().lower()
        path = body.get("path")
        if not _DOMAIN_RE.match(domain) or (path is not None and not isinstance(path, str)):
            return self.json({"ok": False, "error": "domain required"})
        res = await self.installer.install_local(domain, path, replace=bool(body.get("replace")))
        if res.get("ok") and res.get("replaced") and self.publisher is not None:
            await self.publisher.async_reconnect()  # the MQTT identity follows the new integration
        if res.get("ok") and body.get("restart") and res.get("redeployed"):
            await self.installer.restart()
            res["restarting"] = True
        return self.json(res)


class BuildPageView(ManagerView):
    url = "/install"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(INSTALL_HTML, "/install"), content_type="text/html")


class BuildOptionsView(ManagerView):
    url = "/api/build/options"

    def __init__(self, installer: Installer, updater: HaUpdater) -> None:
        self.installer = installer
        self.updater = updater

    async def get(self, request: web.Request) -> web.Response:
        ha = await self.updater.status()
        return self.json({
            "registry": {d: {"name": s.get("name"), "repo": s.get("repo"), "local": bool(s.get("local"))} for d, s in self.installer.registry().items()},
            "installed": {d: sorted((rec.get("versions") or {})) for d, rec in self.installer.state.installed.items()},
            "running": {"domain": self.installer.running, "tag": self.installer.running_tag},
            "ha": {k: ha.get(k) for k in ("current", "latest_stable", "recent", "installed_venvs", "desired", "pending", "python", "requires_python", "error")},
        })


class BuildCheckView(ManagerView):
    url = "/api/build/check"

    def __init__(self, hass: HomeAssistant, installer: Installer, updater: HaUpdater, preflight_view: PreflightView) -> None:
        self.hass = hass
        self.installer = installer
        self.updater = updater
        self._pf = preflight_view
        self._checks: dict[str, float] = {}  # check_id -> when it passed (Prepare needs one for its exact combination)

    def checked(self, domain: str, ref: str, ha: str, check_id: str, commit: str = "") -> bool:
        token = _check_token(domain, ref, ha, commit)
        at = self._checks.get(token)
        return check_id == token and at is not None and time.monotonic() - at < CHECK_TTL_S

    async def _resolve(self, body: dict[str, Any], register: bool = False) -> tuple[str, str, str, str]:
        """(domain, ref, ha, repo).  Only Prepare (``register=True``) writes the repository into the
        registry: a Check is a question, and it left the repository registered (and rewrote a damaged
        registry.json) although nothing was installed.  The repo it returns is passed to the lookups
        that would otherwise need the entry."""
        domain = str(body.get("domain", "")).strip().lower()
        repo = str(body.get("repo", "") or "").strip().strip("/")
        ref = str(body.get("ref", "")).strip()
        ha = str(body.get("ha", "") or "").strip()
        if not _DOMAIN_RE.match(domain) or not _ok_ref(ref):
            raise ValueError("domain and ref (release tag, branch or commit) are required")
        if (why := manager_domain_error(domain)):
            raise ValueError(why)
        if ha and not _HA_RE.match(ha):
            raise ValueError("bad Home Assistant version")
        spec = self.installer.spec(domain)
        if repo:
            if not _REPO_RE.match(repo) or ".." in repo:
                raise ValueError("repo must be owner/name")
            if spec and spec.get("repo") and spec["repo"] != repo:
                raise ValueError(f"{domain} is registered with {spec['repo']}; use another domain name for a different repository")
            if register and (not spec or not spec.get("repo")):
                # reads and writes the registry files: in the executor (a ValueError comes through unchanged)
                await self.hass.async_add_executor_job(self.installer.add_to_registry, domain, repo, str(body.get("name") or "")[:80] or None)
        elif not spec or not spec.get("repo"):
            raise ValueError(f"{domain} is not in the registry: give its GitHub owner/repo")
        return domain, ref, ha, repo or str(spec.get("repo") or "")

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        try:
            domain, ref, ha, repo = await self._resolve(body)  # a check registers nothing: Prepare does
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        ha_check: dict[str, Any] = {"version": ha or None, "ok": True, "error": ""}
        if ha:
            try:
                await self.updater.validate(ha)
            except ValueError as err:
                ha_check = {"version": ha, "ok": False, "error": str(err)}
            if ha_check["ok"] and ha != HA_VERSION and ha_vkey(ha) < ha_vkey(HA_VERSION):
                ha_check = {"version": ha, "ok": False,
                            "error": f"older than the running {HA_VERSION}: switch Home Assistant down on the System page first (it asks what the older version starts with)"}
        # the commit first, the download by that commit: a branch that moves during the check must not
        # leave a check id naming a commit whose code was never looked at
        commit = await _commit_of(self.hass, self.installer, domain, ref, repo)
        async with self._pf._lock:
            try:
                report = await preflight.run(self.hass, self.installer, domain, ref, ha or None, archive_ref=commit or None, repo=repo)
            except Exception as err:  # noqa: BLE001
                return self.json({"ok": False, "error": scrub_text(f"{type(err).__name__}: {err}"), "ha_check": ha_check})
        if not ha_check["ok"]:
            report["blockers"].append(f"Home Assistant {ha}: {ha_check['error']}")
            report["ok"] = False
        cur = self.installer.installed_domain
        if cur and cur != domain:
            report["replaces"] = cur
            report["warnings"].append(f"this container holds {cur}: preparing {domain} replaces it (config entries, patches, YAML, MQTT identity), after a backup")
        check_id = _check_token(domain, ref, ha, commit)
        if report["ok"]:
            now = time.monotonic()
            for key in [k for k, at in self._checks.items() if now - at >= CHECK_TTL_S]:
                del self._checks[key]  # a pass Prepare can no longer use
            self._checks[check_id] = now
        else:
            self._checks.pop(check_id, None)  # a combination that fails now must not be prepared on an older pass
        return self.json({"ok": True, "report": report, "ha_check": ha_check, "check_id": check_id if report["ok"] else None})


class BuildPrepareView(ManagerView):
    url = "/api/build/prepare"

    def __init__(self, hass: HomeAssistant, installer: Installer, updater: HaUpdater, check_view: BuildCheckView, publisher: Any = None) -> None:
        self.hass = hass
        self.installer = installer
        self.updater = updater
        self._check = check_view
        self.publisher = publisher

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        wanted_ha = str(body.get("ha") or "").strip()
        if wanted_ha and wanted_ha != HA_VERSION and ha_vkey(wanted_ha) < ha_vkey(HA_VERSION):
            # a downgrade must say what the older version starts with (restore, clean start, keep)
            return self.json({"ok": False, "error": f"Home Assistant {wanted_ha} is older than the running {HA_VERSION}: switch Home Assistant "
                                                    "down on the System page first (it asks what the older version starts with), then prepare here"})
        try:
            domain, ref, ha, repo = await self._check._resolve(body, register=True)  # installing it needs the entry
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        commit = await _commit_of(self.hass, self.installer, domain, ref, repo)
        if not commit and not _SHA_RE.match(ref):
            # the download must be the commit Check verified, and a tag or branch can move in between
            return self.json({"ok": False, "error": f"GitHub did not say which commit {ref} points at now, so Prepare cannot install exactly what Check verified: try again in a moment"})
        if not self._check.checked(domain, ref, ha, str(body.get("check_id") or ""), commit):
            return self.json({"ok": False, "error": "run Check for exactly this integration, version and Home Assistant version first (the report on screen belongs to another combination or commit (a branch that moved), is older than an hour, or was made before the manager restarted: passed checks are kept in memory)"})
        steps: list[dict[str, Any]] = []
        ha_state = await self.updater.status()
        ha_changes = bool(ha) and ha != ha_state.get("current")
        if ha_changes:
            try:
                await self.updater.validate(ha)  # before anything is installed, let alone replaced
            except ValueError as err:
                return self.json({"ok": False, "error": f"Home Assistant {ha}: {err}", "steps": steps})
        from .views import _HA_CHANGE_LOCK, _ha_change_lock_taken

        if ha_changes:
            # What refuses the change below is refused before the install, which may replace (remove) the
            # integration there is: that lock is then held from here to the change, and the install's busy is
            # released with no await before the change checks it.  A pending restore refuses the install itself.
            if _ha_change_lock_taken() or self.installer.busy:
                return self.json({"ok": False, "error": "a Home Assistant version change or another action is running (an install, start, stop, import, restore or full rollback): try again in a moment", "steps": steps})
            await _HA_CHANGE_LOCK.acquire()  # free: checked above, with no await since
        try:
            res = await self.installer.install(ref, domain=domain, replace=bool(body.get("replace")), archive_ref=commit or ref)
        finally:
            if ha_changes:
                _HA_CHANGE_LOCK.release()  # async_change_ha_version takes it again before anything else can run
        steps.append({"step": "install", **res})
        if not res.get("ok"):
            return self.json({"ok": False, "error": f"install: {res.get('error')}", "steps": steps})
        # a reinstall of the running copy refreshed the files under the process: its old code keeps running until a restart
        restart_required = bool(res.get("redeployed"))
        ha_error = None
        if ha_changes:
            try:
                from .views import async_change_ha_version  # the same backup and change record as the System page

                st = await async_change_ha_version(self.installer, self.updater, ha, "keep", "environment builder")
                steps.append({"step": "ha", "ok": True, "desired": st["desired"], "backup": st["backup"]})
                restart_required = True
            except (ValueError, OSError) as err:
                steps.append({"step": "ha", "ok": False, "error": str(err)})
                ha_error = f"Home Assistant {ha}: {err}"
        if res.get("replaced") and self.publisher is not None:
            await self.publisher.async_reconnect()  # the MQTT identity follows the new integration
        if ha_error:
            return self.json({"ok": False, "error": ha_error, "steps": steps})
        if ha and not ha_changes and ha_state.get("pending"):
            # the running version was chosen explicitly: an older intention to
            # move to another version at the next restart contradicts it
            if _ha_change_lock_taken() or self.installer.busy:
                return self.json({"ok": False, "error": "a Home Assistant version change or another action is running (an install, start, stop, import, restore or full rollback): try again in a moment", "steps": steps})
            try:
                async with _HA_CHANGE_LOCK:  # held across both writes: a change scheduled in between would be overwritten
                    dropped = await self.installer.hass.async_add_executor_job(self.updater.cancel_config_change)
                    await self.installer.hass.async_add_executor_job(self.updater.set_desired, ha)
            except ValueError as err:  # an unreadable ha.json
                return self.json({"ok": False, "error": f"Home Assistant {ha}: {err}", "steps": steps})
            steps.append({"step": "ha", "ok": True, "desired": ha, "note": f"cancelled the scheduled move to {ha_state.get('desired')}"
                          + (f" (dropped: {', '.join(dropped)})" if dropped else "")})
            events.emit("ha", f"scheduled Home Assistant {ha_state.get('desired')} cancelled: {ha} chosen in the environment builder", version=ha)
        deferred = False
        if body.get("start") and ha_changes:
            # the requirements and the setup must happen in the target venv, which
            # only exists after the restart: the start is recorded for that boot
            self.installer.state.pending_start = {"domain": domain, "tag": ref, "ha": ha}
            self.installer._save_state()
            steps.append({"step": "start", "ok": True, "deferred": True, "note": f"starts after the restart, on Home Assistant {ha}"})
            deferred = True
        elif body.get("start"):
            res = await self.installer.start(domain, ref)
            steps.append({"step": "start", **res})
            if not res.get("ok"):
                return self.json({"ok": False, "error": f"start: {res.get('error')}", "steps": steps, "restart_required": restart_required})
            if self.publisher is not None:
                await self.publisher.async_after_start(res)  # as a start from the Overview: MQTT follows, stale documents go
            restart_required = restart_required or bool(res.get("restart_required"))
        events.emit("build", f"{domain} {ref}" + (f" on Home Assistant {ha}" if ha else "")
                    + (" starts after the restart" if deferred else " started" if body.get("start") else " prepared")
                    + ("; restart required" if restart_required else ""), domain=domain, ref=ref, ha=ha or None)
        return self.json({"ok": True, "steps": steps, "restart_required": restart_required, "deferred_start": deferred, "domain": domain, "ref": ref})
