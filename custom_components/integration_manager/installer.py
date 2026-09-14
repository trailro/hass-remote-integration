"""Install / start / stop / update integrations without HACS.

Model
-----
* **Registry**: ``/app/registry.json`` (image) + ``<config>/integration_manager/registry.json``
  (user) map domains to GitHub repos and per-integration hooks.
* **Installed** = downloaded into the version store
  ``<config>/integration_manager/versions/<domain>/<tag>/``.  Any number of
  integrations and any number of versions per integration may be installed
  side by side (they only cost disk).
* **Running** = exactly one ``(domain, tag)``: its files are copied into
  ``custom_components/<domain>`` (HA insists that directory name == domain,
  so only one version of a domain can be there), its requirements are
  installed into the venv, its patches applied, its config entries enabled.
  Starting another integration stops the running one (entries disabled).
  Everything published derives its identity from the running domain
  (``hass_<domain>``); with nothing running there is no MQTT identity.
* Config entries belong to the domain, so switching versions keeps the
  settings; a downgrade after a config migration needs the pre-update
  backup (taken automatically before starting a different version).
"""

from __future__ import annotations

import importlib
import io
import json
import weakref
import functools
import logging
import os
import re
import shutil
import site
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any

import homeassistant
from homeassistant import loader
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import package as pkg_util

import jsonio
from jsonio import vkey, write_json

from . import events, patches
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com/repos/{repo}"
RAW_GITHUB = "https://raw.githubusercontent.com/{repo}/{tag}/custom_components/{domain}/manifest.json"
BUILTIN_REGISTRY = "/app/registry.json"
RELEASE_CACHE_S = 300


def instance_key(domain: str | None) -> str | None:
    """Identity everything published derives from; None when nothing runs."""
    return f"hass_{domain}" if domain else None


@dataclass
class Domain:
    versions: dict[str, dict[str, Any]] = field(default_factory=dict)  # tag -> {installed_at, version, requirements, pin}
    running_tag: str | None = None      # tag whose files sit in custom_components/<domain>
    previous_tag: str | None = None     # what ran before the last version switch
    pre_update_backup: str | None = None


@dataclass
class State:
    domain: str | None = None           # the RUNNING integration, if any
    installed: dict[str, dict[str, Any]] = field(default_factory=dict)  # domain -> Domain
    restart_required: bool = False
    last_action: str = ""
    last_error: str = ""
    pending_smoke: dict[str, Any] | None = None  # a start that needed a restart: smoke test after that restart
    pending_start: dict[str, Any] | None = None  # {domain, tag, ha, blocked?}: start it at the boot on HA <ha> (builder chose another HA version)
    last_smoke: dict[str, Any] | None = None     # last smoke verdict (survives restarts and rollbacks)
    last_release_check: int = 0                  # epoch of the last weekly GitHub release check
    rollback_backup: str | None = None           # the backup a full rollback restores: protected until that restore succeeded
    release_updates: dict[str, str] = field(default_factory=dict)  # last release check: domain -> newest stable tag not in the store


def _gh_check(resp, what: str) -> None:
    if resp.status in (401, 403, 404):
        raise RuntimeError(f"GitHub {resp.status} for {what}: private repo or bad token? (set a token in the Integrations card)")
    resp.raise_for_status()


def _req_name(req: str) -> str:
    try:
        from packaging.requirements import Requirement

        return Requirement(req).name
    except Exception:  # noqa: BLE001
        return re.split(r"[\s<>=!~;@\[]", req, 1)[0].strip()


def _mtime(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return -1


_DELAYED_STORES: "weakref.WeakSet[Any]" = weakref.WeakSet()


def track_delayed_stores() -> None:
    """Remember every Store that schedules a delayed save (integrations use
    async_delay_save for caches, paired devices and the like), so a backup can
    write what they still hold: Home Assistant itself only does that at its
    final write.  Weak references: a store that goes away is forgotten."""
    from homeassistant.helpers.storage import Store

    if getattr(Store.async_delay_save, "_hri_tracked", False):
        return
    original = Store.async_delay_save

    @functools.wraps(original)
    def async_delay_save(self, *args: Any, **kwargs: Any) -> None:
        _DELAYED_STORES.add(self)
        return original(self, *args, **kwargs)

    async_delay_save._hri_tracked = True  # type: ignore[attr-defined]
    Store.async_delay_save = async_delay_save


class Installer:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.config_dir = hass.config.config_dir
        self.state_dir = os.path.join(self.config_dir, "integration_manager")
        self.state_file = os.path.join(self.state_dir, "state.json")
        self.versions_dir = os.path.join(self.state_dir, "versions")
        self.user_registry_file = os.path.join(self.state_dir, "registry.json")
        self.constraints = os.path.join(os.path.dirname(homeassistant.__file__), "package_constraints.txt")
        self._releases_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._releases_checked: dict[str, str] = {}
        self._patch_cache: dict[str, str | None] = {}
        self._req_versions_cache: dict[str, str | None] = {}
        self.settings = Settings(self.state_dir)
        self.health_source = None  # set by __init__: publisher.build_health(grace=...)
        self.on_domain_removed = None  # set by __init__: async (base_topic) -> clears the old MQTT identity
        self._smoke_pending: dict[str, Any] | None = None
        self._smoke_handle = None
        self.updates: dict[str, str] = {}  # domain -> newest stable tag not yet in the store
        self.updates_checked_at: str | None = None
        self._loaded_tags: dict[str, str] = {}  # domain -> tag whose code this process imported
        self.busy = False
        os.makedirs(self.versions_dir, exist_ok=True)
        self.state = self._load_state()
        self.updates = dict(self.state.release_updates or {})  # the badge and the update entity survive a restart
        self._migrate_version_dirs()

    # ----- registry --------------------------------------------------------

    def _builtin_registry(self) -> dict[str, dict[str, Any]]:
        try:
            with open(BUILTIN_REGISTRY, encoding="utf-8") as fh:
                return json.load(fh).get("integrations") or {}
        except (OSError, ValueError):
            return {}

    def registry(self) -> dict[str, dict[str, Any]]:
        """Built-in + user registries, cached by both files' mtimes (read
        several times per status poll otherwise)."""
        key = tuple(_mtime(p) for p in (BUILTIN_REGISTRY, self.user_registry_file))
        cache = getattr(self, "_registry_cache", None)
        if cache and cache[0] == key:
            return cache[1]
        out = self._registry_uncached()
        self._registry_cache = (key, out)
        return out

    def _registry_uncached(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for path in (BUILTIN_REGISTRY, self.user_registry_file):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for domain, spec in (data.get("integrations") or {}).items():
                    if isinstance(spec, dict) and (spec.get("repo") or spec.get("local")):
                        out[domain] = {**out.get(domain, {}), **spec}
            except (OSError, ValueError):
                continue
        return out

    def add_to_registry(self, domain: str, repo: str, name: str | None = None, local: bool = False) -> dict[str, Any]:
        """``local=True`` registers a dev-mode integration installed from a
        directory: no repo, so no releases/updates, everything else works."""
        domain = domain.strip().lower()
        repo = repo.strip().strip("/")
        if not domain.replace("_", "").isalnum() or (repo.count("/") != 1 and not (local and not repo)):
            raise ValueError("domain must be a HA domain (a_b), repo must be owner/name")
        builtin = self._builtin_registry().get(domain)
        if builtin and builtin.get("repo") != repo:
            raise ValueError(f"{domain} is a built-in registry entry pinned to {builtin['repo']}; use another domain name")
        data = jsonio.read_json(self.user_registry_file, {"integrations": {}})
        if not isinstance(data, dict):
            data = {"integrations": {}}
        entry = {"name": name or domain, "repo": repo}
        if local:
            entry["local"] = True
        data.setdefault("integrations", {})[domain] = entry
        os.makedirs(self.state_dir, exist_ok=True)
        write_json(self.user_registry_file, data, fsync=False)  # called from request handlers on the loop
        return self.registry()[domain]

    def spec(self, domain: str | None) -> dict[str, Any]:
        return (self.registry().get(domain) or {}) if domain else {}

    # ----- state -----------------------------------------------------------

    def _load_state(self) -> State:
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError:
            return State()
        except ValueError:
            kept = f"{self.state_file}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                shutil.copyfile(self.state_file, kept)
            except OSError:
                kept = "(could not be copied)"
            _LOGGER.error("state.json is not valid JSON; starting with an empty state, the damaged file is kept as %s", kept)
            return State()
        if not isinstance(data, dict) or "installed" not in data:
            _LOGGER.error("state.json has an unknown layout; starting with an empty state (file kept)")
            return State()
        return State(**{k: v for k, v in data.items() if k in State.__dataclass_fields__})

    def _save_state(self) -> None:
        # atomic (tmp + replace) but no fsync on the event loop: a torn file is
        # impossible, only a power cut between replace and flush loses the last write
        write_json(self.state_file, asdict(self.state), fsync=False)

    def _dom(self, domain: str) -> dict[str, Any]:
        return self.state.installed.setdefault(domain, asdict(Domain()))

    # ----- paths -----------------------------------------------------------

    def _component_dir(self, domain: str) -> str:
        return os.path.join(self.config_dir, "custom_components", domain)

    def component_dir(self, domain: str) -> str:
        return self._component_dir(domain)

    def _migrate_version_dirs(self) -> None:
        """Refs with '/' used to live in '<tag with / as _>'; they now live in
        the reversible encoding.  Rename what is unambiguous, report the rest."""
        for domain, rec in self.state.installed.items():
            for tag in list(rec.get("versions") or {}):
                if "/" not in tag:
                    continue
                old = os.path.join(self.versions_dir, domain, tag.replace("/", "_"))
                new = self._version_dir(domain, tag)
                if os.path.isdir(new) or not os.path.isdir(old):
                    continue
                if tag.replace("/", "_") in rec["versions"]:
                    _LOGGER.warning("version store: %s %s and %s share the old directory %s; not migrated, reinstall one of them",
                                    domain, tag, tag.replace("/", "_"), old)
                    continue
                try:
                    os.rename(old, new)
                    _LOGGER.info("version store: migrated %s %s to %s", domain, tag, os.path.basename(new))
                except OSError as err:
                    _LOGGER.warning("version store: could not migrate %s %s: %s", domain, tag, err)

    def _version_dir(self, domain: str, tag: str) -> str:
        # reversible: feature/x and feature_x are different git refs and must not share a directory
        return os.path.join(self.versions_dir, domain, tag.replace("%", "%25").replace("/", "%2F"))

    @staticmethod
    def _manifest_at(path: str) -> dict[str, Any] | None:
        try:
            with open(os.path.join(path, "manifest.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    @property
    def installed_domain(self) -> str | None:
        """The one integration this container holds (several versions of it)."""
        return next(iter(self.state.installed), None)

    @property
    def running(self) -> str | None:
        return self.state.domain

    @property
    def running_tag(self) -> str | None:
        return self.state.installed.get(self.state.domain or "", {}).get("running_tag") if self.state.domain else None

    @property
    def instance_key(self) -> str | None:
        return instance_key(self.state.domain)

    def site_packages_for(self, domain: str | None) -> str:
        spec = self.spec(domain)
        req_mod = spec.get("patch_module")  # registry: the pip module whose site-packages the patches target
        if req_mod:
            found = importlib.util.find_spec(req_mod)
            if found and found.submodule_search_locations:
                return os.path.dirname(list(found.submodule_search_locations)[0])
        for p in sys.path:
            if p.endswith("site-packages") and p.startswith(sys.prefix):
                return p
        return site.getsitepackages()[0]

    @property
    def site_packages(self) -> str:
        return self.site_packages_for(self.state.domain)

    # ----- read-only info --------------------------------------------------

    def installed_manifest(self, domain: str | None = None) -> dict[str, Any] | None:
        """Cached by file mtime: status()/health() ask for it every few seconds."""
        domain = domain or self.state.domain
        if not domain:
            return None
        path = os.path.join(self._component_dir(domain), "manifest.json")
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return None
        cache = getattr(self, "_manifest_cache", None)
        if cache is None:
            cache = self._manifest_cache = {}
        hit = cache.get(domain)
        if hit and hit[0] == mtime:
            return hit[1]
        data = self._manifest_at(self._component_dir(domain))
        cache[domain] = (mtime, data)
        return data

    @staticmethod
    def _dist_version(name: str) -> str | None:
        import importlib.metadata as md

        try:
            return md.version(name)
        except md.PackageNotFoundError:
            return None

    def _requirement_versions(self, requirements: list[str]) -> dict[str, str | None]:
        """importlib.metadata per requirement is not free: cached until pip runs."""
        cache = self._req_versions_cache
        if all(r in cache for r in requirements):
            return {r: cache[r] for r in requirements}
        out = {}
        for req in requirements:
            try:
                from packaging.requirements import Requirement

                name = Requirement(req).name
            except Exception:  # noqa: BLE001 - odd specifier: best effort
                name = re.split(r"[\s<>=!~;@\[]", req, 1)[0].strip()
            out[req] = self._dist_version(name)
        cache.update(out)
        return out

    def protected_backups(self) -> set[str]:
        """Backups a full rollback still needs: never pruned automatically."""
        out = {rec["pre_update_backup"] for rec in self.state.installed.values() if rec.get("pre_update_backup")}
        if self.state.rollback_backup:
            out.add(self.state.rollback_backup)
        # a scheduled Home Assistant version change: its pre-change backup is the
        # way back after a failed boot, and a clean start rebuilds from it
        ha_state = jsonio.read_json(os.path.join(self.state_dir, "ha.json"), {}) or {}
        change = ha_state.get("change") if isinstance(ha_state, dict) else None
        if isinstance(change, dict) and change.get("backup"):
            out.add(str(change["backup"]))
        plan = jsonio.read_json(os.path.join(self.state_dir, "rebuild-pending.json"), {}) or {}
        if isinstance(plan, dict) and plan.get("backup"):
            out.add(str(plan["backup"]))
        return out

    def _patch_summary(self, rows: list[dict[str, Any]] | None) -> str | None:
        """'applied' when every applicable patch is, else the first problem."""
        if not rows:
            return None
        bad = [f"{r['name']}: {r['status']}" for r in rows if r["status"] not in ("applied", "skipped")]
        return "applied" if not bad else bad[0]

    def _patch_status(self, domain: str | None) -> str | None:
        """Last computed summary (status() / reconcile / start refresh it);
        never touches the disk on the loop."""
        return self._patch_cache.get(domain) if domain else None

    def _patch_rows(self, domain: str) -> list[dict[str, Any]]:
        """Blocking: the patch rows of one domain; refreshes the summary cache."""
        rows = patches.status(self.config_dir, domain, self.site_packages_for(domain), self._component_dir(domain),
                              (self.state.installed.get(domain) or {}).get("running_tag"))
        self._patch_cache[domain] = self._patch_summary(rows)
        return rows

    def _entries_of(self, domain: str) -> list[Any]:
        return self.hass.config_entries.async_entries(domain)

    def _domain_info(self, domain: str, patch_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        rec = self.state.installed.get(domain, {})
        running = domain == self.state.domain
        manifest = self.installed_manifest(domain) if running else None
        reqs = manifest.get("requirements", []) if manifest else []
        req_versions = self._requirement_versions(reqs) if running else {}
        entries = self._entries_of(domain)
        versions = {tag: {**v, "dir_present": os.path.isdir(self._version_dir(domain, tag))} for tag, v in (rec.get("versions") or {}).items()}
        return {
            "domain": domain,
            "name": self.spec(domain).get("name") or domain,
            "repo": self.spec(domain).get("repo"),
            "versions": versions,
            "newest_tag": max(versions, key=vkey) if versions else None,
            "running": running,
            "running_tag": rec.get("running_tag"),
            "previous_tag": rec.get("previous_tag"),
            "pre_update_backup": rec.get("pre_update_backup"),
            "loaded_tag": self._loaded_tags.get(domain),
            "code_version": manifest.get("version") if manifest else None,
            "requirements": req_versions,
            "requirements_ok": (bool(reqs) and all(req_versions.values())) if running else None,
            "loaded_as_integration": domain in self.hass.config.components,
            "entries": [{"entry_id": e.entry_id, "title": e.title, "state": e.state.value,
                         "disabled_by": e.disabled_by.value if e.disabled_by else None, "version": e.version} for e in entries],
            "patch": (self._patch_summary(patch_rows) if patch_rows is not None else self._patch_status(domain)) if running else None,
            "patches": patch_rows if patch_rows is not None else [],
            "update_available": self.updates.get(domain),
        }

    def health(self) -> dict[str, Any]:
        """Cheap, synchronous view of the running integration for the MQTT
        health document: identity, version, config-entry states, flags."""
        domain = self.state.domain
        if not domain:
            return {"integration": None, "state": "stopped", "reason": "no integration is running"}
        rec = self.state.installed.get(domain, {})
        manifest = self.installed_manifest(domain) or {}
        entries = [{"title": e.title, "state": e.state.value, "reason": e.reason, "disabled_by": e.disabled_by.value if e.disabled_by else None}
                   for e in self._entries_of(domain)]
        active = [e for e in entries if not e["disabled_by"]]
        loaded = domain in self.hass.config.components
        if not entries:
            # YAML-only integration: no config entry to look at, "loaded" is the verdict
            state, reason = ("ok", "") if loaded else ("error", "not loaded (no config entry, no YAML setup)")
        elif not active:
            state, reason = "error", "no enabled config entry"
        elif all(e["state"] == "loaded" for e in active):
            state, reason = "ok", ""
        else:
            bad = next(e for e in active if e["state"] != "loaded")
            state, reason = "error", f"config entry '{bad['title']}' is {bad['state']}" + (f": {bad['reason']}" if bad["reason"] else "")
        return {
            "integration": domain,
            "tag": rec.get("running_tag"),
            "version": manifest.get("version"),
            "loaded": loaded,
            "entries": entries,
            "restart_required": self.state.restart_required,
            "last_error": self.state.last_error or "",
            "patch": self._patch_status(domain),
            "state": state,
            "reason": reason,
        }

    async def status(self) -> dict[str, Any]:
        domain = self.state.domain
        # user patch modules run their status(ctx) and read files: executor, once
        patch_rows = await self.hass.async_add_executor_job(lambda: {d: self._patch_rows(d) for d in list(self.state.installed)})
        infos = {d: self._domain_info(d, patch_rows.get(d)) for d in self.state.installed}
        info = infos.get(domain) if domain else None
        if info:
            info["dependency_requirements"] = self._requirement_versions(await self.dependency_requirements(domain))
        return {
            "integration": self.installed_domain,
            "ha_version": homeassistant.const.__version__,
            "python": sys.version.split()[0],
            "venv": sys.prefix,
            "instance_key": self.instance_key,
            "running": info,
            "installed": infos,
            "smoke_test": self.smoke,
            "updates": self.updates,
            "updates_checked_at": self.updates_checked_at,
            "registry": self.registry(),
            "site_packages": self.site_packages,
            "state": asdict(self.state),
            "busy": self.busy,
            "versions_store_bytes": await self.hass.async_add_executor_job(self._store_size),
        }

    def _store_size(self) -> int:
        cache = getattr(self, "_store_size_cache", None)
        key = tuple(sorted((d, tuple(sorted(r.get("versions") or {}))) for d, r in self.state.installed.items()))
        if cache and cache[0] == key:
            return cache[1]
        n = self._store_size_uncached()
        self._store_size_cache = (key, n)
        return n

    def _store_size_uncached(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.versions_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    async def releases(self, domain: str | None = None, force: bool = False) -> list[dict[str, Any]]:
        domain = domain or self.state.domain
        spec = self.spec(domain)
        if not spec or not spec.get("repo"):
            return []  # dev-mode (local directory) integrations have no releases
        now = time.monotonic()
        cached = self._releases_cache.get(domain)
        if not force and cached and now - cached[0] < RELEASE_CACHE_S:
            rels = cached[1]
        else:
            session = async_get_clientsession(self.hass)
            async with session.get(GITHUB_API.format(repo=spec["repo"]) + "/releases", params={"per_page": 15},
                                   headers=self.settings.github_headers()) as resp:
                _gh_check(resp, spec["repo"])
                raw = await resp.json()
            self._releases_checked[domain] = time.strftime("%Y-%m-%dT%H:%M:%S")
            rels = [{"tag": r["tag_name"], "prerelease": bool(r["prerelease"]), "published": (r.get("published_at") or "")[:10],
                     "notes": (r.get("body") or "")[:4000], "url": r.get("html_url")} for r in raw]
            self._releases_cache[domain] = (now, rels)
        rec = self.state.installed.get(domain, {})
        checked = self._releases_checked.get(domain)
        return [{**r, "installed": r["tag"] in (rec.get("versions") or {}), "running": r["tag"] == rec.get("running_tag") and domain == self.state.domain,
                 "checked_at": checked} for r in rels]

    async def preview(self, domain: str, tag: str) -> dict[str, Any]:
        spec = self.spec(domain)
        if not spec:
            raise ValueError(f"unknown integration {domain}")
        session = async_get_clientsession(self.hass)
        async with session.get(RAW_GITHUB.format(repo=spec["repo"], tag=tag, domain=domain), headers=self.settings.github_headers()) as resp:
            if resp.status != 200:
                raise ValueError(f"manifest.json not found for {domain} {tag} (HTTP {resp.status})")
            new = json.loads(await resp.text())
        rec = self.state.installed.get(domain, {})
        cur_tag = rec.get("running_tag")
        old = (self._manifest_at(self._version_dir(domain, cur_tag)) if cur_tag else None) or {}
        old_req, new_req = set(old.get("requirements", [])), set(new.get("requirements", []))
        notes = next((r.get("notes") for r in (self._releases_cache.get(domain) or (0, []))[1] if r.get("tag") == tag), None)
        return {"domain": domain, "tag": tag, "compared_to": cur_tag, "notes": notes,
                "installed_version": old.get("version"), "new_version": new.get("version"),
                "requirements_added": sorted(new_req - old_req), "requirements_removed": sorted(old_req - new_req),
                "requirements_unchanged": sorted(new_req & old_req), "dependencies": new.get("dependencies", []),
                "after_dependencies": new.get("after_dependencies", []), "config_flow": new.get("config_flow"),
                "min_ha_version": new.get("homeassistant"), "currently_installed_versions": self._requirement_versions(sorted(new_req))}

    # ----- install (into the store) -------------------------------------------

    def _replace_guard(self, domain: str, replace: bool) -> str | None:
        """One integration per container: installing another domain replaces
        the current one (its versions, files, config entries, patches, YAML
        and MQTT identity go; a backup is taken first) and must be asked
        for explicitly."""
        cur = self.installed_domain
        if cur and cur != domain and not replace:
            return (f"this container holds {cur}: installing {domain} replaces it (config entries, patches, YAML and its "
                    f"MQTT identity are removed after a backup); confirm with replace=true, or run {domain} in a second container")
        return None

    async def _replace_current(self, new_domain: str) -> dict[str, Any]:
        """Blocking-ish: backup, then remove the current integration entirely."""
        import backupkit

        old = self.installed_domain
        if not old or old == new_domain:
            return {}
        pre = await self.async_backup(f"pre-replace-{old}")
        await self.hass.async_add_executor_job(backupkit.prune, self.config_dir, self.settings.backup_keep, self.protected_backups() | {pre["name"]})
        await self._remove_domain(old)
        events.emit("replace", f"{old} replaced by {new_domain}; backup {pre['name']} taken first", old=old, new=new_domain, backup=pre["name"])
        return {"replaced": old, "pre_replace_backup": pre["name"]}

    async def install(self, tag: str, domain: str | None = None, replace: bool = False) -> dict[str, Any]:
        """Download a release into the version store.  Nothing runs yet."""
        import backupkit

        if backupkit.pending(self.config_dir):
            return {"ok": False, "error": "a restore is scheduled for the next restart: restart (or cancel it) first"}
        domain = domain or self.installed_domain
        spec = self.spec(domain)
        if not domain or not spec:
            return {"ok": False, "error": f"unknown integration {domain!r}: add it to the registry first"}
        if not spec.get("repo"):
            return {"ok": False, "error": f"{domain} is a dev-mode integration (no repository): install it from its directory"}
        if (why := self._replace_guard(domain, replace)):
            return {"ok": False, "error": why, "replace_required": True, "current": self.installed_domain}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True
        self.state.last_error = ""
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(GITHUB_API.format(repo=spec["repo"]) + f"/zipball/{tag}", headers=self.settings.github_headers()) as resp:
                _gh_check(resp, f"{spec['repo']}@{tag}")
                blob = await resp.read()
            manifest = await self.hass.async_add_executor_job(self._store_version, blob, domain, tag)
            # only now, with the new release verified and in the store, does the
            # current integration go (a bad tag or a GitHub error leaves it untouched)
            replaced = await self._replace_current(domain)
            pin = next((r for r in manifest.get("requirements", []) if _req_name(r).replace("-", "_") in
                        (spec.get("patch_module") or "",)), None)
            self._dom(domain)["versions"][tag] = {"installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": manifest.get("version"),
                                                 "requirements": manifest.get("requirements", []), "pin": pin}
            self.state.last_action = f"installed {domain} {tag} into the version store"
            self._save_state()
            events.emit("install", f"{domain} {tag} (version {manifest.get('version')}) into the version store", domain=domain, tag=tag)
            self._releases_cache.pop(domain, None)
            if domain in self.updates and vkey(tag) >= vkey(self.updates[domain]):
                self.updates.pop(domain)  # the newer release is in the store now
                self.state.release_updates = dict(self.updates)
                self._save_state()
            was_running = domain == self.state.domain and self._dom(domain).get("running_tag") == tag
            if was_running:  # reinstall of the running version: refresh the files in place
                await self.hass.async_add_executor_job(self._deploy, domain, tag)
                await self.hass.async_add_executor_job(self._ensure_deployed, domain, tag)  # rewrites the .hri-tag marker
                self.state.restart_required = True
                self._save_state()
            return {"ok": True, "domain": domain, "tag": tag, "version": manifest.get("version"), "pin": pin, "redeployed": was_running, **replaced}
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("install %s %s failed", domain, tag)
            self.state.last_error = f"{type(err).__name__}: {err}"
            events.emit("error", f"install {domain} {tag} failed: {self.state.last_error}", domain=domain, tag=tag)
            self._save_state()
            return {"ok": False, "error": self.state.last_error}
        finally:
            self.busy = False

    async def remove_version(self, domain: str, tag: str) -> dict[str, Any]:
        rec = self.state.installed.get(domain)
        if not rec or tag not in rec.get("versions", {}):
            return {"ok": False, "error": "not installed"}
        if domain == self.state.domain and rec.get("running_tag") == tag:
            return {"ok": False, "error": "this version is running; stop it or start another version first"}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True
        try:
            await self.hass.async_add_executor_job(shutil.rmtree, self._version_dir(domain, tag), True)
        finally:
            self.busy = False
        rec["versions"].pop(tag, None)
        ps = self.state.pending_start
        if isinstance(ps, dict) and (ps.get("domain"), ps.get("tag")) == (domain, tag):
            self.state.pending_start = None
            self._save_state()
        events.emit("remove", f"{domain} {tag} removed from the version store", domain=domain, tag=tag)
        if rec.get("previous_tag") == tag:
            rec["previous_tag"] = None
            rec["pre_update_backup"] = None  # a rollback target without its version makes no sense
        if rec.get("running_tag") == tag:  # a stopped domain whose deployed files were this tag
            rec["running_tag"] = None
            rec["pre_update_backup"] = None
        self.state.last_action = f"removed {domain} {tag} from the version store"
        self._save_state()
        return {"ok": True}

    # ----- start / stop -----------------------------------------------------

    async def start(self, domain: str, tag: str | None = None, boot: bool = False) -> dict[str, Any]:
        """Make (domain, tag) the running integration.  ``boot``: called by
        the deferred start during this boot, where run.py applies the YAML
        and sets the domain up right after (no restart for either)."""
        rec = self.state.installed.get(domain)
        if not rec or not rec.get("versions"):
            return {"ok": False, "error": f"{domain} is not installed"}
        tag = tag or rec.get("running_tag") or max(rec["versions"], key=vkey)
        if tag not in rec["versions"] or not os.path.isdir(self._version_dir(domain, tag)):
            return {"ok": False, "error": f"{domain} {tag} is not in the version store"}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        import backupkit

        if backupkit.pending(self.config_dir):
            return {"ok": False, "error": "a restore is scheduled for the next restart: restart (or cancel it in the Backup card) first"}
        self.busy = True
        prev_domain = self.state.domain if self.state.domain != domain else None
        was_running = self.state.domain == domain
        try:
            if not boot and self.state.pending_start:
                self.cancel_pending_start()  # a manual start supersedes an older intention
            changed: dict[str, Any] = {"stopped": None, "disabled": [], "enabled": []}
            if prev_domain:
                changed["stopped"] = prev_domain
                changed["disabled"] = await self._disable_entries(prev_domain)
            switching = rec.get("running_tag") != tag
            backup = None
            effective = bool(changed["stopped"]) or switching or self.state.domain != domain
            if effective:
                # Snapshot of the state exactly before the start (registries,
                # config entries, the deployed files): a few hundred KB, and
                # exactly what a rollback wants.
                label = f"pre-update-{domain}-{rec['running_tag']}" if switching and rec.get("running_tag") else f"pre-start-{domain}-{tag}"
                pre = await self.async_backup(label)
                await self.hass.async_add_executor_job(backupkit.prune, self.config_dir, self.settings.backup_keep, self.protected_backups() | {pre["name"]})
                backup = pre["name"]
            deployed = await self.hass.async_add_executor_job(self._ensure_deployed, domain, tag)
            failed = await self.hass.async_add_executor_job(self._install_requirements, await self._requirements_for(domain))
            if failed and switching and rec.get("running_tag"):
                # the new version's requirements are not there: put the old files back, nothing else changed yet
                await self.hass.async_add_executor_job(self._ensure_deployed, domain, rec["running_tag"])
                # pip installs one by one: what succeeded before the failure may have
                # moved a library to the new version's pin; put the old pins back
                old_failed = await self.hass.async_add_executor_job(self._install_requirements, await self._requirements_for(domain))
                if old_failed:
                    self.state.restart_required = True
                if prev_domain:
                    await self._enable_entries(prev_domain)  # A was disabled for a switch that did not happen
                self.state.last_error = f"pip failed for: {', '.join(failed)}; {domain} stays on {rec['running_tag']}" \
                    + (f"; restoring its own requirements failed too ({', '.join(old_failed)}): restart" if old_failed else "")
                self._save_state()
                return {"ok": False, "error": self.state.last_error, "pip_failed": failed}
            if failed:
                self.state.last_error = f"pip failed for: {', '.join(failed)}"
            if switching:
                rec["previous_tag"] = rec.get("running_tag")
                if backup:
                    rec["pre_update_backup"] = backup
            rec["running_tag"] = tag
            self.state.domain = domain
            patch_outcome = await self.hass.async_add_executor_job(self._apply_patches, domain)
            # New code for a module this process already imported only takes
            # effect after a restart (Python cannot reload an integration).
            loaded = self._loaded_tags.get(domain)
            needs_restart = domain in self.hass.config.components and loaded is not None and loaded != tag
            if not needs_restart and not await self._loadable(domain):
                # HA scanned custom_components at boot; a domain deployed since is
                # invisible to its loader until a restart
                needs_restart = True
            if not needs_restart:
                changed["enabled"] = await self._enable_entries(domain)
                self._loaded_tags[domain] = tag
            yaml_pending = (not was_running) and os.path.isfile(self.yaml_path(domain)) and not needs_restart and not boot
            # YAML config is only read at boot: an integration started now runs without it until a restart
            self.state.restart_required = self.state.restart_required or needs_restart or yaml_pending
            self.state.last_action = f"started {domain} {tag}; patches: {patch_outcome}" + ("; restart required" if needs_restart else "") \
                + ("; restart required for its YAML config" if yaml_pending else "")
            self._save_state()
            events.emit("switch" if (switching and was_running) else "start",
                        f"{domain} {tag}" + (f" (from {rec.get('previous_tag')})" if switching and was_running else "")
                        + (f"; stopped {prev_domain}" if prev_domain else "") + ("; restart required" if needs_restart or yaml_pending else ""),
                        domain=domain, tag=tag, patches=patch_outcome, pip_failed=failed)
            if effective and not needs_restart and not yaml_pending:
                self._schedule_smoke(domain, tag, switching and bool(backup))
            elif effective:
                # entries get enabled by the reconcile of the next boot; the
                # smoke test runs there too.  An older timer must not fire in
                # between and wipe this record.
                if self._smoke_handle is not None:
                    self._smoke_handle.cancel()
                    self._smoke_handle = None
                self.state.pending_smoke = {"domain": domain, "tag": tag, "can_rollback": switching and bool(backup)}
                self._save_state()
            return {"ok": True, "domain": domain, "tag": tag, "deployed": deployed, "restart_required": needs_restart or yaml_pending,
                    "pre_update_backup": backup, "patches": patch_outcome, "pip_failed": failed, **changed}
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("start %s %s failed", domain, tag)
            self.state.last_error = f"{type(err).__name__}: {err}"
            events.emit("error", f"start {domain} {tag} failed: {self.state.last_error}", domain=domain, tag=tag)
            if prev_domain and self.state.domain == prev_domain:
                # nothing was switched: give the previous integration its entries back
                try:
                    await self._enable_entries(prev_domain)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("could not re-enable %s after the failed start", prev_domain)
            self._save_state()
            return {"ok": False, "error": self.state.last_error}
        finally:
            self.busy = False

    # ----- YAML config per integration ------------------------------------

    def yaml_path(self, domain: str) -> str:
        return os.path.join(self.state_dir, "yaml", f"{domain}.yaml")

    def yaml_read(self, domain: str) -> str:
        try:
            with open(self.yaml_path(domain), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def yaml_write(self, domain: str, text: str) -> dict[str, Any]:
        """Validate with HA's loader (tags like !secret resolve against
        secrets.yaml) and store; empty text removes the file."""
        path = self.yaml_path(domain)
        if not text.strip():
            try:
                os.remove(path)
            except OSError:
                pass
            return {"keys": 0, "removed": True}
        from homeassistant.util.yaml import load_yaml
        from homeassistant.util.yaml.loader import Secrets

        # validate from a file in the final directory: !secret walks up from
        # the file's directory to <config>/secrets.yaml, exactly as at boot
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        try:
            data = load_yaml(path + ".tmp", Secrets(self.hass.config.path()))
            if not isinstance(data, dict):
                raise ValueError(f"the content must be a mapping: what goes under '{domain}:' in configuration.yaml")
        except Exception:
            os.remove(path + ".tmp")
            raise
        os.replace(path + ".tmp", path)
        return {"keys": len(data), "removed": False}

    # ----- smoke test after a start ---------------------------------------

    @property
    def smoke(self) -> dict[str, Any]:
        return {"pending": self._smoke_pending, "last": self.state.last_smoke}

    def _cancel_smoke(self) -> None:
        if self._smoke_handle is not None:
            self._smoke_handle.cancel()
            self._smoke_handle = None
        self._smoke_pending = None
        self.state.pending_smoke = None

    def _schedule_smoke(self, domain: str, tag: str, can_rollback: bool) -> None:
        delay = self.settings.int_("smoke_test_s", 0, 86400)
        if self._smoke_handle is not None:
            self._smoke_handle.cancel()
            self._smoke_handle = None
        if delay <= 0:
            self._smoke_pending = None
            self.state.pending_smoke = None
            self._save_state()
            return
        self._smoke_pending = {"domain": domain, "tag": tag, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + delay)),
                                 "auto_rollback": can_rollback and self.settings.bool_("auto_rollback")}
        self.state.pending_smoke = {"domain": domain, "tag": tag, "can_rollback": can_rollback}  # survives a restart
        self._save_state()
        self._smoke_handle = self.hass.loop.call_later(
            delay, lambda: self.hass.async_create_task(self._smoke_check(domain, tag, can_rollback)))

    async def _smoke_check(self, domain: str, tag: str, can_rollback: bool) -> None:
        """Health verdict without the boot grace, `smoke_test_s` after a
        start.  ok -> recorded.  Not ok after a version switch with
        auto_rollback -> full rollback + restart; otherwise recorded as the
        last error (health on MQTT shows it too)."""
        self._smoke_handle = None
        if self.state.domain != domain or self.running_tag != tag:
            self._smoke_pending = None
            ps = self.state.pending_smoke
            if isinstance(ps, dict) and (ps.get("domain"), ps.get("tag")) == (domain, tag):
                self.state.pending_smoke = None  # only this start's record: a newer start's survives
                self._save_state()
            return
        still_setting_up = any(e.state.value == "setup_in_progress" for e in self._entries_of(domain) if not e.disabled_by)
        if self.busy or not self.hass.is_running or still_setting_up:
            # an install/start in progress, or (at boot) HA not started / the entry
            # still setting up: judging now would be a false failure -> rollback
            self._smoke_handle = self.hass.loop.call_later(
                60, lambda: self.hass.async_create_task(self._smoke_check(domain, tag, can_rollback)))
            return
        self._smoke_pending = None
        self.state.pending_smoke = None  # the verdict is recorded below, whatever it is
        try:
            h = self.health_source(grace=False) if self.health_source else self.health()
        except Exception as err:  # noqa: BLE001
            h = {"state": "error", "reason": f"health check failed: {err}"}
        ok = h.get("state") == "ok"
        rec = {"domain": domain, "tag": tag, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "state": h.get("state"), "reason": h.get("reason", ""),
               "action": "none"}
        events.emit("smoke", f"{domain} {tag}: {h.get('state')}" + (f" ({h.get('reason')})" if h.get("reason") else "")
                    + ("" if ok else ("; full rollback" if can_rollback and self.settings.bool_("auto_rollback") else "; no automatic rollback")),
                    domain=domain, tag=tag, state=h.get("state"))
        if ok:
            _LOGGER.info("smoke test %s %s: ok", domain, tag)
        elif can_rollback and self.settings.bool_("auto_rollback"):
            _LOGGER.error("smoke test %s %s FAILED (%s: %s): full rollback", domain, tag, h.get("state"), h.get("reason"))
            res = await self.rollback_full(domain)
            if res.get("ok"):
                rec["action"] = f"full rollback to {res['tag']} + restart (restoring {res['restore']})"
                self.state.last_smoke = rec
                self.state.last_error = f"smoke test of {domain} {tag} failed: {h.get('reason')}; rolled back to {res['tag']}"
                self._save_state()
                await self.restart()
                return
            rec["action"] = f"rollback failed: {res.get('error')}"
            self.state.last_error = f"smoke test of {domain} {tag} failed ({h.get('reason')}) and the rollback too: {res.get('error')}"
        else:
            _LOGGER.warning("smoke test %s %s failed: %s: %s (no automatic rollback)", domain, tag, h.get("state"), h.get("reason"))
            self.state.last_error = f"smoke test of {domain} {tag} failed: {h.get('state')}: {h.get('reason')}"
        self.state.last_smoke = rec
        self._save_state()

    # ----- release check ----------------------------------------------------

    async def check_updates(self, force: bool = True) -> dict[str, str]:
        """Newest stable GitHub tag newer than anything in the store, per
        installed integration (badge in the UI; nothing is installed)."""
        out: dict[str, str] = {}
        for domain, rec in list(self.state.installed.items()):
            try:
                rels = await self.releases(domain, force=force)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("release check %s: %s", domain, err)
                continue
            stable = [r["tag"] for r in rels if not r.get("prerelease")]
            have = list(rec.get("versions") or {})
            if stable and have and vkey(max(stable, key=vkey)) > vkey(max(have, key=vkey)):
                out[domain] = max(stable, key=vkey)
        self.updates = out
        self.updates_checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.state.release_updates = dict(out)
        self._save_state()
        return out

    async def stop(self) -> dict[str, Any]:
        domain = self.state.domain
        if not domain:
            return {"ok": False, "error": "nothing is running"}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True
        try:
            try:
                disabled = await self._disable_entries(domain)
            except RuntimeError as err:
                self.state.last_error = str(err)
                self._save_state()
                events.emit("error", f"stop {domain} failed: {err}", domain=domain)
                return {"ok": False, "error": str(err), "restart_required": True}
            self._cancel_smoke()
            cancelled = self.cancel_pending_start()  # "stop" also means "and do not start anything at the restart"
            stays_loaded = self._stays_loaded_until_restart(domain)
            self.state.domain = None
            if stays_loaded:
                # YAML-only: nothing to unload, its code keeps running until the
                # process restarts (no YAML is injected for a stopped domain)
                self.state.restart_required = True
            self.state.last_action = f"stopped {domain}" + ("; YAML-only integration: fully stopped at the next restart" if stays_loaded else "")
            self._save_state()
            events.emit("stop", f"{domain} stopped ({len(disabled)} entries disabled)" + ("; code stays loaded until the restart" if stays_loaded else ""), domain=domain)
            return {"ok": True, "stopped": domain, "disabled": disabled, "restart_required": stays_loaded, "cancelled_pending_start": cancelled,
                    **({"note": "no config entries to unload: the integration keeps running until the process restarts"} if stays_loaded else {})}
        finally:
            self.busy = False

    def _stays_loaded_until_restart(self, domain: str) -> bool:
        """Loaded, but with no config entry to unload (YAML setup): Python
        cannot unload it, only a restart ends it."""
        return domain in self.hass.config.components and not self._entries_of(domain)

    async def _loadable(self, domain: str) -> bool:
        """Can HA's loader see the deployed component?  Its custom-component
        scan is cached from boot: drop the caches and look again."""
        from homeassistant import loader

        self.hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
        integrations = self.hass.data.get(loader.DATA_INTEGRATIONS)
        if isinstance(integrations, dict):
            integrations.pop(domain, None)
        try:
            await loader.async_get_integration(self.hass, domain)
            return True
        except loader.IntegrationNotFound:
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("loader check for %s: %s", domain, err)
            return False

    async def _disable_entries(self, domain: str) -> list[str]:
        """Disable (= unload) the domain's entries.  An entry that refuses to
        unload keeps its connections and tasks: that is reported, and the
        caller must not pretend the integration stopped."""
        out = []
        for entry in self._entries_of(domain):
            if entry.disabled_by is None:
                try:
                    ok = await self.hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
                except HomeAssistantError as err:  # OperationNotAllowed: an entry in migration_error or failed_unload
                    self.state.restart_required = True
                    raise RuntimeError(f"config entry '{entry.title}' of {domain} could not be disabled ({err}): restart the process") from None
                if not ok or entry.state.value == "failed_unload":
                    self.state.restart_required = True
                    raise RuntimeError(f"config entry '{entry.title}' of {domain} did not unload ({entry.state.value}): restart the process")
                out.append(entry.entry_id)
            elif entry.state.value == "failed_unload":
                # HA marks the entry disabled BEFORE it tries to unload it: a second
                # Stop must not mistake a still-running entry for a stopped one
                self.state.restart_required = True
                raise RuntimeError(f"config entry '{entry.title}' of {domain} is still loaded after a failed unload: restart the process")
        return out

    async def _enable_entries(self, domain: str) -> list[str]:
        out = []
        for entry in self._entries_of(domain):
            if entry.disabled_by is not None:
                await self.hass.config_entries.async_set_disabled_by(entry.entry_id, None)
                out.append(entry.entry_id)
        return out

    async def uninstall(self, domain: str) -> dict[str, Any]:
        """Remove every version, the deployed files and the config entries."""
        if domain not in self.state.installed:
            return {"ok": False, "error": f"{domain} is not installed"}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True
        try:
            try:
                await self._remove_domain(domain)
            except RuntimeError as err:  # an entry that refused to unload
                self.state.last_error = str(err)
                self._save_state()
                return {"ok": False, "error": str(err), "restart_required": True}
            self.state.last_action = f"uninstalled {domain}"
            self._save_state()
            events.emit("uninstall", f"{domain} uninstalled (versions, files, config entries)", domain=domain)
            return {"ok": True}
        finally:
            self.busy = False

    async def _remove_domain(self, domain: str) -> None:
        """Everything of one integration: stop it, drop its config entries,
        its deployed files, every version in the store, its user patches,
        its YAML, and its retained MQTT documents (identity hass_<domain>)."""
        ps = self.state.pending_start
        if isinstance(ps, dict) and ps.get("domain") == domain:
            self.state.pending_start = None  # nothing of this integration may start at the next boot
        if domain == self.state.domain:
            self._cancel_smoke()
            await self._disable_entries(domain)
            self.state.domain = None
        if self._stays_loaded_until_restart(domain):
            self.state.restart_required = True  # its code keeps running until then
        for entry in list(self._entries_of(domain)):
            await self.hass.config_entries.async_remove(entry.entry_id)
        await self.hass.async_add_executor_job(shutil.rmtree, self._component_dir(domain), True)
        await self.hass.async_add_executor_job(shutil.rmtree, os.path.join(self.versions_dir, domain), True)
        await self.hass.async_add_executor_job(shutil.rmtree, patches.patch_dir(self.config_dir, domain), True)
        try:
            os.remove(self.yaml_path(domain))  # a later reinstall must not inherit stale YAML
        except OSError:
            pass
        self.state.installed.pop(domain, None)
        self._save_state()
        if self.on_domain_removed is not None:
            try:
                await self.on_domain_removed(instance_key(domain) or "")
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("retained MQTT documents of %s not cleared: %s", domain, err)

    async def rollback_full(self, domain: str | None = None) -> dict[str, Any]:
        """Previous version AND the backup taken before the switch (registries,
        config entry as it was), applied at the restart the caller triggers."""
        import backupkit

        domain = domain or self.state.domain
        rec = self.state.installed.get(domain or "", {})
        if not rec.get("previous_tag") or not rec.get("pre_update_backup"):
            return {"ok": False, "error": "no previous version + pre-update backup recorded for this integration"}
        # start() rewrites previous_tag / pre_update_backup on the same dict:
        # keep what the rollback needs before calling it
        prev_tag, backup = rec["previous_tag"], rec["pre_update_backup"]
        zip_path = os.path.join(self.config_dir, backupkit.BACKUP_DIR, backup)  # backups live in <config>/backups
        if not os.path.isfile(zip_path):
            return {"ok": False, "error": f"the pre-update backup {backup} no longer exists (deleted?); only a plain start of {prev_tag} is possible"}
        if prev_tag not in rec.get("versions", {}):
            return {"ok": False, "error": f"previous version {prev_tag} is no longer in the version store"}
        try:
            # validate BEFORE switching files/pip back: a corrupt zip must not leave a half rollback
            await self.hass.async_add_executor_job(backupkit.validate, zip_path)
        except ValueError as err:
            return {"ok": False, "error": f"the pre-update backup is unusable ({err}); only a plain start of {prev_tag} is possible"}
        res = await self.start(domain, prev_tag)
        if not res.get("ok"):
            return res
        try:
            # not the manager part: start() already wrote a consistent state.json
            # (and it holds this rollback's verdict/last_error); .storage brings
            # back the un-migrated config entry, custom_components the old files
            await self.hass.async_add_executor_job(backupkit.schedule_restore, self.config_dir, backup, ["storage", "custom_components"])
        except (ValueError, OSError) as err:
            return {"ok": False, "error": f"files rolled back, but the backup could not be scheduled: {err}"}
        self._cancel_smoke()
        self.state.restart_required = True
        self.state.last_action = f"full rollback of {domain} to {prev_tag}: restoring {backup} at restart"
        self.state.rollback_backup = backup
        self._save_state()
        events.emit("rollback", f"{domain} back to {prev_tag}; {backup} restored at the next restart", domain=domain, tag=prev_tag)
        return {"ok": True, "tag": prev_tag, "restore": backup, "restart_required": True}

    def pending_start_applies(self) -> bool:
        """The deferred start is for THIS Home Assistant version (the update
        it was prepared for did not fall back)."""
        ps = self.state.pending_start
        return bool(ps) and (not ps.get("ha") or ps["ha"] == homeassistant.const.__version__)

    async def async_run_pending_start(self) -> None:
        """Boot: a start deferred to this (new) Home Assistant venv by the
        environment builder.  run.py already put the domain's YAML into the
        boot config and sets the domain up after us, so a YAML integration
        is complete at this boot."""
        ps = self.state.pending_start
        if not ps:
            return
        if not self.pending_start_applies():
            why = f"Home Assistant is {homeassistant.const.__version__}, not {ps.get('ha')} (the update failed or was rolled back)"
            if ps.get("blocked") != why:
                ps["blocked"] = why
                self.state.last_error = f"deferred start of {ps['domain']} {ps.get('tag')} NOT run: {why}"
                self._save_state()
                events.emit("error", self.state.last_error, domain=ps["domain"], tag=ps.get("tag"))
            return  # kept, blocked: cancel it or fix the HA version
        self.state.pending_start = None
        self._save_state()
        res = await self.start(ps["domain"], ps.get("tag"), boot=True)
        events.emit("start" if res.get("ok") else "error",
                    f"deferred start of {ps['domain']} {ps.get('tag')} after the restart: " + ("ok" if res.get("ok") else str(res.get("error"))),
                    domain=ps["domain"], tag=ps.get("tag"))

    def cancel_pending_start(self) -> dict[str, Any] | None:
        ps = self.state.pending_start
        self.state.pending_start = None
        if ps:
            self.state.last_action = f"cancelled the deferred start of {ps['domain']} {ps.get('tag')}"
            self._save_state()
            events.emit("stop", f"deferred start of {ps['domain']} {ps.get('tag')} cancelled", domain=ps["domain"])
        return ps

    async def async_flush_stores(self) -> int:
        """Write what Home Assistant still holds in memory: config entries, the
        registries and every store an integration saves with a delay (a
        backup taken right after a change would miss it).  Returns how many stores had data
        pending.  Uses the same path HA runs at its final write."""
        from homeassistant.helpers import (
            area_registry as ar, category_registry as cr, device_registry as dr, entity_registry as er,
            floor_registry as fr, label_registry as lr,
        )

        stores = [getattr(self.hass.config_entries, "_store", None)]
        for mod in (er, dr, ar, lr, fr, cr):
            try:
                stores.append(getattr(mod.async_get(self.hass), "_store", None))
            except Exception:  # noqa: BLE001 - a registry that is not loaded yet
                pass
        stores += list(_DELAYED_STORES)  # the integration's own stores with a delayed save pending
        flushed = 0
        seen: set[int] = set()
        for store in stores:
            if store is None or id(store) in seen:
                continue
            seen.add(id(store))
            if store is None or getattr(store, "_data", None) is None:
                continue
            try:
                await store._async_handle_write_data()  # noqa: SLF001 - what EVENT_HOMEASSISTANT_FINAL_WRITE triggers
                flushed += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("store %s could not be flushed before the backup: %s", getattr(store, "key", "?"), err)
        return flushed

    async def async_backup(self, label: str = "") -> dict[str, Any]:
        """A backup that contains what HA knows now, not what it last wrote."""
        import backupkit

        await self.async_flush_stores()
        return await self.hass.async_add_executor_job(backupkit.create, self.config_dir, label)

    async def restart(self) -> None:
        self.state.restart_required = False
        self.state.last_action = "restart requested"
        self._save_state()
        events.emit("restart", "process restart requested")
        await self.hass.async_add_executor_job(self._reset_boot_failures)
        self.hass.async_create_task(self.hass.async_stop())

    def _reset_boot_failures(self) -> None:
        """A deliberate restart before HA reached STARTED must not count as
        a crash for the entrypoint's fallback logic."""
        path = os.path.join(self.state_dir, "ha.json")
        data = jsonio.read_json(path)
        if isinstance(data, dict) and data.get("boot_failures"):
            data["boot_failures"] = 0
            try:
                write_json(path, data)
            except OSError:
                pass

    async def _requirements_for(self, domain: str) -> list[str]:
        """Manifest requirements plus those of the integration's dependencies."""
        manifest = self.installed_manifest(domain) or {}
        return list(manifest.get("requirements", [])) + await self.dependency_requirements(domain)

    async def dependency_requirements(self, domain: str | None = None) -> list[str]:
        manifest = self.installed_manifest(domain)
        if not manifest:
            return []
        seen: set[str] = set()
        todo = list(manifest.get("dependencies", [])) + list(manifest.get("after_dependencies", []))
        reqs: list[str] = []
        while todo:
            dom = todo.pop()
            if dom in seen or dom == domain:
                continue
            seen.add(dom)
            try:
                integ = await loader.async_get_integration(self.hass, dom)
            except loader.IntegrationNotFound:
                continue
            reqs.extend(integ.requirements or [])
            todo.extend(list(integ.dependencies or []) + list(integ.after_dependencies or []))
        return sorted(set(reqs))

    async def async_reconcile(self) -> None:
        """Boot self-heal for the RUNNING integration: deployed files match
        the running tag, requirements present in this venv, patches applied."""
        domain = self.state.domain
        rec = self.state.installed.get(domain or "", {})
        tag = rec.get("running_tag")
        if not domain or not tag:
            if self.state.restart_required:
                self.state.restart_required = False  # this boot IS the restart it asked for
                self._save_state()
            return
        deployed = await self.hass.async_add_executor_job(self._ensure_deployed, domain, tag)
        # requirements BEFORE the entries are enabled (enabling = setting up =
        # importing the libraries): otherwise the process runs the old library
        reqs = await self._requirements_for(domain)
        missing = [r for r in reqs if not pkg_util.is_installed(r)]
        pip_failed: list[str] = []
        if missing:
            self.busy = True
            try:
                pip_failed = await self.hass.async_add_executor_job(self._install_requirements, reqs + missing)
            finally:
                self.busy = False
            if pip_failed:
                _LOGGER.error("reconcile %s: pip failed for %s", domain, pip_failed)
            elif domain in self.hass.config.components:
                self.state.restart_required = True  # installed after the code was already imported
        # patches BEFORE the entries are enabled: enabling imports the code
        user_patches = await self.hass.async_add_executor_job(self._patch_rows, domain)
        pending = any(p["status"] == "pending" for p in user_patches)  # absent/not applicable/failed: nothing to do at boot
        patch_state = "pending" if pending else "applied"
        patched_now = ""
        if deployed or pending:
            self.busy = True
            try:
                patched_now = await self.hass.async_add_executor_job(self._apply_patches, domain)
            finally:
                self.busy = False
            if domain in self.hass.config.components:
                self.state.restart_required = True  # patched after the code was imported
        # a start that ended in restart_required could not enable the entries
        # in the old process (see start()); this process can
        enabled = await self._enable_entries(domain)
        if enabled:
            _LOGGER.info("reconcile %s: enabled config entries %s", domain, enabled)
        pend = self.state.pending_smoke
        if pend:
            # kept in state.json until the verdict is recorded: a restart between
            # the scheduling and the verdict (a container recreate, a crash) must
            # not lose the smoke test
            if pend.get("domain") == domain and pend.get("tag") == tag:
                self._schedule_smoke(domain, tag, bool(pend.get("can_rollback")))
                _LOGGER.info("reconcile %s %s: smoke test scheduled (start needed this restart)", domain, tag)
            else:
                self.state.pending_smoke = None
                self._save_state()
        if patched_now:
            pending, patch_state = False, "applied"
        self._loaded_tags[domain] = tag
        if not deployed and not missing and not pending:
            if not pip_failed:
                self.state.restart_required = False  # this boot IS the restart that was required
                self._save_state()
            return
        _LOGGER.info("reconcile %s %s: deployed=%s missing=%s patch=%s user_patches_pending=%s", domain, tag, deployed, missing, patch_state, pending)
        self.busy = True
        try:
            failed = pip_failed or (await self.hass.async_add_executor_job(self._install_requirements, reqs) if deployed else [])
            outcome = patched_now or await self.hass.async_add_executor_job(self._apply_patches, domain)
            self.state.last_action = f"reconciled {domain} {tag}; patches: {outcome}" + (f"; pip failed: {', '.join(failed)}" if failed else "")
            self.state.last_error = "" if not failed else "reconcile: pip failed"
            if not failed and not (missing and domain in self.hass.config.components):
                self.state.restart_required = False
            self._save_state()
        finally:
            self.busy = False

    # ----- blocking helpers (executor) ------------------------------------

    def _unpack(self, blob: bytes, domain: str, dest: str) -> dict[str, Any]:
        """Blocking: the component directory of a GitHub zipball into ``dest``
        (replaced); returns its manifest.  Shared by the version store and
        the preflight scratch directory."""
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = zf.namelist()
            tops = {n.split("/", 1)[0] for n in names if "/" in n}
            at_root = f"{next(iter(tops))}/custom_components/{domain}/" if len(tops) == 1 else None
            if at_root and any(n.startswith(at_root) for n in names):
                prefix = at_root  # the repository's own component, not a copy deeper in the tree
            else:
                prefix = next((n for n in names if n.endswith(f"custom_components/{domain}/")), None)
            if prefix is None:
                raise RuntimeError(f"custom_components/{domain}/ not in zipball")
            shutil.rmtree(dest, ignore_errors=True)
            os.makedirs(dest)
            root = os.path.realpath(dest)
            try:
                for n in names:
                    if not n.startswith(prefix) or n.endswith("/"):
                        continue
                    target = os.path.realpath(os.path.join(dest, n[len(prefix):]))
                    if not target.startswith(root + os.sep):
                        raise RuntimeError(f"zip member escapes the component dir: {n}")
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(n) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                manifest = self._manifest_at(dest)
                if not manifest or manifest.get("domain") != domain:
                    raise RuntimeError("manifest.json missing or its domain differs")
            except Exception:
                shutil.rmtree(dest, ignore_errors=True)
                raise
        return manifest

    def _store_version(self, blob: bytes, domain: str, tag: str) -> dict[str, Any]:
        final_dir = self._version_dir(domain, tag)
        staging = os.path.join(os.path.dirname(final_dir), ".staging-" + os.path.basename(final_dir))  # cannot be a tag: tags never start with a dot
        manifest = self._unpack(blob, domain, staging)
        final = self._version_dir(domain, tag)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
        return manifest

    # ----- dev mode: install from a directory --------------------------------

    LOCAL_TAG = "local"

    def dev_candidates(self) -> dict[str, Any]:
        """Blocking: what the dev source directory offers: every
        manifest.json found at <dir>/custom_components/<x>/, <dir>/<x>/ or
        <dir>/ itself."""
        root = self.settings.dev_source_dir
        out: dict[str, Any] = {"dir": root, "exists": os.path.isdir(root), "candidates": []}
        if not out["exists"]:
            return out
        places = [root]
        for sub in ("custom_components", "."):
            base = os.path.join(root, sub)
            try:
                places += [os.path.join(base, n) for n in sorted(os.listdir(base)) if not n.startswith(".")]
            except OSError:
                pass
        seen: set[str] = set()
        for p in places:
            real = os.path.realpath(p)
            if real in seen or not os.path.isdir(real):
                continue
            seen.add(real)
            m = self._manifest_at(real)
            if m and m.get("domain"):
                out["candidates"].append({"path": real, "domain": m["domain"], "version": m.get("version"), "name": m.get("name"),
                                          "requirements": m.get("requirements", []), "config_flow": bool(m.get("config_flow")),
                                          "in_registry": m["domain"] in self.registry(),
                                          "installed": self.LOCAL_TAG in (self.state.installed.get(m["domain"]) or {}).get("versions", {})})
        return out

    async def install_local(self, domain: str, path: str | None = None, replace: bool = False) -> dict[str, Any]:
        """Copy an integration from the dev source directory into the version
        store as tag 'local' (replacing the previous copy); a domain unknown
        to the registry is registered as a dev-mode entry.  If that version
        is running, its deployed files are refreshed and a restart is due."""
        import backupkit

        if backupkit.pending(self.config_dir):
            return {"ok": False, "error": "a restore is scheduled for the next restart: restart (or cancel it) first"}
        if (why := self._replace_guard(domain, replace)):
            return {"ok": False, "error": why, "replace_required": True, "current": self.installed_domain}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True  # before the first await: two requests must not both reach the staging directory
        self.state.last_error = ""
        try:
            cands = await self.hass.async_add_executor_job(self.dev_candidates)
            cand = next((c for c in cands["candidates"] if c["domain"] == domain and (path is None or c["path"] == path)), None)
            if cand is None:
                return {"ok": False, "error": f"no {domain} with a manifest.json under {cands['dir']}" if cands["exists"]
                        else f"dev source directory {cands['dir']} does not exist (bind-mount it: see docker-compose.dev.yml)"}
            if domain not in self.registry():
                self.add_to_registry(domain, "", cand.get("name"), local=True)
            tag = self.LOCAL_TAG
            dest = self._version_dir(domain, tag)

            def _copy() -> dict[str, Any]:
                staging = os.path.join(os.path.dirname(dest), ".staging-" + os.path.basename(dest))
                shutil.rmtree(staging, ignore_errors=True)
                shutil.copytree(cand["path"], staging, ignore=shutil.ignore_patterns("__pycache__", ".git", ".mypy_cache", ".pytest_cache"))
                shutil.rmtree(dest, ignore_errors=True)
                os.replace(staging, dest)
                return self._manifest_at(dest) or {}

            manifest = await self.hass.async_add_executor_job(_copy)
            replaced = await self._replace_current(domain)  # after the copy succeeded
            spec = self.spec(domain)
            pin = next((r for r in manifest.get("requirements", []) if _req_name(r).replace("-", "_") in
                        (spec.get("patch_module") or "",)), None)
            self._dom(domain)["versions"][tag] = {"installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": manifest.get("version"),
                                                 "requirements": manifest.get("requirements", []), "pin": pin, "source": cand["path"]}
            self.state.last_action = f"installed {domain} from {cand['path']} as {tag}"
            self._save_state()
            was_running = domain == self.state.domain and self._dom(domain).get("running_tag") == tag
            if was_running:
                await self.hass.async_add_executor_job(self._deploy, domain, tag)
                await self.hass.async_add_executor_job(self._ensure_deployed, domain, tag)
                failed = await self.hass.async_add_executor_job(self._install_requirements, await self._requirements_for(domain))
                self.state.restart_required = True
                self._save_state()
            else:
                failed = []
            events.emit("dev", f"{domain} {tag} from {cand['path']} (version {manifest.get('version')})"
                        + ("; running copy refreshed, restart required" if was_running else ""), domain=domain, path=cand["path"])
            return {"ok": True, "domain": domain, "tag": tag, "version": manifest.get("version"), "path": cand["path"],
                    "redeployed": was_running, "restart_required": was_running, "pip_failed": failed, **replaced}
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("install_local %s failed", domain)
            self.state.last_error = f"{type(err).__name__}: {err}"
            self._save_state()
            return {"ok": False, "error": self.state.last_error}
        finally:
            self.busy = False

    def _deploy(self, domain: str, tag: str) -> None:
        """Copy the stored version into custom_components/<domain>."""
        src = self._version_dir(domain, tag)
        target = self._component_dir(domain)
        tmp = target + ".deploying"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(tmp, target)

    def _ensure_deployed(self, domain: str, tag: str) -> bool:
        """Deploy unless custom_components/<domain> already holds this tag's
        files (compared by manifest version + the .hri-tag marker).  Returns True when
        it deployed."""
        want = self._manifest_at(self._version_dir(domain, tag)) or {}
        have = self.installed_manifest(domain) or {}
        marker = os.path.join(self._component_dir(domain), ".hri-tag")
        try:
            have_tag = open(marker, encoding="utf-8").read().strip()
        except OSError:
            have_tag = None
        # the tag alone is not enough: "local" or a branch name gets new code under
        # the same tag, so the marker also carries when that copy entered the store
        rec = ((self.state.installed.get(domain) or {}).get("versions") or {}).get(tag) or {}
        stamp = f"{tag}\n{rec.get('installed_at') or ''}".strip()
        if have and have.get("version") == want.get("version") and have_tag == stamp:
            return False
        self._deploy(domain, tag)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(stamp)
        return True

    def _install_requirements(self, requirements: list[str], force: bool = False) -> list[str]:
        """pip only for requirements that are not satisfied (HA's
        install_package always spawns pip): a start with nothing new costs
        nothing.  ``force`` reinstalls everything (repair)."""
        failed = []
        todo = [r for r in dict.fromkeys(requirements) if force or not pkg_util.is_installed(r)]
        for req in todo:
            if not pkg_util.install_package(req, constraints=self.constraints, timeout=600):
                failed.append(req)
        if todo:
            importlib.invalidate_caches()
            self._req_versions_cache = {}
        return failed

    def _apply_patches(self, domain: str) -> str:
        parts = []
        tag = self.state.installed.get(domain, {}).get("running_tag")
        results = patches.apply_all(self.config_dir, domain, self.site_packages_for(domain), self._component_dir(domain), tag)
        for res in results:
            parts.append(f"{res['name']}: {res['status']}")
        self._patch_rows(domain)  # refresh the cached summary
        self._notify_patches(domain, results)
        return "; ".join(parts) if parts else "n/a"

    def _notify_patches(self, domain: str, results: list[dict[str, Any]]) -> None:
        """Blocking-safe: a persistent notification while a patch does not fit
        the code it targets (upstream changed it, a file is gone, the module
        failed), dismissed once every patch applies again.  A patch retired
        by its headers ("skipped") is fine."""
        from homeassistant.components import persistent_notification as pn

        nid = f"integration_manager_patches_{domain}"
        bad = [r for r in results if str(r["status"]) not in ("applied", "already applied", "skipped")]
        if not bad:
            pn.dismiss(self.hass, nid)
            return
        lines = "\n".join(f"- {r['name']}: {r['status']}" for r in bad)
        pn.create(self.hass, f"{lines}\n\nThe integration runs without them. On the Integration page, *Edit* a patch and *Check* it "
                  "against the running code to see what changed.", title=f"Patches of {domain} no longer fit", notification_id=nid)
