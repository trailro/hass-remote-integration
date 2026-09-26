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

import asyncio
import importlib
import io
import json
import weakref
import functools
import logging
import os
import re
import shutil
import signal
import site
import stat
import subprocess
import sys
import tempfile
import threading
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

from . import writer
import jsonio
from jsonio import ha_vkey, is_stable_tag, tag_key, vkey, write_json

from . import change_report, events, patches
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com/repos/{repo}"
RAW_GITHUB = "https://raw.githubusercontent.com/{repo}/{tag}/custom_components/{domain}/manifest.json"
BUILTIN_REGISTRY = "/app/registry.json"
MANAGER_DOMAIN = "integration_manager"  # this component; never the integration the container runs
RELEASE_CACHE_S = 300
DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024  # a release zipball; integrations are a few MB
METADATA_MAX_BYTES = 10 * 1024 * 1024  # a release list, a manifest.json or a hacs.json: kilobytes
UNPACK_MAX_BYTES = 300 * 1024 * 1024    # summed uncompressed size of an archive
UNPACK_MAX_MEMBERS = 20000
DEV_COPY_IGNORE = frozenset({"__pycache__", ".git", ".mypy_cache", ".pytest_cache"})
_DOMAIN_RE = re.compile(r"^[a-z0-9_]{1,64}\Z")  # \Z: "$" also matches before a trailing newline
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+@-]{0,100}\Z")  # a release tag, branch or commit; the views check with it too
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")  # GitHub owner/name
SCRATCH_PREFIXES = (".staging-", ".old-", ".preflight-")  # never a tag: tags do not start with a dot
SCRATCH_MAX_AGE_S = 3600
STORE_STAMP = ".hri-stored"  # in a stored version: the install that put that copy there, matched against its record ("stored")
PRE_RESTORE_GRACE_S = 7 * 86400  # as backupkit's upload grace: a restore proves itself wrong within days
_PRE_RESTORE_NAME = re.compile(r"(\d{8}-\d{6})-pre-restore(?:-\d+)?\.zip")  # backupkit.create(label="pre-restore")
CORRUPT_STATE_KEEP = 3  # state.json.corrupt-<stamp> copies kept; the older ones are the same damage, twice removed


def tag_ok(tag: Any) -> bool:
    # a stored tag is a directory name with "/" spelled "%2F" (_version_dir), unpacked first as .staging-<name>:
    # past NAME_MAX that fails only after the download
    return isinstance(tag, str) and bool(_TAG_RE.match(tag)) and ".." not in tag and len(tag) + 2 * tag.count("/") <= 255 - len(".staging-")


_SAVE_LOCKS: dict[str, threading.Lock] = {}
_SAVE_LOCKS_GUARD = threading.Lock()


def save_lock(path: str) -> threading.Lock:
    """One lock per file, for writes that run in the executor (two requests
    are two executor jobs, on two threads)."""
    with _SAVE_LOCKS_GUARD:
        return _SAVE_LOCKS.setdefault(os.path.realpath(path), threading.Lock())


def manager_domain_error(domain: Any) -> str | None:
    """The manager's own domain is never an integration to install, start or remove: deploying it would replace
    custom_components/integration_manager, the code that is running."""
    if str(domain or "").strip().lower() == MANAGER_DOMAIN:
        return f"{MANAGER_DOMAIN} is this manager itself, not an integration it can install, start or remove"
    return None


def bad_requirement(req: Any) -> str | None:
    """Why ``req`` must not reach pip or uv, or None: an option ("-e ...",
    "--index-url ...") or a direct URL ("pkg @ https://...", "pkg @ git+...",
    "pkg @ file://...") in a manifest would change what gets installed from where.
    A URL requirement is also never "installed" to HA (util/package.is_installed
    returns False for any req.url), so it would go to uv at every boot and start."""
    if not isinstance(req, str) or req.strip().startswith("-"):
        return f"requirement {str(req)[:100]!r} is an option, not a package"
    try:
        from packaging.requirements import Requirement

        parsed = Requirement(req)
    except Exception as err:  # noqa: BLE001
        return f"requirement {req[:100]!r} is not a valid requirement ({err})"
    if parsed.url:
        return f"requirement {req[:100]!r} installs from a URL, not from the package index"
    return None


PIP_INSTALL_TIMEOUT_S = 1800  # one requirement, wall clock (uv runs --quiet: no output to watch for progress)
_pip_deadline = threading.local()


class _BoundedPopen(subprocess.Popen):
    """The ``Popen`` that homeassistant.util.package calls for install_package (every supported HA does
    ``with Popen(...) as process: process.communicate()``, inline up to 2026.7, in ``_install`` from 2026.8).

    Exactly Popen, unless the calling thread carries a deadline (_install_requirements sets one): then uv starts
    in its own process group and communicate() waits only until the deadline.  Past it the whole group is killed
    (uv and the build backends it started) and reaped, and communicate() answers as a failed uv would: the
    returncode is -SIGKILL and stderr says why, in the type the caller asked for.  HA's code after it is left
    to report the failure as it reports any other: its ERROR log, its extra-index retry (2026.8+), False."""

    _hri_bounded = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._hri_deadline = getattr(_pip_deadline, "at", None)
        if self._hri_deadline is not None:
            kwargs["start_new_session"] = True
        super().__init__(*args, **kwargs)

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:  # noqa: A002
        if self._hri_deadline is None:
            return super().communicate(input, timeout)
        left = max(0.0, self._hri_deadline - time.monotonic())
        try:
            return super().communicate(input, left if timeout is None else min(left, timeout))
        except subprocess.TimeoutExpired:
            if timeout is not None and timeout < left:
                raise  # the caller's own, shorter timeout: Popen's contract, the process keeps running
            self._hri_kill()
            why = f"did not finish within {PIP_INSTALL_TIMEOUT_S}s: stopped"
            return ("", why) if self.text_mode else (b"", why.encode())
        except BaseException:
            self._hri_kill()
            raise

    def _hri_kill(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except OSError:
            pass
        self.wait()  # not communicate(): a backend that left the group could hold the pipes open


_BOUND_GUARD = threading.Lock()


def _bind_popen(module: Any = None) -> bool:
    """Put _BoundedPopen in place of the ``Popen`` name of homeassistant.util.package (once; after a reload of
    this module it replaces the previous copy's class, never wraps it).  False when that name is something
    else than subprocess.Popen or one of ours: the install then runs without a time limit, as before."""
    module = pkg_util if module is None else module
    with _BOUND_GUARD:
        cur = getattr(module, "Popen", None)
        if cur is _BoundedPopen:
            return True
        if cur is not subprocess.Popen and not getattr(cur, "_hri_bounded", False):
            return False
        module.Popen = _BoundedPopen
        return True


async def read_capped(resp, what: str, limit: int | None = None) -> bytes:
    """The body of a download, refused above ``limit`` (announced or streamed)."""
    limit = DOWNLOAD_MAX_BYTES if limit is None else limit
    if resp.content_length is not None and resp.content_length > limit:
        raise RuntimeError(f"{what}: {resp.content_length} bytes, more than the {limit // 1048576} MB allowed")
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(1 << 16):
        buf += chunk
        if len(buf) > limit:
            raise RuntimeError(f"{what}: more than the {limit // 1048576} MB allowed")
    return bytes(buf)


def _rmtree_under(path: str, base: str) -> None:
    """Blocking: shutil.rmtree, only for a directory strictly inside ``base``
    (a domain or tag like ".." from a damaged state.json must not reach the volume)."""
    real, root = os.path.realpath(path), os.path.realpath(base)
    if not real.startswith(root + os.sep):
        _LOGGER.warning("not deleting %s: outside %s", path, base)
        return
    shutil.rmtree(real, ignore_errors=True)


def instance_key(domain: str | None) -> str | None:
    """Identity everything published derives from; None when nothing runs."""
    return f"hass_{domain}" if domain else None


@dataclass
class Domain:
    versions: dict[str, dict[str, Any]] = field(default_factory=dict)  # tag -> {installed_at, version, requirements, ...}
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
    rollback_at: str | None = None               # when that rollback was recorded: an older restore of the same backup is not its restore
    release_updates: dict[str, str] = field(default_factory=dict)  # last release check: domain -> newest stable tag not in the store
    pending_change: dict[str, Any] | None = None  # {domain, from_tag, to_tag, at, before}: compared once the new version runs
    suspended_entries: list[str] | None = None     # entry ids the manager disabled (stop, switch, flow or import for another integration); None = not recorded yet
    smoke_announced: str | None = None             # "at" of the failed smoke verdict already raised as a notification
    last_restore_reported: str | None = None      # "at" of the restore outcome already put on the timeline
    pending_rollback: dict[str, Any] | None = None  # {domain, tag, backup}: a full rollback whose restore is scheduled but whose start did not finish
    watchdog: dict[str, Any] | None = None         # health watchdog: {restarts: [epoch], attempts, last, gave_up, announced, reloads: [epoch], last_reload}; survives its own restart


_NONE = type(None)
_STATE_TYPES: dict[str, tuple[type, ...]] = {
    "domain": (str, _NONE), "restart_required": (bool,), "last_action": (str,), "last_error": (str,),
    "pending_smoke": (dict, _NONE), "pending_start": (dict, _NONE), "last_smoke": (dict, _NONE), "last_release_check": (int,),
    "rollback_backup": (str, _NONE), "rollback_at": (str, _NONE), "release_updates": (dict,), "pending_change": (dict, _NONE),
    "suspended_entries": (list, _NONE), "smoke_announced": (str, _NONE), "last_restore_reported": (str, _NONE),
    "pending_rollback": (dict, _NONE), "watchdog": (dict, _NONE),
}


def _gh_check(resp, what: str) -> None:
    headers = getattr(resp, "headers", None) or {}
    if resp.status in (403, 429) and (headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After")):
        try:
            when = "resets at " + time.strftime("%H:%M", time.localtime(int(headers["X-RateLimit-Reset"])))
        except (KeyError, TypeError, ValueError):
            when = f"retry after {headers.get('Retry-After')} s" if headers.get("Retry-After") else "try again later"
        raise RuntimeError(f"GitHub {resp.status} for {what}: rate limit reached, {when} (a token in the Integrations card raises the limit)")
    if resp.status in (401, 403, 404):
        raise RuntimeError(f"GitHub {resp.status} for {what}: private repo or bad token? (set a token in the Integrations card)")
    resp.raise_for_status()


def _req_name(req: str) -> str:
    try:
        from packaging.requirements import Requirement

        return Requirement(req).name
    except Exception:  # noqa: BLE001
        return re.split(r"[\s<>=!~;@\[]", req, 1)[0].strip()


_UNREADABLE = object()


def _registry_integrations(path: str) -> dict[str, Any]:
    """The ``integrations`` map of a registry file, {} for anything else.
    The user registry is documented as hand-editable ("add your own in
    /config/integration_manager/registry.json"), so a list, a string or a
    number where the map belongs is a user error to log, not a crash: the
    manager comes up and says what it ignored.  A file that is not JSON at all (empty, a trailing comma) is
    logged the same way; otherwise every integration it adds would leave the Install page without a word."""
    data = jsonio.read_json(path, _UNREADABLE)
    if data is _UNREADABLE:
        if os.path.lexists(path):  # a missing user registry is the normal case
            _LOGGER.error("%s is ignored: it cannot be read as JSON (empty, or a syntax error such as a trailing comma); "
                          "it must be {\"integrations\": {\"<domain>\": {\"repo\": \"owner/name\"}}}", path)
        return {}
    integrations = data.get("integrations") if isinstance(data, dict) else None
    if isinstance(integrations, dict):
        return integrations
    _LOGGER.error("%s is ignored: it must be {\"integrations\": {\"<domain>\": {\"repo\": \"owner/name\"}}}, not %s",
                  path, type(integrations if isinstance(data, dict) else data).__name__)
    return {}


def _mtime(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return -1


_MIN_HA_RE = re.compile(r"\s*\d+\.\d+(?:\.\d+)?(?:b\d+)?\s*")  # what ha_vkey parses (hacs.json may say 2024.1)


def _min_ha_ok(value: Any) -> str | None:
    """hacs.json's minimum Home Assistant version when ha_vkey can compare it, else None: a crafted value
    (thousands of digits, which int() refuses) raised out of every comparison, start() and a version change."""
    text = str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
    return text if len(text) <= 32 and _MIN_HA_RE.fullmatch(text) else None


def _within(stamp: Any, window_s: int) -> bool:
    """Whether an ha.json timestamp is younger than `window_s`; an unreadable
    one counts as young, so a missing date never drops a protection."""
    try:
        return time.time() - time.mktime(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")) < window_s
    except (TypeError, ValueError):
        return True


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
    # set by ManagerDevice.__init__ (manager_device.py); None while there is none, which the watchdog allows for
    manager: Any = None
    # set by __init__: FlowDriver.reload_entry, the path of the manual Reload button; the watchdog's first step
    reload_entry: Any = None

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
        self.settings = Settings(self.state_dir, hass)
        self.health_source = None  # set by __init__: publisher.build_health(grace=...)
        self.on_domain_removed = None  # set by __init__: async (base_topic) -> clears the old MQTT identity
        self.last_identity_cleared = 0  # retained topics that clearing removed at the last uninstall
        self._smoke_pending: dict[str, Any] | None = None
        self._smoke_handle = None
        self._smoke_waiting: dict[tuple[str, str], float] = {}  # (domain, tag) -> when the smoke test first had to wait
        self._smoke_rechecked: set[tuple[str, str]] = set()  # (domain, tag) already given a second look for a setup_retry entry
        self.updates: dict[str, str] = {}  # domain -> newest stable tag not yet in the store
        self.updates_checked_at: str | None = None
        self._loaded_tags: dict[str, str] = {}  # domain -> tag whose code this process imported
        self._code_hash: dict[str, str] = {}  # domain -> content of that code (patched), kept across an uninstall
        self._restart_before_uninstall: dict[str, bool] = {}
        # domain -> bookkeeping before a switch that is waiting for a restart: starting the loaded version
        # again abandons that switch, and the records must not say the other version ever ran
        self._abandoned_switch: dict[str, dict[str, Any]] = {}  # domain -> restart_required before its uninstall asked for one
        self.busy = False
        self._backup_lock = asyncio.Lock()  # backups on their own queue up instead of refusing each other
        self.state_load_error: str | None = None  # a damaged state.json, reported by the boot reconcile
        os.makedirs(self.versions_dir, exist_ok=True)
        self.state = self._load_state()
        self._sweep_scratch()  # after the state: a set-aside copy goes back when the record still describes it
        self.updates = dict(self.state.release_updates or {})  # the badge and the update entity survive a restart
        self._migrate_version_dirs()

    # ----- registry --------------------------------------------------------

    def _builtin_registry(self) -> dict[str, dict[str, Any]]:
        return _registry_integrations(BUILTIN_REGISTRY)

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
            for domain, spec in _registry_integrations(path).items():
                if manager_domain_error(domain):
                    _LOGGER.warning("%s: the %s entry is ignored (%s)", path, domain, manager_domain_error(domain))
                    continue
                if not isinstance(spec, dict) or not (spec.get("repo") or spec.get("local")):
                    continue
                repo = spec.get("repo")
                if repo and not (isinstance(repo, str) and _REPO_RE.match(repo) and ".." not in repo):
                    # as add_to_registry and the views check it: the repo becomes a GitHub API path
                    _LOGGER.warning("%s: the %s entry is ignored (repo %r is not owner/name)", path, domain, str(repo)[:100])
                    continue
                out[str(domain)] = {**out.get(str(domain), {}), **spec}
        return out

    def add_to_registry(self, domain: str, repo: str, name: str | None = None, local: bool = False) -> dict[str, Any]:
        """``local=True`` registers a dev-mode integration installed from a
        directory: no repo, so no releases/updates, everything else works."""
        domain = domain.strip().lower()
        repo = repo.strip().strip("/")
        repo_ok = bool(_REPO_RE.match(repo)) and ".." not in repo  # as the views check: it becomes a GitHub API path
        if not _DOMAIN_RE.match(domain) or (not repo_ok and not (local and not repo)):
            raise ValueError("domain must be a HA domain (a_b), repo must be owner/name")
        if (why := manager_domain_error(domain)):
            raise ValueError(why)
        builtin = self._builtin_registry().get(domain)
        if builtin and builtin.get("repo") != repo:
            raise ValueError(f"{domain} is a built-in registry entry pinned to {builtin['repo']}; use another domain name")
        data = jsonio.read_json(self.user_registry_file, None)
        if not isinstance(data, dict) or not isinstance(data.get("integrations"), dict):
            # a hand-edited file that cannot be read or has the wrong shape is reported at every read and ignored
            # everywhere else; adding an entry has to start from something usable, but the entries typed into the
            # damaged file are kept beside it rather than overwritten
            if os.path.isfile(self.user_registry_file):
                kept = f"{self.user_registry_file}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
                shutil.copy2(self.user_registry_file, kept)
                _LOGGER.warning("registry.json could not be used: kept as %s before adding %s", os.path.basename(kept), domain)
            data = {"integrations": {}}
        entry = {"name": name or domain, "repo": repo}
        if local:
            entry["local"] = True
        data["integrations"][domain] = entry
        os.makedirs(self.state_dir, exist_ok=True)
        write_json(self.user_registry_file, data, fsync=False)  # called from request handlers on the loop
        return self.registry()[domain]

    def spec(self, domain: str | None) -> dict[str, Any]:
        return (self.registry().get(domain) or {}) if domain else {}

    # ----- state -----------------------------------------------------------

    def _keep_corrupt_state(self, why: str) -> str:
        """A copy of the state file the manager is about to stop using, and
        the reason, which _reconcile reports (timeline, last_error, a
        notification): an empty state silently means "nothing runs"."""
        kept = f"{self.state_file}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.copyfile(self.state_file, kept)
        except OSError:
            kept = "(could not be copied)"
        _LOGGER.error("state.json %s; starting with an empty state, the damaged file is kept as %s", why, kept)
        self.state_load_error = (f"state.json {why}: the manager started with an empty state "
                                 f"(a copy is kept as {os.path.basename(kept)})")
        self._prune_corrupt_states()
        return kept

    def _prune_corrupt_states(self, keep: int = CORRUPT_STATE_KEEP) -> list[str]:
        """The copies are only ever read by a human; without this they stay
        for the life of the volume, one per damaged boot."""
        try:
            names = sorted(n for n in os.listdir(self.state_dir) if n.startswith(os.path.basename(self.state_file) + ".corrupt-"))
        except OSError:
            return []
        removed = []
        for name in names[:-keep] if keep > 0 else names:
            try:
                os.remove(os.path.join(self.state_dir, name))
                removed.append(name)
            except OSError:
                continue
        if removed:
            _LOGGER.info("state.json: removed %s older damaged copies (%s kept)", len(removed), keep)
        return removed

    def _load_state(self) -> State:
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError:
            return State()
        except ValueError:
            self._keep_corrupt_state("is not valid JSON")
            return State()
        if not isinstance(data, dict) or "installed" not in data:
            # kept aside like a file that does not parse: both are a state the manager cannot use,
            # and the copy is the only way back to what was installed
            self._keep_corrupt_state("has an unknown layout")
            return State()
        fields = {k: v for k, v in data.items() if k in State.__dataclass_fields__}
        defaults = State()
        for key, types in _STATE_TYPES.items():
            # a field of the wrong type would break the manager far from here (a restart loop at setup, an unhashable backup name)
            if key in fields and not isinstance(fields[key], types):
                if fields[key] is not None:
                    _LOGGER.warning("state.json: %s is a %s, not what it should be; reset", key, type(fields[key]).__name__)
                fields[key] = getattr(defaults, key)
        state = State(**fields)
        for name in [n for n, t in state.release_updates.items() if not isinstance(t, str)]:
            _LOGGER.warning("state.json: dropping the release update of %r (not a tag)", str(name)[:80])
            state.release_updates.pop(name)
        if state.suspended_entries is not None and not all(isinstance(i, str) for i in state.suspended_entries):
            _LOGGER.warning("state.json: suspended_entries holds ids that are not strings; those are dropped")
            state.suspended_entries = [i for i in state.suspended_entries if isinstance(i, str)]
        ps = state.pending_start
        if ps is not None and not (isinstance(ps.get("domain"), str) and isinstance(ps.get("tag"), (str, _NONE)) and isinstance(ps.get("ha"), (str, _NONE))):
            _LOGGER.warning("state.json: dropping the deferred start %r (malformed)", str(ps)[:120])
            state.pending_start = None
        pr = state.pending_rollback
        if pr is not None and not all(isinstance(pr.get(k), str) for k in ("domain", "tag", "backup")):
            _LOGGER.warning("state.json: dropping the interrupted rollback %r (malformed)", str(pr)[:120])
            state.pending_rollback = None
        installed = state.installed if isinstance(state.installed, dict) else {}
        state.installed = {}
        for domain, rec in installed.items():
            # the names become paths (custom_components/<domain>, versions/<domain>/<tag>): a hand-edited or damaged file must not point elsewhere
            if not _DOMAIN_RE.match(str(domain)) or not isinstance(rec, dict):
                _LOGGER.warning("state.json: dropping installed entry %r (not a valid domain)", str(domain)[:80])
                continue
            versions = rec.get("versions") if isinstance(rec.get("versions"), dict) else {}
            for tag in [t for t in versions if not tag_ok(t) or not isinstance(versions[t], dict)]:
                _LOGGER.warning("state.json: dropping %s version %r (not a valid tag or record)", domain, str(tag)[:80])
                versions.pop(tag)
            for tag, vrec in versions.items():
                if not isinstance(vrec.get("requirements", []), list):
                    _LOGGER.warning("state.json: %s %s requirements are not a list; reset", domain, tag)
                    vrec["requirements"] = []
            rec["versions"] = versions
            for key in ("running_tag", "previous_tag"):
                if rec.get(key) is not None and not tag_ok(rec[key]):
                    rec[key] = None
            if not isinstance(rec.get("pre_update_backup"), (str, _NONE)):
                _LOGGER.warning("state.json: %s pre_update_backup is not a backup name; reset", domain)
                rec["pre_update_backup"] = None
            state.installed[domain] = rec
        if state.domain is not None and state.domain not in state.installed:
            state.domain = None
        return state

    def _save_state(self) -> None:
        # on the loop, where the state changes: saves land in the order of the changes, and the file on disk is
        # current as soon as this returns (backups and a restart read it).  Fsynced: a power cut must not bring
        # back a state from before an install or a switch.  Measured on an Unraid user share (shfs), a ~5 KB
        # write + file fsync + directory fsync: p50 1.6 ms, p95 2.2 ms, p99 10 ms, max 14 ms (200 writes), and
        # a save happens a few times per user action, not per event: not worth an async writer with flushes.
        write_json(self.state_file, asdict(self.state))

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

    def _sweep_scratch(self) -> None:
        """Blocking, at setup (nothing installs yet): staging, set-aside and preflight directories a crash left
        in the version store.  A set-aside copy goes back when its version directory is missing (killed mid-swap),
        or holds a copy its record does not describe (killed after the swap, before the new record was saved)."""
        cutoff = time.time() - SCRATCH_MAX_AGE_S
        try:
            domains = [os.path.join(self.versions_dir, d) for d in os.listdir(self.versions_dir)]
        except OSError:
            return
        for base in domains:
            try:
                names = os.listdir(base) if os.path.isdir(base) else []
            except OSError:
                continue
            for name in names:
                path = os.path.join(base, name)
                if not name.startswith(SCRATCH_PREFIXES):
                    continue
                try:
                    final = os.path.join(base, name[len(".old-"):])
                    if name.startswith(".old-") and (not os.path.lexists(final) or self._unrecorded_copy(os.path.basename(base), final)):
                        if os.path.lexists(final):
                            _rmtree_under(final, self.versions_dir)
                        os.replace(path, final)
                        _LOGGER.warning("version store: %s put back (an install stopped before it recorded the copy that replaced it)", path)
                        continue
                    if os.path.getmtime(path) >= cutoff:
                        continue
                except OSError:
                    continue
                _LOGGER.info("version store: removing the leftover %s", path)
                _rmtree_under(path, self.versions_dir)

    def _unrecorded_copy(self, domain: str, final: str) -> bool:
        """Blocking: the stored copy at ``final`` was put there by an install whose record was never saved."""
        tag = os.path.basename(final).replace("%2F", "/").replace("%25", "%")
        rec = ((self.state.installed.get(domain) or {}).get("versions") or {}).get(tag)
        try:
            with open(os.path.join(final, STORE_STAMP), encoding="utf-8") as fh:
                stamp = fh.read().strip()
        except OSError:
            return False  # a copy stored before stamps: nothing tells, it stays as before
        return isinstance(rec, dict) and rec.get("stored") != stamp

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
        recovery = ha_state.get("recovery") if isinstance(ha_state, dict) else None
        if isinstance(recovery, dict) and recovery.get("backup"):
            out.add(str(recovery["backup"]))
        # the backup the last restore came from needs nothing once that restore is over (applied, failed and put
        # back, or dropped at the boot): what it brought back is on the volume, and a restore still scheduled or
        # retried is covered by backupkit.restore_needs.  Pinned by name here, ha.json kept it for good.
        last_restore = ha_state.get("last_restore") if isinstance(ha_state, dict) else None
        if isinstance(last_restore, dict) and last_restore.get("pre_restore") and _within(last_restore.get("at"), PRE_RESTORE_GRACE_S):
            # the copy of what the restore replaced: the only way back from it once restore-pending.json is
            # gone (deleted as soon as the restore succeeds).  Not pinned for good like the source the user
            # picked: ha.json keeps last_restore forever, so an automatic copy would hold a keep slot and
            # refuse deletion for the life of the instance
            out.add(str(last_restore["pre_restore"]))
        # every pre-restore copy of the last 7 days, not only the last restore's: a later restore that was dropped or
        # failed before taking its own copy replaces last_restore, and the copy of the restore before it (the only
        # way back from what that one brought) lost its protection with it.  Dated by the name backupkit gives it.
        bdir = os.path.join(os.path.dirname(self.state_dir), "backups")
        try:
            names = os.listdir(bdir)
        except OSError:
            names = []
        for name in names:
            m = _PRE_RESTORE_NAME.fullmatch(name)
            if not m:
                continue
            try:
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
            except ValueError:  # 20260931-...: no date, so no way to tell it is old; kept like an unreadable stamp
                out.add(name)
                continue
            if _within(stamp, PRE_RESTORE_GRACE_S):
                out.add(name)
        plan = jsonio.read_json(os.path.join(self.state_dir, "rebuild-pending.json"), {}) or {}
        if isinstance(plan, dict):
            for key in ("backup", "boot_backup"):  # boot_backup: taken by the entrypoint right before the clean start
                if plan.get(key):
                    out.add(str(plan[key]))
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

    def _disk_info(self, domain: str) -> dict[str, Any]:
        """Blocking: what _domain_info reads from the volume (status() gathers it in the executor)."""
        tags = list((self.state.installed.get(domain) or {}).get("versions") or {})
        return {"present": {t: os.path.isdir(self._version_dir(domain, t)) for t in tags},
                "manifest": self.installed_manifest(domain) if domain == self.state.domain else None,
                "spec": self.spec(domain)}

    def _domain_info(self, domain: str, patch_rows: list[dict[str, Any]] | None = None,
                     disk: dict[str, Any] | None = None) -> dict[str, Any]:
        rec = self.state.installed.get(domain, {})
        running = domain == self.state.domain
        disk = disk if disk is not None else self._disk_info(domain)
        manifest = disk["manifest"] if running else None
        reqs = manifest.get("requirements", []) if manifest else []
        req_versions = self._requirement_versions(reqs) if running else {}
        entries = self._entries_of(domain)
        versions = {tag: {**v, "dir_present": bool(disk["present"].get(tag))} for tag, v in (rec.get("versions") or {}).items()}
        return {
            "domain": domain,
            "name": disk["spec"].get("name") or domain,
            "repo": disk["spec"].get("repo"),
            "versions": versions,
            "newest_tag": max(versions, key=tag_key) if versions else None,
            "running": running,
            "running_tag": rec.get("running_tag"),
            "previous_tag": rec.get("previous_tag"),
            "pre_update_backup": rec.get("pre_update_backup"),
            "loaded_tag": self._loaded_tags.get(domain),
            "code_version": manifest.get("version") if manifest else None,
            "requirements": req_versions,
            # an integration whose manifest has no requirements has nothing missing: all({}) is True
            "requirements_ok": all(req_versions.values()) if running else None,
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
            # YAML-only integration: no config entry to look at, "loaded" is the verdict.  Stored YAML that did not
            # load it is a failure the watchdog acts on; without YAML nothing was ever configured (it leaves that alone)
            if loaded:
                state, reason = "ok", ""
            elif os.path.isfile(self.yaml_path(domain)):
                state, reason = "error", "not loaded (no config entry; the YAML setup did not load it)"
            else:
                state, reason = "error", "not loaded (no config entry, no YAML setup)"
        elif not active:
            state, reason = "error", "no enabled config entry"
        elif all(e["state"] == "loaded" for e in active):
            state, reason = "ok", ""
        else:
            bad = next(e for e in active if e["state"] != "loaded")
            state, reason = "error", f"config entry '{bad['title']}' is {bad['state']}" + (f": {bad['reason']}" if bad["reason"] else "")
        from .diagnostics import scrub_text  # diagnostics imports this module

        # retained on the broker and recorded by the main Home Assistant: an entry's reason or an error can carry
        # a URL with a token in it (ConfigEntryNotReady(f"cannot connect to {url}")), masked as the UI masks it
        for e in entries:
            if e["reason"]:
                e["reason"] = scrub_text(str(e["reason"]))
        return {
            "integration": domain,
            "tag": rec.get("running_tag"),
            "version": manifest.get("version"),
            "loaded": loaded,
            "entries": entries,
            "restart_required": self.state.restart_required,
            "last_error": scrub_text(self.state.last_error or ""),
            "patch": self._patch_status(domain),
            "state": state,
            "reason": scrub_text(reason),
        }

    async def status(self) -> dict[str, Any]:
        domain = self.state.domain

        def read() -> tuple[dict, dict, dict, str]:
            # user patch modules run their status(ctx) and read files; the version directories, the manifest, the
            # registry files and importlib (metadata, find_spec) are the volume too: executor, once per poll
            domains = list(self.state.installed)
            rows = {d: self._patch_rows(d) for d in domains}
            disk = {d: self._disk_info(d) for d in domains}
            if domain:  # _domain_info then reads the warm cache
                self._requirement_versions(((disk.get(domain) or {}).get("manifest") or self.installed_manifest(domain) or {}).get("requirements", []))
            return rows, disk, self.registry(), self.site_packages

        patch_rows, disk, registry, site_packages = await self.hass.async_add_executor_job(read)
        infos = {d: self._domain_info(d, patch_rows.get(d), disk.get(d)) for d in self.state.installed}
        info = infos.get(domain) if domain else None
        if info:
            deps = await self.dependency_requirements(domain, manifest=disk[domain]["manifest"] if domain in disk else None)
            info["dependency_requirements"] = await self.hass.async_add_executor_job(self._requirement_versions, deps)
        return {
            "integration": self.installed_domain,
            "ha_version": homeassistant.const.__version__,
            "python": sys.version.split()[0],
            "venv": sys.prefix,
            "instance_key": self.instance_key,
            "running": info,
            "installed": infos,
            "smoke_test": self.smoke,
            "watchdog": self.watchdog_status(),
            "updates": self.updates,
            "updates_checked_at": self.updates_checked_at,
            "registry": registry,
            "site_packages": site_packages,
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
                raw = json.loads(await read_capped(resp, f"{spec['repo']} releases", METADATA_MAX_BYTES))
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
        if not spec.get("repo"):
            raise ValueError(f"{domain} is a dev-mode integration (no repository): there is no release to preview")
        session = async_get_clientsession(self.hass)
        async with session.get(RAW_GITHUB.format(repo=spec["repo"], tag=tag, domain=domain), headers=self.settings.github_headers()) as resp:
            if resp.status != 200:
                raise ValueError(f"manifest.json not found for {domain} {tag} (HTTP {resp.status})")
            new = json.loads(await read_capped(resp, f"{spec['repo']}@{tag} manifest.json", METADATA_MAX_BYTES))
        min_ha = None
        try:
            async with session.get(f"https://raw.githubusercontent.com/{spec['repo']}/{tag}/hacs.json", headers=self.settings.github_headers()) as r2:
                if r2.status == 200:
                    min_ha = (json.loads(await read_capped(r2, f"{spec['repo']}@{tag} hacs.json", METADATA_MAX_BYTES)) or {}).get("homeassistant")
        except (ValueError, OSError, AttributeError, RuntimeError):
            min_ha = None  # the preview still shows the manifest
        rec = self.state.installed.get(domain, {})
        cur_tag = rec.get("running_tag")
        old = (await self.hass.async_add_executor_job(self._manifest_at, self._version_dir(domain, cur_tag)) if cur_tag else None) or {}
        old_req, new_req = set(old.get("requirements", [])), set(new.get("requirements", []))
        notes = next((r.get("notes") for r in (self._releases_cache.get(domain) or (0, []))[1] if r.get("tag") == tag), None)
        return {"domain": domain, "tag": tag, "compared_to": cur_tag, "notes": notes,
                "installed_version": old.get("version"), "new_version": new.get("version"),
                "requirements_added": sorted(new_req - old_req), "requirements_removed": sorted(old_req - new_req),
                "requirements_unchanged": sorted(new_req & old_req), "dependencies": new.get("dependencies", []),
                "after_dependencies": new.get("after_dependencies", []), "config_flow": new.get("config_flow"),
                "min_ha_version": min_ha or new.get("homeassistant"),
                "currently_installed_versions": await self.hass.async_add_executor_job(self._requirement_versions, sorted(new_req))}

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
        keep = self.settings.backup_keep
        # protected_backups() reads ha.json, the rebuild plan and the backups directory: in the executor too
        await self.hass.async_add_executor_job(lambda: backupkit.prune(self.config_dir, keep, self.protected_backups() | {pre["name"]}))
        await self._remove_domain(old)
        events.emit("replace", f"{old} replaced by {new_domain}; backup {pre['name']} taken first", old=old, new=new_domain, backup=pre["name"])
        return {"replaced": old, "pre_replace_backup": pre["name"]}

    async def install(self, tag: str, domain: str | None = None, replace: bool = False, archive_ref: str | None = None) -> dict[str, Any]:
        """Download a release into the version store.  Nothing runs yet.
        ``archive_ref``: the commit to download when it is known (the builder's
        check verified it); ``tag`` stays the name in the store."""
        import backupkit

        if not tag_ok(tag) or (archive_ref is not None and not tag_ok(archive_ref)):
            # both become a GitHub URL path, the tag also a directory of the version store
            return {"ok": False, "error": f"invalid tag {str(tag)[:80]!r}"}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True  # while restore-pending.json is read: a restore scheduled meanwhile would go unseen
        try:
            pending = await self.hass.async_add_executor_job(backupkit.pending, self.config_dir)
        finally:
            self.busy = False
        if pending:
            return {"ok": False, "error": self.rollback_restore_refusal() or "a restore is scheduled for the next restart: restart (or cancel it) first"}
        domain = domain or self.installed_domain
        if (why := manager_domain_error(domain)):
            return {"ok": False, "error": why}
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
        fresh = tag not in ((self.state.installed.get(domain) or {}).get("versions") or {})
        stored = recorded = False
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(GITHUB_API.format(repo=spec["repo"]) + f"/zipball/{archive_ref or tag}", headers=self.settings.github_headers()) as resp:
                _gh_check(resp, f"{spec['repo']}@{archive_ref or tag}")
                blob = await read_capped(resp, f"{spec['repo']}@{archive_ref or tag}")
            stamp = os.urandom(8).hex()
            manifest = await self.hass.async_add_executor_job(self._store_version, blob, domain, tag, stamp)  # validated before it replaces anything
            stored = True
            # only now, with the new release verified and in the store, does the
            # current integration go (a bad tag or a GitHub error leaves it untouched)
            replaced = await self._replace_current(domain)
            self._dom(domain)["versions"][tag] = {"installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": manifest.get("version"),
                                                 "requirements": manifest.get("requirements", []),
                                                 "min_ha": manifest.get("_hri_min_ha"), "stored": stamp}
            recorded = True
            self.state.last_action = f"installed {domain} {tag} into the version store"
            self._save_state()
            await self.hass.async_add_executor_job(_rmtree_under, self._aside_dir(domain, tag), self.versions_dir)  # the record describes the new copy
            events.emit("install", f"{domain} {tag} (version {manifest.get('version')}) into the version store", domain=domain, tag=tag)
            self._releases_cache.pop(domain, None)
            if domain in self.updates and is_stable_tag(tag) and vkey(tag) >= vkey(self.updates[domain]):
                self.updates.pop(domain)  # the newer release is in the store now
                self.state.release_updates = dict(self.updates)
                self._save_state()
            was_running = domain == self.state.domain and self._dom(domain).get("running_tag") == tag
            if was_running:  # reinstall of the running version: refresh the files in place
                await self.hass.async_add_executor_job(self._ensure_deployed, domain, tag, True)  # one copy + the .hri-tag marker
                self.state.restart_required = True
                self._save_state()
            return {"ok": True, "domain": domain, "tag": tag, "version": manifest.get("version"), "redeployed": was_running, **replaced}
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("install %s %s failed", domain, tag)
            if fresh and not recorded:
                # never recorded: nothing would ever list or remove that directory (or the empty domain directory staging made)
                await self.hass.async_add_executor_job(self._drop_unrecorded, domain, tag)
            elif stored and not recorded:
                # a reinstall of a recorded tag: the record still describes the copy set aside, which goes back
                await self.hass.async_add_executor_job(self._restore_aside, domain, tag)
            from .diagnostics import scrub_text  # diagnostics imports this module

            # raise_for_status names the URL it answered for: a private repo's zipball is redirected to codeload
            # with ?token= in it, and last_error goes to state.json, the timeline and the page
            self.state.last_error = scrub_text(f"{type(err).__name__}: {err}")
            events.emit("error", f"install {domain} {tag} failed: {self.state.last_error}", domain=domain, tag=tag)
            self._save_state()
            return {"ok": False, "error": self.state.last_error}
        finally:
            self.busy = False

    def _drop_unrecorded(self, domain: str, tag: str) -> None:
        """Blocking: a version directory whose install failed after it was stored."""
        _rmtree_under(self._version_dir(domain, tag), self.versions_dir)
        try:
            os.rmdir(os.path.join(self.versions_dir, domain))  # only when empty: a domain that was never installed
        except OSError:
            pass

    async def remove_version(self, domain: str, tag: str) -> dict[str, Any]:
        if not tag_ok(tag):
            return {"ok": False, "error": f"invalid tag {str(tag)[:80]!r}"}
        rec = self.state.installed.get(domain)
        if not rec or tag not in rec.get("versions", {}):
            return {"ok": False, "error": "not installed"}
        undo = self._rollback_undo
        if domain == self.state.domain and tag in (rec.get("running_tag"), rec.get("previous_tag"), undo[1] if undo and undo[0] == domain else None) \
                and (why := self.rollback_restore_refusal()):
            return {"ok": False, "error": why}  # the version the rollback goes back to, or the one starting again undoes it
        if domain == self.state.domain and rec.get("running_tag") == tag:
            return {"ok": False, "error": "this version is running; stop it or start another version first"}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True
        try:
            rec["versions"].pop(tag, None)
            ps = self.state.pending_start
            if isinstance(ps, dict) and (ps.get("domain"), ps.get("tag")) == (domain, tag):
                self.state.pending_start = None
            if rec.get("previous_tag") == tag:
                rec["previous_tag"] = None
                rec["pre_update_backup"] = None  # a rollback target without its version makes no sense
            if rec.get("running_tag") == tag:  # a stopped domain whose deployed files were this tag
                rec["running_tag"] = None
                rec["pre_update_backup"] = None
            self.state.last_action = f"removed {domain} {tag} from the version store"
            # recorded as gone before the tree goes (as _remove_domain): a crash in between leaves a stray directory, never a record of one that is not there
            self._save_state()
            await self.hass.async_add_executor_job(_rmtree_under, self._version_dir(domain, tag), self.versions_dir)
        finally:
            self.busy = False
        events.emit("remove", f"{domain} {tag} removed from the version store", domain=domain, tag=tag)
        return {"ok": True}

    # ----- start / stop -----------------------------------------------------

    async def start(self, domain: str, tag: str | None = None, boot: bool = False, own_restore: str | None = None) -> dict[str, Any]:
        """Make (domain, tag) the running integration.  ``boot``: called by
        the deferred start during this boot, where run.py applies the YAML
        and sets the domain up right after (no restart for either).
        ``own_restore``: the archive of a restore the caller scheduled itself as the first
        step of the same operation (a full rollback); any other scheduled restore refuses."""
        if tag and not tag_ok(tag):  # none: the running or newest stored tag
            return {"ok": False, "error": f"invalid tag {str(tag)[:80]!r}"}
        if (why := manager_domain_error(domain)):
            return {"ok": False, "error": why}
        rec = self.state.installed.get(domain)
        if not rec or not rec.get("versions"):
            return {"ok": False, "error": f"{domain} is not installed"}
        tag = tag or rec.get("running_tag") or max(rec["versions"], key=tag_key)
        if tag not in rec["versions"] or not os.path.isdir(self._version_dir(domain, tag)):
            return {"ok": False, "error": f"{domain} {tag} is not in the version store"}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        import backupkit

        self.busy = True  # while restore-pending.json is read: a restore scheduled meanwhile would go unseen
        try:
            scheduled = await self.hass.async_add_executor_job(backupkit.pending_archive, self.config_dir)
        finally:
            self.busy = False
        rollback = self._rollback_undo if scheduled is not None and self._rollback_undo and self._rollback_undo[2] == os.path.basename(scheduled) else None
        # the version a full rollback left, started again: the rollback is undone and its restore dropped with it
        undo = rollback if own_restore is None and rollback == (domain, tag, os.path.basename(scheduled or "")) else None
        if scheduled is not None and undo is None and (own_restore is None or os.path.basename(scheduled) != own_restore):
            return {"ok": False, "error": self.rollback_restore_refusal() or "a restore is scheduled for the next restart: restart (or cancel it in the Backup card) first"}
        min_ha = self.min_ha_of(domain, tag)
        if min_ha and not boot and ha_vkey(str(min_ha)) > ha_vkey(homeassistant.const.__version__):
            return {"ok": False, "error": f"{domain} {tag} needs Home Assistant {min_ha} or newer (hacs.json); this is {homeassistant.const.__version__}: "
                                          "update Home Assistant first, or prepare both together in the Environment builder"}
        if (why := next((w for w in map(bad_requirement, rec["versions"][tag].get("requirements") or []) if w), None)):
            return {"ok": False, "error": f"{domain} {tag} is refused: {why}"}
        self.busy = True
        prev_domain = self.state.domain if self.state.domain != domain else None
        was_running = self.state.domain == domain
        deploy_started = False
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
            before = None
            if switching and was_running and rec.get("running_tag") and domain in self.hass.config.components:
                # what the consuming side sees now, compared once the new version runs (change_report.py)
                before = change_report.snapshot(self.hass, domain)
            if effective:
                # Snapshot of the state exactly before the start (registries,
                # config entries, the deployed files): a few hundred KB, and
                # exactly what a rollback wants.
                label = f"pre-update-{domain}-{rec['running_tag']}" if switching and rec.get("running_tag") else f"pre-start-{domain}-{tag}"
                pre = await self.async_backup(label)
                keep = self.settings.backup_keep
                await self.hass.async_add_executor_job(lambda: backupkit.prune(self.config_dir, keep, self.protected_backups() | {pre["name"]}))
                backup = pre["name"]
            deploy_started = True
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
            had_previous = bool(rec.get("running_tag"))  # a first start has nothing a rollback could go back to
            leaving_tag = rec.get("running_tag")
            before_switch = {"previous_tag": rec.get("previous_tag"), "pre_update_backup": rec.get("pre_update_backup"),
                             "restart_required": self.state.restart_required}
            if switching:
                # running_tag is what the files say, not what this process imported: when a switch before
                # this one never got its restart, that tag never ran, and recording it as the rollback
                # target would send a failed smoke test to code nobody has seen boot.  Keep the version
                # that did run - and the backup taken before it was left.
                ran = self._loaded_tags.get(domain)
                if ran is None or ran == rec.get("running_tag"):
                    rec["previous_tag"] = rec.get("running_tag")
                    if backup:
                        rec["pre_update_backup"] = backup
            rec["running_tag"] = tag
            self.state.domain = domain
            if before and (before["entities"] or before["services"]):
                self.state.pending_change = {"domain": domain, "from_tag": rec.get("previous_tag"), "to_tag": tag,
                                             "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "before": before}
            elif switching and isinstance(self.state.pending_change, dict) and self.state.pending_change.get("domain") == domain:
                self.state.pending_change = None  # an older switch's comparison no longer describes what runs
            patch_outcome = await self.hass.async_add_executor_job(self._apply_patches, domain)
            # New code for a module this process already imported only takes
            # effect after a restart (Python cannot reload an integration).
            loaded = self._loaded_tags.get(domain)
            # a module Python already imported (set up, or only its config flow) keeps its old code: files
            # deployed over it (a same-named local or branch build, another tag) only count after a restart
            imported = domain in self.hass.config.components or f"custom_components.{domain}" in sys.modules
            code_hash = await self.hass.async_add_executor_job(self._tree_hash, domain)
            # an uninstall + install of the tag this process imported deploys files again, byte for byte the same code
            same_code = loaded == tag and code_hash is not None and self._code_hash.get(domain) == code_hash
            # a mutable reference (main, a branch, a local build) that got new code: install() refreshed the running
            # copy itself, so _ensure_deployed has nothing left to do and the tag did not change - what did change is
            # the content on disk.  Only known when this process recorded a hash: an adopted integration (boot, or one
            # Home Assistant set up before the manager knew of it) has none, and its deployed files are what runs.
            known_hash = self._code_hash.get(domain)
            code_changed = known_hash is not None and code_hash is not None and known_hash != code_hash
            needs_restart = imported and not same_code and (deployed or code_changed or (loaded is not None and loaded != tag))
            if not needs_restart and not await self._loadable(domain):
                # HA scanned custom_components at boot; a domain deployed since is
                # invisible to its loader until a restart
                needs_restart = True
            abandoned = self._abandoned_switch.pop(domain, None) if same_code else None
            if abandoned is not None:
                # back to the code this process runs: the switch in between never ran
                rec["previous_tag"], rec["pre_update_backup"] = abandoned["previous_tag"], abandoned["pre_update_backup"]
                self.state.restart_required = abandoned["restart_required"]
                if isinstance(self.state.pending_change, dict) and self.state.pending_change.get("domain") == domain:
                    self.state.pending_change = None
            elif switching and needs_restart and loaded is not None and loaded != tag:
                self._abandoned_switch.setdefault(domain, before_switch)
            if same_code and domain in self._restart_before_uninstall:
                # the code the uninstall wanted gone by a restart runs again, unchanged
                self.state.restart_required = self._restart_before_uninstall.pop(domain)
            if not needs_restart:
                changed["enabled"] = await self._enable_entries(domain)
                self._loaded_tags[domain] = tag
                if code_hash is not None:
                    self._code_hash[domain] = code_hash
            yaml_pending = (not was_running) and os.path.isfile(self.yaml_path(domain)) and not needs_restart and not boot
            # YAML config is only read at boot: an integration started now runs without it until a restart
            self.state.restart_required = self.state.restart_required or needs_restart or yaml_pending
            self.state.last_action = f"started {domain} {tag}; patches: {patch_outcome}" + ("; restart required" if needs_restart else "") \
                + ("; restart required for its YAML config" if yaml_pending else "")
            self._save_state()
            if undo is not None:
                # dropped only once this version is recorded: killed in between, the boot restores the older configuration
                # and deploys this version over it, which migrates it again; the other order boots the older code on it
                await self.hass.async_add_executor_job(self._cancel_own_restore, undo[2])
                self._rollback_undo = None
                self.state.rollback_backup = self.state.rollback_at = None  # no restore of it is coming
                self.state.last_action += f"; the full rollback to {leaving_tag} is undone, its restore cancelled"
                self._save_state()
            events.emit("switch" if (switching and was_running) else "start",
                        f"{domain} {tag}" + ((f" (back to the loaded version: the switch to {leaving_tag} is abandoned)" if abandoned is not None
                                              else f" (from {rec.get('previous_tag')})") if switching and was_running else "")
                        + (f"; stopped {prev_domain}" if prev_domain else "") + ("; restart required" if needs_restart or yaml_pending else ""),
                        domain=domain, tag=tag, patches=patch_outcome, pip_failed=failed)
            can_rollback = switching and had_previous and bool(backup) and abandoned is None
            # A start of the version that already runs is not "effective", but the code it deploys is new
            # (a dev build, a repair), or a dev install deployed it just before and asked for the restart.
            # Without a verdict nothing notices that the entry fails to set up and the Overview goes on saying
            # the integration runs.
            verdict = effective or deployed or needs_restart or self.state.restart_required
            if verdict and not needs_restart and not yaml_pending:
                self._schedule_smoke(domain, tag, can_rollback)
            elif verdict:
                # entries get enabled by the reconcile of the next boot; the
                # smoke test runs there too.  An older timer must not fire in
                # between and wipe this record.
                if self._smoke_handle is not None:
                    self._smoke_handle.cancel()
                    self._smoke_handle = None
                self._smoke_pending = None  # that timer's record: the answer and status() would report it as still coming
                self.state.pending_smoke = {"domain": domain, "tag": tag, "can_rollback": can_rollback}
                self._save_state()
            # what this answer promises: ok means deployed and recorded, never "it set up"; the verdict says so
            scheduled = self._smoke_pending or (self.state.pending_smoke if verdict else None)
            if scheduled:
                note = ""
            elif not verdict:
                note = "nothing changed: this version was already deployed and running; no health verdict is scheduled"
            else:
                note = "the start only deployed: the smoke test is off (smoke_test_s = 0), so nothing checks that it sets up"
            return {"ok": True, "domain": domain, "tag": tag, "deployed": deployed, "restart_required": needs_restart or yaml_pending,
                    "pre_update_backup": backup, "patches": patch_outcome, "pip_failed": failed,
                    "smoke_test": scheduled, "note": note, **changed}
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("start %s %s failed", domain, tag)
            from .diagnostics import scrub_text  # diagnostics imports this module

            self.state.last_error = scrub_text(f"{type(err).__name__}: {err}")  # state.json, the timeline, MQTT health
            events.emit("error", f"start {domain} {tag} failed: {self.state.last_error}", domain=domain, tag=tag)
            if prev_domain and self.state.domain == prev_domain:
                # nothing was switched: give the previous integration its entries back
                try:
                    await self._enable_entries(prev_domain)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("could not re-enable %s after the failed start", prev_domain)
            old_tag = rec.get("running_tag")
            if deploy_started and old_tag != tag and old_tag in rec["versions"]:
                # the new files went out but were never recorded: the record and the next boot expect the old ones
                try:
                    await self.hass.async_add_executor_job(self._ensure_deployed, domain, old_tag)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("could not put back the files of %s %s after the failed start", domain, old_tag)
                    self.state.restart_required = True
            if rec.get("running_tag") == tag and self.state.domain == domain:
                self.state.restart_required = True  # recorded as running: the next boot's reconcile sets up files, patches and entries
            try:
                self._save_state()
            except OSError:
                _LOGGER.error("state.json not written after the failed start of %s %s", domain, tag)
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
        from homeassistant.util.yaml import load_yaml
        from homeassistant.util.yaml.loader import Secrets

        path = self.yaml_path(domain)
        # two saves are two executor jobs: one shared "<file>.tmp" let the second write into the file the first
        # had just renamed into place, then fail on the missing temporary file
        with save_lock(path):
            if not text.strip():
                try:
                    os.remove(path)
                except OSError:
                    pass
                return {"keys": 0, "removed": True}
            # validate from a file in the final directory: !secret walks up from
            # the file's directory to <config>/secrets.yaml, exactly as at boot
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{domain}.yaml.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text if text.endswith("\n") else text + "\n")
                data = load_yaml(tmp, Secrets(self.hass.config.path()))
                if not isinstance(data, dict):
                    raise ValueError(f"the content must be a mapping: what goes under '{domain}:' in configuration.yaml")
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
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
        self._smoke_waiting.clear()
        self._smoke_rechecked.clear()

    def _schedule_smoke(self, domain: str, tag: str, can_rollback: bool) -> None:
        delay = self.settings.int_("smoke_test_s", 0, 86400)
        if self._smoke_handle is not None:
            self._smoke_handle.cancel()
            self._smoke_handle = None
        if delay <= 0:
            self._smoke_pending = None
            self.state.pending_smoke = None
            self._save_state()
            if isinstance(self.state.pending_change, dict):
                self._finish_change_later(domain, tag)  # no smoke test to wait for
            return
        self._smoke_pending = {"domain": domain, "tag": tag, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + delay)),
                                 "auto_rollback": can_rollback and self.settings.bool_("auto_rollback")}
        self.state.pending_smoke = {"domain": domain, "tag": tag, "can_rollback": can_rollback}  # survives a restart
        self._save_state()
        self._smoke_handle = self.hass.loop.call_later(
            delay, lambda: self.hass.async_create_task(self._smoke_check(domain, tag, can_rollback)))

    def _finish_change_later(self, domain: str, tag: str) -> None:
        """The change report once the new version had a moment to set up and
        nothing else runs (the smoke test's own conditions)."""

        started = time.monotonic()

        async def _run() -> None:
            if self.state.domain != domain or self.running_tag != tag:
                return
            setting_up = any(e.state.value == "setup_in_progress" for e in self._entries_of(domain) if not e.disabled_by)
            if self.busy or not self.hass.is_running or setting_up:
                if time.monotonic() - started > self.SETUP_WAIT_S + change_report.DELAY_S:
                    change = self.state.pending_change
                    if isinstance(change, dict) and change.get("domain") == domain:
                        self.state.pending_change = None  # never ready: comparing with a half set-up version would lie
                        self._save_state()
                    return
                self.hass.loop.call_later(60, lambda: self.hass.async_create_task(_run()))
                return
            await self.async_finish_change_report(domain, tag)

        self.hass.loop.call_later(change_report.DELAY_S, lambda: self.hass.async_create_task(_run()))

    async def _smoke_check(self, domain: str, tag: str, can_rollback: bool) -> None:
        """Health verdict without the boot grace, `smoke_test_s` after a
        start.  ok -> recorded.  degraded (entities unavailable or silent:
        the version did set up) -> recorded and announced, the version kept.
        error after a version switch with auto_rollback -> full rollback +
        restart (an entry in setup_retry first gets one more interval);
        otherwise recorded as the last error (health on MQTT shows it too)."""
        self._smoke_handle = None
        if self.state.domain != domain or self.running_tag != tag:
            self._smoke_waiting.pop((domain, tag), None)
            self._smoke_pending = None
            ps = self.state.pending_smoke
            if isinstance(ps, dict) and (ps.get("domain"), ps.get("tag")) == (domain, tag):
                self.state.pending_smoke = None  # only this start's record: a newer start's survives
                self._save_state()
            return
        still_setting_up = any(e.state.value == "setup_in_progress" for e in self._entries_of(domain) if not e.disabled_by)
        if still_setting_up:  # the clock runs only while the entry is setting up, not while an install was busy
            waited = time.monotonic() - self._smoke_waiting.setdefault((domain, tag), time.monotonic())
        else:
            self._smoke_waiting.pop((domain, tag), None)
            waited = 0.0
        from .views import _ha_change_lock_taken

        # a full rollback is refused while a version change's lock is held (a switch being cancelled holds it without busy)
        busy = self.busy or _ha_change_lock_taken()
        hung = still_setting_up and not busy and self.hass.is_running and waited > max(self.SETUP_WAIT_S, self.settings.int_("smoke_test_s", 0, 86400))
        if (busy or not self.hass.is_running or still_setting_up) and not hung:
            # an install/start in progress, or (at boot) HA not started / the entry
            # still setting up: judging now would be a false failure -> rollback
            self._smoke_handle = self.hass.loop.call_later(
                60, lambda: self.hass.async_create_task(self._smoke_check(domain, tag, can_rollback)))
            return
        self._smoke_waiting.pop((domain, tag), None)
        try:
            h = self.health_source(grace=False) if self.health_source else self.health()
        except Exception as err:  # noqa: BLE001
            # the manager's own check broke, not necessarily the version: never a reason for a rollback
            pending = self._smoke_pending if isinstance(self._smoke_pending, dict) else {}
            failures = int(pending.get("health_failures") or 0) + 1
            h = {"state": "unknown", "reason": f"the health check failed {failures} times: {type(err).__name__}: {err}"}
            if not hung and failures <= self.SMOKE_HEALTH_RETRIES:
                _LOGGER.warning("smoke test %s %s: the health check failed (%s: %s); checked again in 60 s", domain, tag, type(err).__name__, err)
                events.emit("smoke", f"{domain} {tag}: the health check failed ({type(err).__name__}: {err}); cannot judge, checked again in 60 s",
                            domain=domain, tag=tag, state="unknown")
                self._smoke_pending = {"auto_rollback": can_rollback and self.settings.bool_("auto_rollback"), **pending,
                                       "domain": domain, "tag": tag, "health_failures": failures,
                                       "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 60))}
                self._smoke_handle = self.hass.loop.call_later(
                    60, lambda: self.hass.async_create_task(self._smoke_check(domain, tag, can_rollback)))
                return
        if hung:
            # Home Assistant puts no timeout on an entry's setup: a version that never finishes it is broken
            h = {**h, "state": "error", "reason": f"config entry still setting up after {int(waited)} s"}
        retrying = [e for e in self._entries_of(domain) if not e.disabled_by and e.state.value == "setup_retry"]
        if not hung and h.get("state") not in ("ok", "degraded") and retrying and (domain, tag) not in self._smoke_rechecked:
            # ConfigEntryNotReady (a device or broker not reachable yet): Home Assistant retries on its own,
            # so one more interval before a rollback; still not loaded then, it is judged like any error
            self._smoke_rechecked.add((domain, tag))
            delay = max(60, self.settings.int_("smoke_test_s", 0, 86400))
            self._smoke_pending = {"domain": domain, "tag": tag, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + delay)),
                                   "auto_rollback": can_rollback and self.settings.bool_("auto_rollback"), "recheck": True}
            events.emit("smoke", f"{domain} {tag}: config entry '{retrying[0].title}' is retrying its setup; checked again in {delay} s",
                        domain=domain, tag=tag, state="setup_retry")
            self._smoke_handle = self.hass.loop.call_later(
                delay, lambda: self.hass.async_create_task(self._smoke_check(domain, tag, can_rollback)))
            return
        self._smoke_rechecked.discard((domain, tag))
        self._smoke_pending = None
        self.state.pending_smoke = None  # the verdict is recorded below, whatever it is
        if not hung and h.get("state") == "error" and not self._entries_of(domain) and not os.path.isfile(self.yaml_path(domain)):
            # no config entry and no YAML, before the switch or after it: nothing was ever set up here, so "not loaded" says
            # nothing about the version, and a rollback would take a version away for a setup that never existed
            h = {**h, "state": "unconfigured"}
        ok = h.get("state") == "ok"
        # degraded = set up, but entities unavailable, silent or without a state yet: a device or the bus, which an
        # older version would not bring back; a rollback (restore + restart) is only for a version that does not set up
        degraded = h.get("state") == "degraded"
        unconfigured = h.get("state") == "unconfigured"
        unknown = h.get("state") == "unknown"  # the health check itself kept failing: nothing is known about the version
        rollback = not ok and not degraded and not unconfigured and not unknown and can_rollback and self.settings.bool_("auto_rollback")
        if ok:
            await self.async_finish_change_report(domain, tag)
            prev = self.state.last_smoke if isinstance(self.state.last_smoke, dict) else {}
            # the verdict right after an automatic rollback belongs to that rollback: the failure it undid
            # must stay visible (notification, last error) until the user dismisses it or starts something else
            after_rollback = prev.get("state") != "ok" and str(prev.get("action") or "").startswith(f"full rollback to {tag} ")
            if not after_rollback:
                if self.state.last_error.startswith(f"smoke test of {domain} "):
                    self.state.last_error = ""  # this version is healthy: an older version's failure no longer describes what runs
                self._dismiss_smoke_notification(domain)
            self._notify_yaml_imported(domain)
        elif degraded:
            # set up and kept, so what it changed matters as much as for a healthy one: its entities are registered
            # and unavailable ones keep their state; one with no state at all counts as removed
            await self.async_finish_change_report(domain, tag)
        elif isinstance(self.state.pending_change, dict) and self.state.pending_change.get("domain") == domain:
            self.state.pending_change = None  # a version that did not set up would report its missing entities as "removed"
        rec = {"domain": domain, "tag": tag, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "state": h.get("state"), "reason": h.get("reason", ""),
               "action": "none"}
        events.emit("smoke", f"{domain} {tag}: {h.get('state')}" + (f" ({h.get('reason')})" if h.get("reason") else "")
                    + ("" if ok else "; version kept" if degraded else "; nothing set up, no rollback" if unconfigured
                       else "; full rollback" if rollback else "; no automatic rollback"),
                    domain=domain, tag=tag, state=h.get("state"))
        if ok:
            _LOGGER.info("smoke test %s %s: ok", domain, tag)
        elif rollback:
            _LOGGER.error("smoke test %s %s FAILED (%s: %s): full rollback", domain, tag, h.get("state"), h.get("reason"))
            res = await self.rollback_full(domain, rejected=True)
            if res.get("ok"):
                rec["action"] = f"full rollback to {res['tag']} + restart (restoring {res['restore']})"
                self.state.last_smoke = rec
                self.state.last_error = f"smoke test of {domain} {tag} failed: {h.get('reason')}; rolled back to {res['tag']}"
                self._save_state()
                await self.restart()  # the notification is raised after the restart (announce_smoke): it would not survive it
                return
            rec["action"] = f"rollback failed: {res.get('error')}"
            self.state.last_error = f"smoke test of {domain} {tag} failed ({h.get('reason')}) and the rollback too: {res.get('error')}"
        elif degraded:
            _LOGGER.warning("smoke test %s %s: degraded (%s): version kept, no rollback", domain, tag, h.get("reason"))
            self.state.last_error = f"smoke test of {domain} {tag}: degraded: {h.get('reason')}; version kept"
        elif unconfigured:
            _LOGGER.info("smoke test %s %s: unconfigured (no config entry, no YAML): nothing to judge, no rollback", domain, tag)
        else:
            _LOGGER.warning("smoke test %s %s failed: %s: %s (no automatic rollback)", domain, tag, h.get("state"), h.get("reason"))
            self.state.last_error = f"smoke test of {domain} {tag} failed: {h.get('state')}: {h.get('reason')}"
        self.state.last_smoke = rec
        self._save_state()
        if not ok and not unconfigured:
            self.announce_smoke()

    def announce_smoke(self) -> None:
        """A failed smoke test (and what was done about it) as a persistent notification, once per verdict;
        called after the verdict and at boot (an automatic rollback restarts before it could show)."""
        from homeassistant.components import persistent_notification as pn

        last = self.state.last_smoke
        if not isinstance(last, dict) or last.get("state") == "ok" or not last.get("at") or last.get("at") == self.state.smoke_announced:
            return
        if last.get("state") == "degraded":
            text = (f"The smoke test of {last.get('domain')} {last.get('tag')} found it degraded" + (f" ({last.get('reason')})" if last.get("reason") else "")
                    + ". The version is kept: a degraded integration is not rolled back automatically. Action: none.")
        else:
            text = (f"The smoke test of {last.get('domain')} {last.get('tag')} failed: {last.get('state')}"
                    + (f" ({last.get('reason')})" if last.get("reason") else "") + f". Action: {last.get('action') or 'none'}.")
        pn.async_create(self.hass, text, title="Integration smoke test", notification_id=f"hri_smoke_{last.get('domain')}")
        self.state.smoke_announced = last.get("at")
        self._save_state()

    def _dismiss_smoke_notification(self, domain: str) -> None:
        from homeassistant.components import persistent_notification as pn

        pn.async_dismiss(self.hass, f"hri_smoke_{domain}")

    def _notify_yaml_imported(self, domain: str) -> None:
        """A version with a config flow imported the YAML stored here into a config entry: the YAML is
        still applied at every boot, which can import it again or keep a deprecation warning."""
        from homeassistant.components import persistent_notification as pn

        if not os.path.isfile(self.yaml_path(domain)):
            return
        imported = [e.title for e in self._entries_of(domain) if getattr(e, "source", None) == "import"]
        if imported:
            pn.async_create(self.hass, f"{domain} imported its YAML configuration into a config entry ({', '.join(imported)}). "
                            "The YAML stored on the Integration page is still applied at every boot: remove it there once the entry works.",
                            title="YAML configuration imported", notification_id=f"hri_yaml_imported_{domain}")

    async def async_finish_change_report(self, domain: str, tag: str) -> None:
        """Compare the snapshot taken before a version switch with what the
        new version provides now (change_report.py)."""
        pend = self.state.pending_change
        if not isinstance(pend, dict) or pend.get("domain") != domain or pend.get("to_tag") != tag:
            return
        self.state.pending_change = None
        self._save_state()
        if self.state.domain != domain or self.running_tag != tag:
            return  # rolled back or switched again in the meantime
        report = change_report.build(pend, change_report.snapshot(self.hass, domain))
        await self.hass.async_add_executor_job(change_report.store, self.state_dir, report)
        events.emit("change", change_report.summary(report), domain=domain, from_tag=pend.get("from_tag"), to_tag=tag,
                    breaking=report["breaking"])
        change_report.notify(self.hass, report)

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
                if domain in self.updates:
                    out[domain] = self.updates[domain]  # a failed check keeps what was known
                continue
            stable = [r["tag"] for r in rels if not r.get("prerelease") and is_stable_tag(r["tag"])]
            have = list(rec.get("versions") or {})
            # a branch, a beta or a commit kept for testing must not hide a newer stable release
            have_stable = [t for t in have if is_stable_tag(t)]
            newest = max(stable, key=vkey) if stable else None
            if newest and have and (not have_stable or vkey(newest) > vkey(max(have_stable, key=vkey))):
                out[domain] = newest
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
        if (why := self.rollback_restore_refusal()):
            return {"ok": False, "error": why}
        self.dismiss_patch_notification(domain)
        if isinstance(self.state.pending_change, dict) and self.state.pending_change.get("domain") == domain:
            self.state.pending_change = None  # nothing runs to compare with
        self.busy = True
        try:
            try:
                disabled = await self._disable_entries(domain)
            except RuntimeError as err:
                from .diagnostics import scrub_text  # diagnostics imports this module

                self.state.last_error = scrub_text(str(err))
                self._save_state()
                events.emit("error", f"stop {domain} failed: {self.state.last_error}", domain=domain)
                return {"ok": False, "error": self.state.last_error, "restart_required": True}
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
                    ok = await self.async_suspend_entry(entry)  # disabled by the manager: resumed at the next start
                except HomeAssistantError as err:  # OperationNotAllowed: an entry in migration_error or failed_unload
                    self.state.restart_required = True
                    what = "is disabled but did not unload" if entry.disabled_by is not None else "could not be disabled"
                    raise RuntimeError(f"config entry '{entry.title}' of {domain} {what} ({err}): restart the process") from None
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

    def _suspended(self) -> list[str]:
        """Entries the manager disabled and resumes.  A volume from before this was recorded: every disabled
        entry of an installed integration counts once (what was resumed until then), precise from here on."""
        if self.state.suspended_entries is None:
            self.state.suspended_entries = [e.entry_id for d in self.state.installed for e in self._entries_of(d) if e.disabled_by is not None]
        return self.state.suspended_entries

    def mark_suspended(self, entry_id: str) -> None:
        suspended = self._suspended()
        if entry_id not in suspended:
            suspended.append(entry_id)
            self._save_state()

    async def async_suspend_entry(self, entry: Any) -> bool:
        """Disable an entry on the manager's behalf and record that it did.  Home Assistant sets disabled_by and
        schedules the save before it unloads, and the unload raises for an entry in a state it cannot leave
        (migration_error, failed_unload, setup_in_progress): the entry is disabled on disk all the same, and
        unrecorded it would count as disabled by the user at every later start (never resumed, "no enabled
        config entry").  So the record follows what happened to the entry, not whether the call returned; an
        entry that was already disabled is not the manager's to resume."""
        was_enabled = entry.disabled_by is None
        try:
            return await self.hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        finally:
            if was_enabled and entry.disabled_by is not None:
                self.mark_suspended(entry.entry_id)

    async def _enable_entries(self, domain: str) -> list[str]:
        """Resume what the manager disabled; an entry the user disabled (or imported disabled) stays disabled."""
        out = []
        suspended = self._suspended()
        for entry in self._entries_of(domain):
            if entry.disabled_by is not None and entry.entry_id in suspended:
                await self.hass.config_entries.async_set_disabled_by(entry.entry_id, None)
                out.append(entry.entry_id)
        if out:
            self.state.suspended_entries = [i for i in suspended if i not in out]
            self._save_state()
        return out

    async def uninstall(self, domain: str) -> dict[str, Any]:
        """Remove every version, the deployed files and the config entries."""
        if (why := manager_domain_error(domain)):
            return {"ok": False, "error": why}
        if domain not in self.state.installed:
            return {"ok": False, "error": f"{domain} is not installed"}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        if (why := self.rollback_restore_refusal()):
            # the restore brings back .storage (this integration's entries among them) and custom_components
            return {"ok": False, "error": why}
        self.busy = True
        try:
            try:
                await self._remove_domain(domain)
            except RuntimeError as err:  # an entry that refused to unload
                from .diagnostics import scrub_text  # diagnostics imports this module

                self.state.last_error = scrub_text(str(err))
                self._save_state()
                return {"ok": False, "error": self.state.last_error, "restart_required": True}
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
        self.dismiss_patch_notification(domain)
        if isinstance(self.state.pending_change, dict) and self.state.pending_change.get("domain") == domain:
            self.state.pending_change = None
        ps = self.state.pending_start
        if isinstance(ps, dict) and ps.get("domain") == domain:
            self.state.pending_start = None  # nothing of this integration may start at the next boot
        if domain == self.state.domain:
            self._cancel_smoke()
            await self._disable_entries(domain)
            self.state.domain = None
        if self._stays_loaded_until_restart(domain):
            self._restart_before_uninstall.setdefault(domain, self.state.restart_required)
            self.state.restart_required = True  # its code keeps running until then
        for entry in list(self._entries_of(domain)):
            await self.hass.config_entries.async_remove(entry.entry_id)
        # recorded as gone before the trees go: a crash in between leaves stray files, never a record of files that are not there
        self.state.installed.pop(domain, None)
        self._save_state()
        await self.hass.async_add_executor_job(_rmtree_under, self._component_dir(domain), os.path.join(self.config_dir, "custom_components"))
        await self.hass.async_add_executor_job(_rmtree_under, os.path.join(self.versions_dir, domain), self.versions_dir)
        await self.hass.async_add_executor_job(_rmtree_under, patches.patch_dir(self.config_dir, domain), os.path.dirname(patches.patch_dir(self.config_dir, "_")))
        try:
            os.remove(self.yaml_path(domain))  # a later reinstall must not inherit stale YAML
        except OSError:
            pass
        if self.on_domain_removed is not None:
            try:
                self.last_identity_cleared = await self.on_domain_removed(instance_key(domain) or "") or 0
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("retained MQTT documents of %s not cleared: %s", domain, err)

    _rollback_running = False
    # (domain, the tag a completed full rollback left, the archive of its restore): starting that tag again before
    # the restart undoes the rollback, restore included.  In memory: the restart that ends this process applies it
    _rollback_undo: tuple[str, str, str] | None = None

    async def rollback_full(self, domain: str | None = None, rejected: bool = False) -> dict[str, Any]:
        """Previous version AND the backup taken before the switch (registries,
        config entry as it was), applied at the restart the caller triggers."""
        from .views import _ha_change_lock_taken

        if self._rollback_running:
            # an automatic one overlapping a manual one, or a double click: the second schedule would drop the
            # first one's archive, and its failed start would cancel the restore the first one reports as done
            return {"ok": False, "error": "a full rollback is already running"}
        # its restore is checked and scheduled like a version change's or a restore by hand: under that lock, with busy
        # reserved.  Without them a change prepared between the check and the schedule replaced this restore, or was
        # replaced by it, and both answered ok.  Refused, never waited for; the smoke test defers its verdict while
        # either is held, so the automatic rollback is not refused by them
        if _ha_change_lock_taken() or self.busy:
            return {"ok": False, "error": "a Home Assistant version change or another action is running (an install, start, stop, import, restore or full rollback): try again in a moment"}
        self._rollback_running = True  # before the first await
        self.busy = True
        try:
            return await self._rollback_full(domain, rejected)
        finally:
            self._rollback_running = False
            self.busy = False

    def rollback_restore_refusal(self) -> str | None:
        """While a full rollback's restore waits for the restart: the refusal for whatever would contradict it
        (a stop, an uninstall, another start or rollback, removing a version it involves).  The rollback already
        recorded the version it goes back to, and the restore brings back the configuration that version ran on,
        entries enabled: a stop recorded now is undone by that restore, and the integration would run with the
        manager recording nothing.  Cancel restore is refused for it too (backup_views), so the only ways out are
        the restart, or starting the version the rollback left (which drops its restore)."""
        import backupkit

        backup = self.state.rollback_backup
        if not backup:
            return None
        meta = backupkit._pending_meta(self.config_dir) or {}  # noqa: SLF001
        if meta.get("name") != backup or backupkit.pending_archive(self.config_dir) is None:
            return None
        undo = self._rollback_undo if self._rollback_undo and self._rollback_undo[2] == meta.get("zip") else None
        return "a full rollback restores its backup at the next restart: restart to finish it" \
            + (f", or start {undo[0]} {undo[1]} again to undo it" if undo else "")

    def _cancel_own_restore(self, zip_name: str) -> None:
        """Blocking: cancel the scheduled restore only while it is still this operation's archive."""
        import backupkit

        backupkit.cancel_restore(self.config_dir, only_zip=zip_name)

    async def _rollback_full(self, domain: str | None, rejected: bool) -> dict[str, Any]:
        import backupkit

        domain = domain or self.state.domain
        rec = self.state.installed.get(domain or "", {})
        if not rec.get("previous_tag") or not rec.get("pre_update_backup"):
            return {"ok": False, "error": "no previous version + pre-update backup recorded for this integration"}
        # start() rewrites previous_tag / pre_update_backup on the same dict:
        # keep what the rollback needs before calling it
        prev_tag, backup, left_tag = rec["previous_tag"], rec["pre_update_backup"], rec.get("running_tag")
        zip_path = os.path.join(self.config_dir, backupkit.BACKUP_DIR, backup)  # backups live in <config>/backups
        from .views import _HA_CHANGE_LOCK

        async with _HA_CHANGE_LOCK:  # free: rollback_full checked it, with no await since (every check that awaits is inside)
            if not await self.hass.async_add_executor_job(os.path.isfile, zip_path):
                return {"ok": False, "error": f"the pre-update backup {backup} no longer exists (deleted?); only a plain start of {prev_tag} is possible"}
            if prev_tag not in rec.get("versions", {}):
                return {"ok": False, "error": f"previous version {prev_tag} is no longer in the version store"}
            try:
                # validate BEFORE switching files/pip back: a corrupt zip must not leave a half rollback
                await self.hass.async_add_executor_job(backupkit.validate, zip_path)
            except ValueError as err:
                return {"ok": False, "error": f"the pre-update backup is unusable ({err}); only a plain start of {prev_tag} is possible"}
            if backupkit.pending(self.config_dir):
                return {"ok": False, "error": self.rollback_restore_refusal() or "a restore is scheduled for the next restart: restart (or cancel it in the Backup card) first"}
            # a clean start schedules no archive: this restore would take its place at the boot, and the switch is cancelled there
            ha_state = await self.hass.async_add_executor_job(jsonio.read_json, os.path.join(self.config_dir, backupkit.STATE_DIR, "ha.json"), {})
            change = ha_state.get("change") if isinstance(ha_state, dict) else None
            if isinstance(change, dict) and change.get("mode") in ("restore", "rebuild") and change.get("to") != homeassistant.const.__version__:
                return {"ok": False, "error": f"a switch to Home Assistant {change.get('to')} with a {'configuration restore' if change.get('mode') == 'restore' else 'clean start'} "
                                              "is scheduled: cancel it on System (choose the running version) before a full rollback"}
            # written BEFORE the restore is scheduled, because state.json is what the restore does NOT bring back:
            # killed between the schedule and the state start() writes at its end, the next boot would restore the
            # old files and .storage while state.json still names the rejected version, and the boot reconcile
            # would deploy that version over the restored configuration.  _apply_pending_rollback finishes it there.
            intent_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.state.pending_rollback = {"domain": domain, "tag": prev_tag, "backup": backup, "at": intent_at}
            self._save_state()
            try:
                # scheduled BEFORE the files change: a kill between the two then restores the backup at the next boot
                # (its custom_components are the old code), never boots the old code on the migrated .storage.
                # not the manager part: start() writes a consistent state.json (and it holds this rollback's
                # verdict/last_error); .storage brings back the un-migrated config entry, custom_components the old files
                zip_name = os.path.basename(await self.hass.async_add_executor_job(
                    backupkit.schedule_restore, self.config_dir, backup, ["storage", "custom_components"], None, True))
            except (ValueError, OSError) as err:
                self.state.pending_rollback = None  # nothing is scheduled: there is no rollback to finish at a boot
                self._save_state()
                return {"ok": False, "error": f"the backup could not be scheduled, nothing was changed: {err}"}
        try:
            # busy handed to start(): it refuses while busy and takes it back before its first await, so nothing
            # begins in between; the lock is not needed past the schedule, busy refuses a change or a restore
            self.busy = False
            try:
                res = await self.start(domain, prev_tag, own_restore=zip_name)  # start() refuses other scheduled restores, not this one
            finally:
                self.busy = True  # until rollback_full releases it
        except BaseException:
            await self.hass.async_add_executor_job(self._cancel_own_restore, zip_name)
            self.state.pending_rollback = None
            self._save_state()
            raise
        if not res.get("ok"):
            await self.hass.async_add_executor_job(self._cancel_own_restore, zip_name)
            self.state.pending_rollback = None
            self._save_state()
            return res
        self.state.pending_rollback = None  # start() recorded the rollback's tag: nothing is left half done
        self.state.pending_change = None  # a rollback is not a version change to report
        if rejected:
            # start() recorded the version the smoke test just rejected as "previous", with a backup of its broken
            # state: a Full rollback would put exactly that back.  After an automatic rollback there is nothing to go back to.
            rec["previous_tag"] = None
            rec["pre_update_backup"] = None
        self._cancel_smoke()
        # a verdict after the rollback's restart too, but never another rollback: a failure is reported, not looped
        self.state.pending_smoke = {"domain": domain, "tag": prev_tag, "can_rollback": False}
        self.state.restart_required = True
        self.state.last_action = f"full rollback of {domain} to {prev_tag}: restoring {backup} at restart"
        self.state.rollback_backup, self.state.rollback_at = backup, intent_at
        self._save_state()
        self._rollback_undo = (domain, left_tag, zip_name) if left_tag and left_tag != prev_tag else None
        events.emit("rollback", f"{domain} back to {prev_tag}; {backup} restored at the next restart", domain=domain, tag=prev_tag)
        return {"ok": True, "tag": prev_tag, "restore": backup, "restart_required": True}

    def pending_start_applies(self) -> bool:
        """The deferred start is for THIS Home Assistant version (the update
        it was prepared for did not fall back)."""
        ps = self.state.pending_start
        return bool(ps) and (not ps.get("ha") or ps["ha"] == homeassistant.const.__version__)

    async def async_run_pending_start(self) -> dict[str, Any] | None:
        """Boot: a start deferred to this (new) Home Assistant venv by the
        environment builder.  run.py already put the domain's YAML into the
        boot config and sets the domain up after us, so a YAML integration
        is complete at this boot.  Returns what that start answered, or None
        when there was none to run or it is kept blocked for another version."""
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
            return None  # kept, blocked: cancel it or fix the HA version
        self.state.pending_start = None
        self._save_state()
        res = await self.start(ps["domain"], ps.get("tag"), boot=True)
        events.emit("start" if res.get("ok") else "error",
                    f"deferred start of {ps['domain']} {ps.get('tag')} after the restart: " + ("ok" if res.get("ok") else str(res.get("error"))),
                    domain=ps["domain"], tag=ps.get("tag"))
        return res  # the boot hands MQTT its identity from this, the way every other start path does

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
        failed: list[str] = []
        seen: set[int] = set()
        unique = []
        for store in stores:
            if store is None or id(store) in seen:
                continue
            seen.add(id(store))
            unique.append(store)
            if getattr(store, "_data", None) is None:
                continue
            try:
                await store._async_handle_write_data()  # noqa: SLF001 - what EVENT_HOMEASSISTANT_FINAL_WRITE triggers
                flushed += 1
            except Exception as err:  # noqa: BLE001
                failed.append(f"{getattr(store, 'key', '?')}: {err}")
        for store in unique:
            # a save HA had already started holds this lock (its data was taken, the file not yet written):
            # zipping now would archive the file from before that save
            lock = getattr(store, "_write_lock", None)
            if lock is not None:
                async with lock:
                    pass
        if not await writer.async_drain(30):  # settings, MQTT config and rules saves still queued
            failed.append("manager JSON files: saves still pending after 30 s")
        if failed:
            raise OSError(f"stores could not be written before the backup: {'; '.join(failed[:3])}")
        return flushed

    @property
    def backup_running(self) -> bool:
        return self._backup_lock.locked()

    async def async_backup_exclusive(self, label: str = "") -> dict[str, Any]:
        """A backup on its own (UI, daily, MQTT): busy while it runs, so no
        deploy replaces the files it is zipping.  Another backup is waited for;
        raises ValueError while an install, start or restore is running."""
        async with self._backup_lock:
            if self.busy:
                raise ValueError("an install, start or restore is running: try again in a moment")
            self.busy = True
            try:
                return await self.async_backup(label)
            finally:
                self.busy = False

    async def async_backup(self, label: str = "") -> dict[str, Any]:
        """A backup that contains what HA knows now, not what it last wrote."""
        import backupkit

        await self.async_flush_stores()
        if not os.path.isfile(self.state_file):
            self._save_state()  # a fresh volume has none yet, and a backup without it cannot be restored
        return await self.hass.async_add_executor_job(backupkit.create, self.config_dir, label)

    async def restart(self) -> dict[str, Any]:
        if self.busy:
            return {"ok": False, "error": "another action is running (an install, start, stop, import, restore or full rollback): wait for it to finish"}
        self.busy = True  # before the first await: no install/start may begin while the process goes down
        before = (self.state.restart_required, self.state.last_action)
        try:
            self.state.restart_required = False
            self.state.last_action = "restart requested"
            try:
                self._save_state()
            except OSError as err:
                # what this write holds is cosmetic - a badge and a line of history - and the operator
                # restarting is often how they clear the disk that made it fail.  Refusing the restart here
                # took the UI's, the MQTT action's and the watchdog's only way out.
                _LOGGER.warning("restart: state.json not written (%s); restarting anyway", err)
            events.emit("restart", "process restart requested")
            # on the loop, like _save_state above: as an executor job it queued behind a pool an
            # integration had exhausted and never returned, so the stop below never started
            self._undo_boot_failure()
            if not await writer.async_drain(10):  # the final write drains it too, but a stop stage could time out first
                _LOGGER.warning("restart: JSON saves still pending after 10 s")
        except BaseException as err:
            # nothing stops yet: busy must not stay set (every install/start/restart would be refused until
            # the container restarts), and the state in memory goes back to what was last saved
            self.busy = False
            self.state.restart_required, self.state.last_action = before
            if not isinstance(err, Exception):
                raise
            _LOGGER.error("restart failed before stopping: %s", err)
            return {"ok": False, "error": f"restart failed before stopping: {err}"}
        self._arm_stop_watchdog()
        if (in_place := self.hass.data.get("hri_restart_in_place")) is not None:
            in_place()  # run.py: the Home Assistant app starts over in place once stopped, instead of staying stopped
        # busy stays set from here on: the process is going down, and if async_stop hangs or fails the
        # watchdog exits hard rather than leaving a half-stopped HA that accepts installs again
        self.hass.async_create_task(self.hass.async_stop())
        return {"ok": True}

    def _arm_stop_watchdog(self) -> None:
        """run.py's hard exit, armed here rather than at EVENT_HOMEASSISTANT_STOP:
        a stop that hangs before HA's first stage never fires that event."""
        arm = self.hass.data.get("hri_stop_watchdog")
        if arm is None:  # not the run.py process (a test, or HA started some other way)
            return
        try:
            arm()
        except Exception as err:  # noqa: BLE001 - the restart goes ahead without the safety net
            _LOGGER.error("the stop watchdog could not be armed: %s", err)

    def _undo_boot_failure(self) -> None:
        """A deliberate restart before HA reached STARTED must not count as
        a crash for the entrypoint's fallback logic: take back THIS boot's
        increment, the one entrypoint.py wrote before it started us, exactly
        like run.py's stop path.  Zeroing the count instead wiped the crashes
        of earlier boots, and a version that never boots could restart-loop
        from the UI without the count ever reaching MAX_BOOT_FAILURES.

        run.py publishes its own undo, which also carries the "this boot is
        settled" flag: without it the stop listener would take the same
        increment back a second time, and a boot that was already marked good
        would lose a failure that belongs to the boot after it.  Under the
        per-file lock, which the loop and the executor both take: one small
        JSON write."""
        undo = self.hass.data.get("hri_undo_boot_failure")
        if undo is not None:
            undo()
            return

        def take_back(data: Any) -> dict[str, Any] | None:
            # not the run.py process (a test, or HA started some other way): no stop listener to share with
            if isinstance(data, dict) and data.get("boot_failures"):
                try:  # json.load spells Infinity and NaN, and int() converts neither
                    count = int(data["boot_failures"])
                except (TypeError, ValueError, OverflowError):
                    return None  # written by hand: entrypoint.py reads it as 0 anyway
                return {**data, "boot_failures": max(0, count - 1)}
            return None  # unreadable, or nothing to take back: left alone

        try:
            jsonio.update_json(os.path.join(self.state_dir, "ha.json"), take_back)
        except OSError:
            pass

    # ----- health watchdog ---------------------------------------------------
    # The verdict is judged by the scheduler once a minute; everything that has
    # to survive the restart it triggers (the rate cap, the backoff ladder, the
    # last action, the notification still to be raised) lives in state.json, as
    # the MQTT manager actions keep their limits in manager_actions.json.

    WATCHDOG_BACKOFF_STEPS = 4  # the window doubles at most this often: 15 -> 30 -> 60 -> 120 -> 240 min
    WATCHDOG_RELOADS_PER_DAY = 6  # the first step (reload the config entries) is cheap, but not free: capped on its own
    WATCHDOG_DAY_S = 86400
    watchdog_pending: dict[str, Any] | None = None  # set by the scheduler each tick: {"bad_for_s", "window_s", "reason"}

    def watchdog_record(self) -> dict[str, Any]:
        """The persisted watchdog record, normalised (state.json can be edited by hand)."""
        rec = self.state.watchdog if isinstance(self.state.watchdog, dict) else {}
        now = time.time()
        cut = now - self.WATCHDOG_DAY_S
        runs = [float(t) for t in (rec.get("restarts") or []) if isinstance(t, (int, float)) and not isinstance(t, bool)]
        reloads = [float(t) for t in (rec.get("reloads") or []) if isinstance(t, (int, float)) and not isinstance(t, bool)]
        try:  # json.load spells Infinity and NaN, and int() converts neither
            attempts = max(0, int(rec.get("attempts") or 0))
        except (TypeError, ValueError, OverflowError):
            attempts = 0
        # A restart stamped past the end of the window it is counted in was written by a clock that was ahead (an
        # NTP correction since): it is no more "a restart in the last 24 h" than one from last week, and left in
        # it blocked every automatic restart until real time caught up.  NaN fails both comparisons and goes too.
        return {"restarts": sorted(t for t in runs if cut < t <= now + self.WATCHDOG_DAY_S), "attempts": attempts,
                "last": rec.get("last") if isinstance(rec.get("last"), dict) else None,
                "gave_up": str(rec.get("gave_up") or ""),
                "announced": str(rec.get("announced") or ""),
                "reloads": sorted(t for t in reloads if cut < t <= now + self.WATCHDOG_DAY_S),
                "last_reload": rec.get("last_reload") if isinstance(rec.get("last_reload"), dict) else None}

    def _watchdog_save(self, rec: dict[str, Any]) -> OSError | None:
        """The ledger is kept in memory whatever the disk does (an OSError out of here ended the tick, every
        minute), and saved again at the next change.  The write error is returned: the boot after a restart
        starts from the file, so an automatic restart on a ledger that was not written would reset the daily
        cap and the backoff and could loop (watchdog_restart refuses it; restart() by hand still restarts)."""
        self.state.watchdog = rec
        try:
            self._save_state()
        except OSError as err:
            _LOGGER.warning("health watchdog: state.json not written (%s): the record is kept in memory only", err)
            return err
        return None

    def watchdog_window_s(self) -> int:
        """How long the verdict must have been ``error`` before the next restart:
        the configured window, doubled once per restart that did not help."""
        cfg = self.settings.watchdog()
        step = min(self.watchdog_record()["attempts"], self.WATCHDOG_BACKOFF_STEPS)
        return cfg["after_min"] * 60 * (2 ** step)

    def watchdog_cap_refusal(self) -> tuple[str, bool] | None:
        """The rate cap, checked against what survived the last restart: None while
        the watchdog may act, otherwise (why not, is that a give-up).  The daily
        maximum is a give-up -- it is said once and nothing is tried until the
        integration recovers; the minimum interval is only a wait."""
        cfg, rec, now = self.settings.watchdog(), self.watchdog_record(), time.time()
        if len(rec["restarts"]) >= cfg["max_per_day"]:
            return (f"{len(rec['restarts'])} automatic restarts in the last 24 h is the maximum "
                    f"({cfg['max_per_day']}/day)"), True
        if rec["restarts"]:
            # the same clamp as ManagerDevice._limit_wait: a timestamp further ahead than one interval cannot be
            # right (a clock that was off, corrected since) and limits nothing, and the wait never exceeds the
            # interval - an hour ahead must not answer "43261 min to go" and block every restart until then
            interval, last = cfg["min_interval_min"] * 60, rec["restarts"][-1]
            wait = min(interval, interval - (now - last)) if interval and last <= now + interval else 0
            if wait > 0:
                return f"the last automatic restart was less than {cfg['min_interval_min']} min ago ({int(wait / 60) + 1} min to go)", False
        if rec["gave_up"]:
            # the restarts that filled the day have aged out of the 24 h window: it may act again
            rec["gave_up"] = ""
            self._watchdog_save(rec)
            events.emit("health", "health watchdog: the 24 h window has moved on; it may restart the process again",
                        integration=self.state.domain)
        return None

    def watchdog_recovered(self) -> None:
        """``ok`` held for a whole window: the backoff ladder and a give-up go, the 24 h ledger
        stays (it is a cap on how often the watchdog may act, not on how often the
        integration may break)."""
        rec = self.watchdog_record()
        if not rec["attempts"] and not rec["gave_up"]:
            return  # nothing to reset; restarts that aged out of the 24 h window are dropped on every read
        events.emit("health", "health watchdog: the integration is healthy again; the backoff is reset",
                    integration=self.state.domain)
        rec["attempts"], rec["gave_up"] = 0, ""
        self._watchdog_save(rec)

    def watchdog_reloadable(self) -> list[Any]:
        """The config entries the first step reloads: the running integration's enabled ones.  None for a
        YAML-only integration, which goes straight to the restart."""
        if not self.state.domain or self.reload_entry is None:
            return []
        return [e for e in self._entries_of(self.state.domain) if not e.disabled_by and getattr(e, "entry_id", None)]

    def watchdog_reload_cap_refusal(self) -> str | None:
        """None while a reload may be tried, otherwise why not: the ladder then goes on to the restart."""
        rec = self.watchdog_record()
        if len(rec["reloads"]) >= self.WATCHDOG_RELOADS_PER_DAY:
            return f"{len(rec['reloads'])} automatic reloads in the last 24 h is the maximum ({self.WATCHDOG_RELOADS_PER_DAY}/day)"
        return None

    async def watchdog_reload(self, state: str, reason: str, unhealthy_s: float, window_s: int) -> dict[str, Any]:
        """The first step: reload the running integration's config entries, the way the Reload button
        does (an entry reload revives an integration whose coordinator keeps writing states on a dead
        connection in well under a second).  Recorded before it runs, like a restart."""
        domain = self.state.domain or "the integration"
        entries = self.watchdog_reloadable()
        rec = self.watchdog_record()
        now = time.time()
        rec["reloads"] = rec["reloads"] + [now]
        minutes = int(unhealthy_s / 60)
        rec["last_reload"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "integration": self.state.domain, "state": state,
                              "reason": reason, "unhealthy_s": int(unhealthy_s), "entries": len(entries), "result": "running"}
        self._watchdog_save(rec)
        left = self.WATCHDOG_RELOADS_PER_DAY - len(rec["reloads"])
        events.emit("health", f"health watchdog: health {state} for {minutes} min ({reason}): reloading {domain}'s entries "
                              f"({len(entries)}); if it is not ok {int(window_s / 60)} min after this, the process is restarted; "
                              f"{left} reload{'s' if left != 1 else ''} left today",
                    domain=self.state.domain, reason=reason)
        _LOGGER.warning("health watchdog: %s %s for %s s (%s): reloading its %s config entr%s",
                        domain, state, int(unhealthy_s), reason, len(entries), "y" if len(entries) == 1 else "ies")
        results = []
        for entry in entries:
            try:
                ok = await self.reload_entry(entry.entry_id)
                results.append("ok" if ok else f"'{entry.title}' did not set up")
            except Exception as err:  # noqa: BLE001 - one entry that cannot reload must not stop the ladder
                results.append(f"'{entry.title}': {type(err).__name__}: {err}")
        failed = [r for r in results if r != "ok"]
        outcome = "reloaded" if not failed else "; ".join(failed)
        done = self.watchdog_record()
        if isinstance(done["last_reload"], dict) and done["last_reload"].get("at") == rec["last_reload"]["at"]:
            done["last_reload"] = {**done["last_reload"], "result": outcome}
            self._watchdog_save(done)
        if failed:
            events.emit("health", f"health watchdog: the reload of {domain} did not complete: {outcome}", domain=self.state.domain)
        return {"ok": not failed, "result": outcome}

    async def watchdog_restart(self, reason: str, unhealthy_s: int, state: str = "error") -> dict[str, Any]:
        """Act: record what was done (so the restart cannot lose it), put it on the
        timeline, then restart.  The notification is raised at the next boot
        (announce_watchdog), like the smoke test's."""
        cfg, rec = self.settings.watchdog(), self.watchdog_record()
        was = "in error" if state == "error" else state
        now = time.time()
        rec["restarts"] = rec["restarts"] + [now]
        rec["attempts"] += 1
        left = cfg["max_per_day"] - len(rec["restarts"])
        next_window = cfg["after_min"] * (2 ** min(rec["attempts"], self.WATCHDOG_BACKOFF_STEPS))
        if left <= 0:
            # gave_up is not set here: the next tick finds the cap reached and says so once, in its own line
            plan = "no further automatic restart today: look at the integration"
        else:
            plan = (f"if it is still {was} after {next_window} min, its config entries are reloaded first (if it has any) "
                    f"and it is restarted again after that, at least {cfg['min_interval_min']} min from now; {left} left today")
        rec["last"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "integration": self.state.domain, "reason": reason,
                       "unhealthy_s": int(unhealthy_s), "attempt": rec["attempts"], "next": plan, "state": state}
        if (err := self._watchdog_save(rec)) is not None:  # last_action is not touched: restart() sets its own
            # the restart would boot on the ledger of the file, without this attempt: no cap, no backoff, a loop
            why = f"the watchdog's record could not be written to state.json ({type(err).__name__}: {err})"
            rolled = self.watchdog_record()
            rolled["restarts"] = [t for t in rolled["restarts"] if t != now]
            rolled["attempts"] = max(0, rolled["attempts"] - 1)
            rolled["last"] = {**rec["last"], "attempt": rolled["attempts"], "next": f"not restarted: {why}"}
            self._watchdog_save(rolled)
            _LOGGER.error("health watchdog: %s %s for %s s (%s): not restarting the process: %s",
                          self.state.domain, was, int(unhealthy_s), reason, why)
            events.emit("restart", f"health watchdog: {self.state.domain or 'the integration'} has been {was} for "
                                   f"{int(unhealthy_s / 60)} min ({reason}) but the process is not restarted: {why}; "
                                   "Restart on System still restarts it", domain=self.state.domain, reason=reason)
            return {"ok": False, "error": why}
        events.emit("restart", f"health watchdog: {self.state.domain or 'the integration'} has been {was} for "
                               f"{int(unhealthy_s / 60)} min ({reason}); restarting the process (attempt {rec['attempts']}); {plan}",
                    domain=self.state.domain, attempt=rec["attempts"], reason=reason)
        _LOGGER.error("health watchdog: %s %s for %s s (%s): restarting the process (attempt %s); %s",
                      self.state.domain, was, int(unhealthy_s), reason, rec["attempts"], plan)
        res = await self.restart()
        if not res.get("ok"):
            # the restart itself was refused (an action started between the check and here): the attempt did
            # not happen, so it must not cost a slot of the daily cap nor a step of the backoff
            rolled = self.watchdog_record()
            rolled["restarts"] = [t for t in rolled["restarts"] if t != now]
            rolled["attempts"] = max(0, rolled["attempts"] - 1)
            rolled["gave_up"] = ""
            rolled["last"] = {**rec["last"], "next": f"not restarted: {res.get('error')}"}
            self._watchdog_save(rolled)
            events.emit("restart", f"health watchdog: the restart was refused ({res.get('error')})", domain=self.state.domain)
        return res

    def watchdog_give_up(self, why: str) -> None:
        """Stop trying and say so, once."""
        rec = self.watchdog_record()
        if rec["gave_up"] == why:
            return
        rec["gave_up"] = why
        self._watchdog_save(rec)
        events.emit("error", f"health watchdog: not restarting any more ({why}); it tries again once the integration "
                             f"has been ok for {self.settings.watchdog()['after_min']} min, or when the 24 h window has "
                             "moved on", domain=self.state.domain)

    def announce_watchdog(self) -> None:
        """The last automatic restart as a persistent notification, once per action;
        called at boot, because the restart it describes ended the process that
        would have shown it."""
        from homeassistant.components import persistent_notification as pn

        rec = self.watchdog_record()
        last = rec["last"]
        if not isinstance(last, dict) or not last.get("at") or last.get("at") == rec["announced"]:
            return
        was = "in error" if last.get("state", "error") == "error" else str(last.get("state"))
        pn.async_create(self.hass, f"The health watchdog restarted the process at {last['at']}: "
                                   f"{last.get('integration') or 'the integration'} had been {was} for "
                                   f"{int((last.get('unhealthy_s') or 0) / 60)} min ({last.get('reason') or 'no reason recorded'}). "
                                   f"Attempt {last.get('attempt')}. Next: {last.get('next') or '—'}.",
                        title="Health watchdog", notification_id="hri_watchdog")
        rec["announced"] = last["at"]
        self._watchdog_save(rec)

    def watchdog_status(self) -> dict[str, Any]:
        """What the System page and the status API show."""
        cfg, rec = self.settings.watchdog(), self.watchdog_record()
        return {**cfg, "restarts_24h": len(rec["restarts"]), "attempts": rec["attempts"],
                "window_min": int(self.watchdog_window_s() / 60), "gave_up": rec["gave_up"],
                "last": rec["last"], "pending": self.watchdog_pending,
                "reloads_24h": len(rec["reloads"]), "max_reloads_per_day": self.WATCHDOG_RELOADS_PER_DAY,
                "last_reload": rec["last_reload"]}

    async def _requirements_for(self, domain: str) -> list[str]:
        """Manifest requirements plus those of the integration's dependencies."""
        manifest = await self.hass.async_add_executor_job(self.installed_manifest, domain) or {}  # opens manifest.json
        return list(manifest.get("requirements", [])) + await self.dependency_requirements(domain, manifest=manifest)

    async def dependency_requirements(self, domain: str | None = None, manifest: dict[str, Any] | None = None) -> list[str]:
        manifest = manifest or self.installed_manifest(domain)
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
        the running tag, requirements present in this venv, patches applied.
        Busy throughout: a start or an MQTT action from the UI, which already
        listens, must not interleave with it."""
        self.busy = True
        try:
            await self._reconcile()
        finally:
            self.busy = False

    def _apply_pending_rollback(self) -> None:
        """A full rollback whose restore was scheduled but whose start() never
        finished (the process was killed in between).  The restore brought the old
        files and .storage back; state.json is not part of a rollback's restore and
        still names the version the rollback left, so the deploy below would put it
        back over the configuration that was just restored."""
        intent = self.state.pending_rollback
        if not intent:
            return
        domain, tag, backup = intent["domain"], intent["tag"], intent["backup"]
        ha_state = jsonio.read_json(os.path.join(self.state_dir, "ha.json"), {}) or {}
        last = ha_state.get("last_restore") if isinstance(ha_state, dict) else None
        # "ok" plus the file name is not this rollback's restore: the same archive may have been restored by
        # hand before the intent was written, and that older outcome would pass a rollback whose restore never
        # ran.  The intent carries when it was recorded; an older outcome is not it (an intent from before this
        # field is still trusted on the name alone, as it was)
        restored = isinstance(last, dict) and last.get("ok") and last.get("backup") == backup \
            and str(last.get("at") or "") >= str(intent.get("at") or "")
        rec = self.state.installed.get(domain) or {}
        self.state.pending_rollback = None
        if not restored or tag not in (rec.get("versions") or {}):
            # the restore did not happen (dropped for the Home Assistant version that booted, cancelled by
            # hand, failed): the configuration is still the one the version in state.json runs on
            _LOGGER.warning("the interrupted full rollback of %s to %s is dropped: %s was not restored", domain, tag, backup)
            self.state.rollback_backup = self.state.rollback_at = None  # no restore of it is coming: pruned as any other again
            self.state.last_error = f"the full rollback of {domain} to {tag} was interrupted and {backup} was not restored: {domain} stays on {rec.get('running_tag')}"
            self._save_state()
            events.emit("error", self.state.last_error, domain=domain, tag=tag)
            return
        rec["running_tag"] = tag
        # what came back is the state from before the update, so there is nothing behind it to roll back to
        # (the start that was killed is where a new way back would have been recorded)
        rec["previous_tag"], rec["pre_update_backup"] = None, None
        self.state.domain = domain
        self.state.rollback_backup = self.state.rollback_at = None  # restored: the regular pruning applies to that backup again
        if isinstance(self.state.pending_change, dict) and self.state.pending_change.get("domain") == domain:
            self.state.pending_change = None
        # a verdict for the version that now runs, never another rollback (as after a completed one)
        self.state.pending_smoke = {"domain": domain, "tag": tag, "can_rollback": False}
        self.state.last_action = f"full rollback of {domain} to {tag} completed at this boot: {backup} was restored"
        self._save_state()
        events.emit("rollback", f"{domain} back to {tag}; {backup} restored (the rollback was interrupted and finished at this boot)",
                    domain=domain, tag=tag)

    def release_rollback_backup(self, last_restore: Any) -> bool:
        """Boot: the backup a full rollback restores is protected until that restore is over.  Succeeded: ha.json
        keeps the last outcome for good, so "a restore succeeded" alone is any restore of any earlier day; only an
        outcome of this backup, applied after the rollback was recorded, is it (a volume from before rollback_at:
        the name).  Did not happen (dropped by the entrypoint for the version that boots, cancelled, failed and
        put back): the rollback's schedule is gone from the volume and no restore of it is coming either.  While
        that schedule is still there (a restore kept for a retry), an older outcome of the same backup releases
        nothing."""
        import backupkit

        backup = self.state.rollback_backup
        if not backup:
            return False
        restored = isinstance(last_restore, dict) and last_restore.get("ok") and last_restore.get("backup") == backup \
            and str(last_restore.get("at") or "") >= str(self.state.rollback_at or "")
        if not restored and backupkit.pending(self.config_dir) and (backupkit._pending_meta(self.config_dir) or {}).get("name") == backup:  # noqa: SLF001
            return False
        self.state.rollback_backup = self.state.rollback_at = None  # the regular pruning applies to it again
        self._save_state()
        return True

    def _tag_of_deployed(self, domain: str) -> tuple[str | None, str | None]:
        """(tag, installed_at) from the marker _ensure_deployed writes next to
        the deployed code."""
        try:
            with open(os.path.join(self._component_dir(domain), ".hri-tag"), encoding="utf-8") as fh:
                parts = fh.read().split("\n")
        except OSError:
            return None, None
        tag = parts[0].strip()
        return (tag if tag_ok(tag) else None), (parts[1].strip() if len(parts) > 1 else None)

    def _adopt_from_disk(self) -> str | None:
        """After a damaged state.json: config entries exist for a domain the
        empty state does not know, so that integration IS loaded and
        publishing.  Recording it is strictly better than reporting "nothing
        runs": stop, rollback and the version list come back, and the
        one-integration rule keeps holding (an install would otherwise land
        next to it instead of replacing it).  Nothing is guessed - the version
        store and the marker of the deployed copy say which tag runs - and
        nothing is deployed: a tag that cannot be identified is left out,
        which only means the manager knows less, not something wrong."""
        domains = {e.domain for e in self.hass.config_entries.async_entries() if e.domain != MANAGER_DOMAIN}
        domains = {d for d in domains if d not in self.state.installed and self._manifest_at(self._component_dir(d))}
        if len(domains) != 1:
            if domains:
                _LOGGER.error("not adopting %s after the damaged state.json: exactly one integration runs in a container", sorted(domains))
            return None
        domain = domains.pop()
        tag, installed_at = self._tag_of_deployed(domain)
        versions: dict[str, dict[str, Any]] = {}
        try:
            stored = sorted(os.listdir(os.path.join(self.versions_dir, domain)))
        except OSError:
            stored = []
        for name in stored:
            stored_tag = name.replace("%2F", "/").replace("%25", "%")
            manifest = self._manifest_at(self._version_dir(domain, stored_tag))
            if not tag_ok(stored_tag) or manifest is None:
                continue
            versions[stored_tag] = {"installed_at": installed_at if stored_tag == tag else "", "version": manifest.get("version"),
                                    "requirements": manifest.get("requirements", []), "adopted": True}
        if not versions:
            _LOGGER.error("not adopting %s after the damaged state.json: no version of it is in the store", domain)
            return None
        rec = asdict(Domain())
        rec["versions"] = versions
        rec["running_tag"] = tag if tag in versions else None
        self.state.installed[domain] = rec
        # "running" is what the config entries say, not what the files on disk are: an integration that was
        # stopped has disabled entries, and adopting it as running would enable them at this very reconcile
        if any(e.disabled_by is None for e in self._entries_of(domain)):
            self.state.domain = domain
        self._save_state()
        return domain

    def _adopt_enabled_entries(self) -> str | None:
        """Boot, with nothing recorded as running: an installed integration whose config entries are enabled and
        whose copy is deployed is set up by this very boot (run.py sets up every domain with an entry), so it
        runs whatever state.json says.  A restore can do that: the configuration it brings back has the entries
        enabled while state.json, which it leaves alone, records a stop made after that backup.  As after a
        damaged state.json (_adopt_from_disk), running is what the config entries say: it is recorded as
        running, so Stop, the health verdict, the MQTT identity and the version list describe it
        again.  Exactly one such integration, or none is adopted."""
        domains = sorted(d for d in self.state.installed
                         if any(e.disabled_by is None for e in self._entries_of(d)) and self._manifest_at(self._component_dir(d)))
        if len(domains) != 1:
            if domains:
                _LOGGER.error("not adopting %s at boot: their config entries are enabled while nothing is recorded as running, "
                              "and exactly one integration runs in a container: stop the ones that should not run", domains)
            return None
        domain = domains[0]
        rec = self.state.installed[domain]
        marker, _ = self._tag_of_deployed(domain)
        if marker in (rec.get("versions") or {}):
            rec["running_tag"] = marker  # the deployed copy (a restore may have brought back other files)
        self.state.domain = domain
        enabled = {e.entry_id for e in self._entries_of(domain) if e.disabled_by is None}
        if self.state.suspended_entries:
            # enabled again by what came back: no longer the manager's to resume
            self.state.suspended_entries = [i for i in self.state.suspended_entries if i not in enabled]
        self.state.last_action = f"adopted {domain} {rec.get('running_tag') or '(version unknown)'} at boot: its config entries are enabled"
        self._save_state()
        _LOGGER.warning("boot: %s has enabled config entries and a deployed copy while nothing was recorded as running "
                        "(a restore brought them back?): recorded as running %s", domain, rec.get("running_tag") or "(version unknown)")
        events.emit("start", f"{domain} {rec.get('running_tag') or ''}: adopted at boot, its config entries are enabled "
                    "while nothing was recorded as running".replace("  ", " "), domain=domain, tag=rec.get("running_tag"))
        return domain

    def _report_state_loss(self) -> None:
        """One damaged state.json used to be one log line and a UI that showed
        nothing running while the integration was loaded and publishing."""
        from homeassistant.components import persistent_notification as ha_pn

        adopted = self._adopt_from_disk()
        note = self.state_load_error or ""
        if adopted:
            note += f"; adopted {adopted} {self.running_tag or '(version unknown)'} from the config entries and the version store"
        else:
            note += "; no config entry of an installed integration was found, so nothing is recorded as running"
        self.state_load_error = None
        self.state.last_error = note
        self._save_state()
        events.emit("error", note, domain=adopted)
        ha_pn.async_create(self.hass, note, title="Manager state lost", notification_id="hri_state_lost")

    async def _reconcile(self) -> None:
        if self.state_load_error:
            self._report_state_loss()  # before the rollback/deploy below: they act on the state it repairs
        self._apply_pending_rollback()  # before anything is deployed: it decides which tag this boot runs
        if not self.state.domain and self.state.installed:
            self._adopt_enabled_entries()  # before the entries are set up (run.py waits for this reconcile)
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
        missing = await self.hass.async_add_executor_job(lambda: [r for r in reqs if not pkg_util.is_installed(r)])  # importlib.metadata
        pip_failed: list[str] = []
        if missing:
            pip_failed = await self.hass.async_add_executor_job(self._install_requirements, reqs)
            if pip_failed:
                _LOGGER.error("reconcile %s: pip failed for %s", domain, pip_failed)
            elif domain in self.hass.config.components:
                self.state.restart_required = True  # installed after the code was already imported
        # patches BEFORE the entries are enabled: enabling imports the code
        user_patches = await self.hass.async_add_executor_job(self._patch_rows, domain)
        self._notify_patches(domain, user_patches)  # notifications live in memory: a boot raises it again
        pending = any(p["status"] == "pending" for p in user_patches)  # absent/not applicable/failed: nothing to do at boot
        patch_state = "pending" if pending else "applied"
        patched_now = ""
        if deployed or pending:
            patched_now = await self.hass.async_add_executor_job(self._apply_patches, domain)
            if domain in self.hass.config.components:
                self.state.restart_required = True  # patched after the code was imported
        # a start that ended in restart_required could not enable the entries
        # in the old process (see start()); this process can
        self._loaded_tags[domain] = tag  # before the entries import the code
        code_hash = await self.hass.async_add_executor_job(self._tree_hash, domain)
        if code_hash is not None:
            self._code_hash[domain] = code_hash
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
        change = self.state.pending_change
        if not pend and isinstance(change, dict) and change.get("domain") == domain and change.get("to_tag") == tag \
                and self.settings.int_("smoke_test_s", 0, 86400) <= 0:
            self._finish_change_later(domain, tag)  # the smoke test is off: the report's intent survived the restart
        if patched_now:
            pending, patch_state = False, "applied"
        if not deployed and not missing and not pending:
            if not pip_failed:
                self.state.restart_required = False  # this boot IS the restart that was required
                self._save_state()
            return
        _LOGGER.info("reconcile %s %s: deployed=%s missing=%s patch=%s user_patches_pending=%s", domain, tag, deployed, missing, patch_state, pending)
        failed = pip_failed  # installed above when missing: a second pass would only check them again
        outcome = patched_now or await self.hass.async_add_executor_job(self._apply_patches, domain)
        self.state.last_action = f"reconciled {domain} {tag}; patches: {outcome}" + (f"; pip failed: {', '.join(failed)}" if failed else "")
        if failed:
            self.state.last_error = "reconcile: pip failed"
        elif self.state.last_error.startswith("reconcile:"):
            self.state.last_error = ""  # only what reconcile itself reported: a smoke failure or rollback stays visible across the restart
        if not failed and not (missing and domain in self.hass.config.components):
            self.state.restart_required = False
        self._save_state()

    # ----- blocking helpers (executor) ------------------------------------

    def _unpack(self, blob: bytes, domain: str, dest: str) -> dict[str, Any]:
        """Blocking: the component directory of a GitHub zipball into ``dest``
        (replaced); returns its manifest.  Shared by the version store and
        the preflight scratch directory."""
        import backupkit

        if backupkit.zip_has_more_members(io.BytesIO(blob), UNPACK_MAX_MEMBERS):  # before zipfile reads every header
            raise RuntimeError(f"archive has more than {UNPACK_MAX_MEMBERS} members")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            infos = zf.infolist()
            if sum(i.file_size for i in infos) > UNPACK_MAX_BYTES:  # the declared size: zipfile never reads past it
                raise RuntimeError(f"archive unpacks to more than {UNPACK_MAX_BYTES // 1048576} MB")
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
                for info in infos:
                    n = info.filename
                    if not n.startswith(prefix) or n.endswith("/"):
                        continue
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        # written as a file it would hold the link's target path as its content
                        _LOGGER.warning("%s: symbolic link %s in the archive skipped", domain, n)
                        continue
                    target = os.path.realpath(os.path.join(dest, n[len(prefix):]))
                    if not target.startswith(root + os.sep):
                        raise RuntimeError(f"zip member escapes the component dir: {n}")
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                manifest = self._manifest_at(dest)
                if not manifest or manifest.get("domain") != domain:
                    raise RuntimeError("manifest.json missing or its domain differs")
            except Exception:
                shutil.rmtree(dest, ignore_errors=True)
                raise
        return manifest

    @staticmethod
    def _hacs_min_ha(blob: bytes) -> str | None:
        """Blocking: the minimum Home Assistant version a release declares in its hacs.json (repository root).
        The preflight reads it before anything is unpacked, so the caps _unpack applies are applied here too:
        without them a few hundred KB of archive whose hacs.json is one long compressed run would be
        decompressed whole into memory - and only refused afterwards, by an _unpack that never ran."""
        import backupkit

        try:
            if backupkit.zip_has_more_members(io.BytesIO(blob), UNPACK_MAX_MEMBERS):  # before every header is read
                _LOGGER.warning("hacs.json not read: the archive has more than %s members", UNPACK_MAX_MEMBERS)
                return None
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                if sum(i.file_size for i in zf.infolist()) > UNPACK_MAX_BYTES:  # the declared size, as in _unpack
                    _LOGGER.warning("hacs.json not read: the archive unpacks to more than %s MB",
                                    UNPACK_MAX_BYTES // 1048576)
                    return None
                names = zf.namelist()
                tops = {n.split("/", 1)[0] for n in names if "/" in n}
                path = f"{next(iter(tops))}/hacs.json" if len(tops) == 1 else "hacs.json"
                if path not in names:
                    return None
                if zf.getinfo(path).file_size > METADATA_MAX_BYTES:  # the cap the same file gets over HTTP
                    _LOGGER.warning("%s ignored: it declares more than %s MB", path, METADATA_MAX_BYTES // 1048576)
                    return None
                data = json.loads(zf.read(path))
        except (zipfile.BadZipFile, KeyError, ValueError, StopIteration):
            return None
        value = data.get("homeassistant") if isinstance(data, dict) else None
        return _min_ha_ok(value)

    def min_ha_of(self, domain: str | None, tag: str | None) -> str | None:
        rec = ((self.state.installed.get(domain or "") or {}).get("versions") or {}).get(tag or "") or {}
        return _min_ha_ok(rec.get("min_ha"))  # a record stored before the value was checked

    def _store_version(self, blob: bytes, domain: str, tag: str, stamp: str | None = None) -> dict[str, Any]:
        """Blocking: the release into versions/<domain>/<tag>, validated in staging first.  A copy already
        there is set aside (.old-<tag>), not deleted: install() drops it once the new copy is recorded, or
        puts it back (_restore_aside) when the install fails before that."""
        final = self._version_dir(domain, tag)
        staging = os.path.join(os.path.dirname(final), ".staging-" + os.path.basename(final))  # cannot be a tag: tags never start with a dot
        manifest = self._unpack(blob, domain, staging)
        if (why := next((w for w in map(bad_requirement, manifest.get("requirements", [])) if w), None)):
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"{domain} {tag}: {why}")
        manifest = {**manifest, "_hri_min_ha": self._hacs_min_ha(blob)}  # not written anywhere: the version record keeps it
        if stamp:
            self._write_stamp(staging, stamp)
        aside = self._aside_dir(domain, tag)
        shutil.rmtree(aside, ignore_errors=True)
        if os.path.isdir(final):
            os.replace(final, aside)
        os.replace(staging, final)
        return manifest

    @staticmethod
    def _write_stamp(staging: str, stamp: str) -> None:
        """Blocking: which install this copy is, written before the swap (the record saved after it names the same)."""
        try:
            with open(os.path.join(staging, STORE_STAMP), "w", encoding="utf-8") as fh:
                fh.write(stamp)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _aside_dir(self, domain: str, tag: str) -> str:
        final = self._version_dir(domain, tag)
        return os.path.join(os.path.dirname(final), ".old-" + os.path.basename(final))  # never a tag: tags do not start with a dot

    def _restore_aside(self, domain: str, tag: str) -> None:
        """Blocking: a reinstall that failed after the swap puts back the copy its record describes."""
        aside = self._aside_dir(domain, tag)
        if not os.path.isdir(aside):
            return
        _rmtree_under(self._version_dir(domain, tag), self.versions_dir)
        os.replace(aside, self._version_dir(domain, tag))

    # ----- dev mode: install from a directory --------------------------------

    LOCAL_TAG = "local"
    SETUP_WAIT_S = 900  # a config entry still setting up after this long is judged, not waited for any more
    SMOKE_HEALTH_RETRIES = 3  # a health check that raises is no verdict: tried again this many times, 60 s apart

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
            if os.path.islink(base):
                continue  # links in the directory are never followed, as _store_local copies it
            try:
                places += [os.path.join(base, n) for n in sorted(os.listdir(base)) if not n.startswith(".")]
            except OSError:
                pass
        seen: set[str] = set()
        for p in places:
            if p != root and os.path.islink(p):
                continue
            real = os.path.realpath(p)
            if real in seen or not os.path.isdir(real):
                continue
            seen.add(real)
            m = self._manifest_at(real)
            if m and m.get("domain") and not manager_domain_error(m["domain"]):  # a checkout of this repository has one
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

        if (why := manager_domain_error(domain)):
            return {"ok": False, "error": why}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True  # while restore-pending.json is read: a restore scheduled meanwhile would go unseen
        try:
            pending = await self.hass.async_add_executor_job(backupkit.pending, self.config_dir)
        finally:
            self.busy = False
        if pending:
            return {"ok": False, "error": self.rollback_restore_refusal() or "a restore is scheduled for the next restart: restart (or cancel it) first"}
        if (why := self._replace_guard(domain, replace)):
            return {"ok": False, "error": why, "replace_required": True, "current": self.installed_domain}
        if self.busy:
            return {"ok": False, "error": "another action is running"}
        self.busy = True  # before the first await: two requests must not both reach the staging directory
        self.state.last_error = ""
        tag = self.LOCAL_TAG
        fresh = tag not in ((self.state.installed.get(domain) or {}).get("versions") or {})
        registered = stored = recorded = False
        try:
            cands = await self.hass.async_add_executor_job(self.dev_candidates)
            cand = next((c for c in cands["candidates"] if c["domain"] == domain and (path is None or c["path"] == path)), None)
            if cand is None:
                return {"ok": False, "error": f"no {domain} with a manifest.json under {cands['dir']}" if cands["exists"]
                        else f"dev source directory {cands['dir']} does not exist (bind-mount it: see docker-compose.dev.yml)"}
            if domain not in self.registry():
                await self.hass.async_add_executor_job(self.add_to_registry, domain, "", cand.get("name"), True)  # reads and writes the registries
                registered = True
            stamp = os.urandom(8).hex()
            manifest = await self.hass.async_add_executor_job(self._store_local, cand["path"], domain, tag, stamp)
            stored = True
            replaced = await self._replace_current(domain)  # after the copy succeeded
            self._dom(domain)["versions"][tag] = {"installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": manifest.get("version"),
                                                 "requirements": manifest.get("requirements", []), "source": cand["path"], "stored": stamp}
            recorded = True
            self.state.last_action = f"installed {domain} from {cand['path']} as {tag}"
            self._save_state()
            await self.hass.async_add_executor_job(_rmtree_under, self._aside_dir(domain, tag), self.versions_dir)
            was_running = domain == self.state.domain and self._dom(domain).get("running_tag") == tag
            if was_running:
                await self.hass.async_add_executor_job(self._ensure_deployed, domain, tag, True)
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
            if fresh and not recorded:
                await self.hass.async_add_executor_job(self._drop_unrecorded, domain, tag)
            elif stored and not recorded:
                await self.hass.async_add_executor_job(self._restore_aside, domain, tag)
            if registered and not recorded:
                # a dev-mode entry for a copy that never made it into the store
                data = jsonio.read_json(self.user_registry_file, {})
                if isinstance(data, dict) and isinstance(data.get("integrations"), dict) and data["integrations"].pop(domain, None) is not None:
                    write_json(self.user_registry_file, data, fsync=False)
                    self._registry_cache = None
            from .diagnostics import scrub_text  # diagnostics imports this module

            self.state.last_error = scrub_text(f"{type(err).__name__}: {err}")
            self._save_state()
            return {"ok": False, "error": self.state.last_error}
        finally:
            self.busy = False

    def _store_local(self, src: str, domain: str, tag: str, stamp: str | None = None) -> dict[str, Any]:
        """Blocking: a dev directory into versions/<domain>/<tag>, as _store_version does for a release (staging,
        the copy already there set aside).  Links are skipped, never followed: one to /config/secrets.yaml would
        copy the secret into the store and every backup, one to .. would recurse.  The release limits apply."""
        final = self._version_dir(domain, tag)
        staging = os.path.join(os.path.dirname(final), ".staging-" + os.path.basename(final))
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        files = size = 0
        try:
            for dirpath, dirs, names in os.walk(src):  # a linked directory is listed in dirs, never entered
                rel = os.path.relpath(dirpath, src)
                out = os.path.normpath(os.path.join(staging, rel))
                keep = []
                for n in sorted(dirs):
                    if n in DEV_COPY_IGNORE:
                        continue
                    if os.path.islink(os.path.join(dirpath, n)):
                        _LOGGER.warning("%s: symbolic link %s in the dev directory skipped", domain, os.path.join(rel, n))
                        continue
                    os.makedirs(os.path.join(out, n))
                    keep.append(n)
                dirs[:] = keep
                for n in sorted(names):
                    if n in DEV_COPY_IGNORE:
                        continue
                    path = os.path.join(dirpath, n)
                    st = os.lstat(path)
                    if not stat.S_ISREG(st.st_mode):
                        _LOGGER.warning("%s: %s in the dev directory skipped (a symbolic link or not a regular file)", domain, os.path.join(rel, n))
                        continue
                    files, size = files + 1, size + st.st_size
                    if files > UNPACK_MAX_MEMBERS:
                        raise RuntimeError(f"{src} has more than {UNPACK_MAX_MEMBERS} files")
                    if size > UNPACK_MAX_BYTES:
                        raise RuntimeError(f"{src} holds more than {UNPACK_MAX_BYTES // 1048576} MB")
                    shutil.copy2(path, os.path.join(out, n), follow_symlinks=False)
            manifest = self._manifest_at(staging)
            if not manifest or manifest.get("domain") != domain:
                raise RuntimeError("manifest.json missing or its domain differs")
            if (why := next((w for w in map(bad_requirement, manifest.get("requirements", [])) if w), None)):
                raise RuntimeError(f"{domain} {tag}: {why}")  # refused here as a release is, not only at start
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if stamp:
            self._write_stamp(staging, stamp)
        aside = self._aside_dir(domain, tag)
        shutil.rmtree(aside, ignore_errors=True)
        if os.path.isdir(final):
            os.replace(final, aside)
        os.replace(staging, final)
        return manifest

    def _deploy(self, domain: str, tag: str) -> None:
        """Copy the stored version into custom_components/<domain>; the files there stay until the new copy replaced them."""
        src = self._version_dir(domain, tag)
        target = self._component_dir(domain)
        tmp, aside = target + ".deploying", target + ".replaced"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(aside, ignore_errors=True)
        try:
            shutil.copytree(src, tmp, ignore=lambda d, names: [STORE_STAMP] if d == src and STORE_STAMP in names else [])  # store bookkeeping, not code
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)  # a half copy holds the domain's manifest: the loader could pick it
            raise
        had = os.path.isdir(target)
        if had:
            os.replace(target, aside)
        try:
            os.replace(tmp, target)
        except OSError:
            if had:
                os.replace(aside, target)
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        shutil.rmtree(aside, ignore_errors=True)

    def _tree_hash(self, domain: str) -> str | None:
        """Blocking: the deployed code's content (bytecode caches and the tag marker left out)."""
        import hashlib

        root = self._component_dir(domain)
        if not os.path.isdir(root):
            return None
        h = hashlib.sha256()
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name == ".hri-tag" or name.endswith(".pyc"):
                    continue
                path = os.path.join(dirpath, name)
                h.update(os.path.relpath(path, root).encode() + b"\0")
                try:
                    with open(path, "rb") as fh:
                        h.update(fh.read())
                except OSError:
                    return None
        return h.hexdigest()

    def _ensure_deployed(self, domain: str, tag: str, force: bool = False) -> bool:
        """Deploy unless custom_components/<domain> already holds this tag's
        files (compared by manifest version + the .hri-tag marker); ``force``: a reinstall
        of the running copy deploys anyway.  Returns True when it deployed."""
        want = self._manifest_at(self._version_dir(domain, tag)) or {}
        have = self.installed_manifest(domain) or {}
        marker = os.path.join(self._component_dir(domain), ".hri-tag")
        try:
            with open(marker, encoding="utf-8") as fh:
                have_tag = fh.read().strip()
        except OSError:
            have_tag = None
        # the tag alone is not enough: "local" or a branch name gets new code under
        # the same tag, so the marker also carries when that copy entered the store
        rec = ((self.state.installed.get(domain) or {}).get("versions") or {}).get(tag) or {}
        stamp = f"{tag}\n{rec.get('installed_at') or ''}".strip()
        if not force and have and have.get("version") == want.get("version") and have_tag == stamp:
            return False
        self._deploy(domain, tag)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(stamp)
        self.installed_manifest(domain)  # read here, in the executor: health() on the loop then finds it cached
        return True

    def _install_requirements(self, requirements: list[str], force: bool = False) -> list[str]:
        """pip only for requirements that are not satisfied (HA's
        install_package always spawns pip): a start with nothing new costs
        nothing.  ``force`` reinstalls everything (repair)."""
        failed = [r for r in dict.fromkeys(requirements) if bad_requirement(r)]  # never handed to pip or uv
        if failed:
            _LOGGER.error("requirements refused: %s", "; ".join(bad_requirement(r) or "" for r in failed))
        todo = [r for r in dict.fromkeys(requirements) if r not in failed and (force or not pkg_util.is_installed(r))]
        if todo and not _bind_popen():
            _LOGGER.warning("homeassistant.util.package.Popen is not subprocess.Popen: requirements install without a time limit")
        for req in todo:
            _pip_deadline.at = time.monotonic() + PIP_INSTALL_TIMEOUT_S  # covers install_package's own retry too
            try:
                ok = pkg_util.install_package(req, constraints=self.constraints, timeout=600)
            finally:
                _pip_deadline.at = None
            if not ok:
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
        failed), or is out of scope for this version but still in an installed
        library, dismissed once every patch applies again.  A patch retired by
        its headers ("skipped") is fine."""
        from homeassistant.components import persistent_notification as pn

        nid = f"integration_manager_patches_{domain}"
        bad = [r for r in results if str(r["status"]).strip().lower() not in ("applied", "already applied", "skipped", "pending")]
        if not bad:
            pn.dismiss(self.hass, nid)
            return
        lines = "\n".join(f"- {r['name']}: {r['status']}" for r in bad)
        pn.create(self.hass, f"{lines}\n\nA patch that does not fit is not applied: the integration runs without it, and Edit "
                  "and Check it on the Integration page to see what changed. One that says \"skipped, still applied\" is the "
                  "other way round — it is out of scope for this version, but the library it patched still carries it: "
                  "reinstall that distribution, or switch back and delete the patch.",
                  title=f"Patches of {domain} need attention", notification_id=nid)

    def dismiss_patch_notification(self, domain: str) -> None:
        from homeassistant.components import persistent_notification as pn

        pn.dismiss(self.hass, f"integration_manager_patches_{domain}")
