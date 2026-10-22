"""Home Assistant version management for the manager UI.

HA runs from ``<config>/venv-<version>`` (see /app/entrypoint.py).  This
module only reads/writes ``<config>/integration_manager/ha.json`` and asks
PyPI which versions exist; the entrypoint does the installing on the next
restart, keeps the previous venv, and reports a failed install through
``last_error`` in the same file.
"""

from __future__ import annotations

import json
import logging

from jsonio import ha_vkey, update_json
import os
import re
import sys
import time
from typing import Any

import homeassistant
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import events

_LOGGER = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/homeassistant/json"
CACHE_S = 600
_STABLE = re.compile(r"^\d{4}\.\d{1,2}\.\d+\Z")  # \Z: "$" also matches before a trailing newline
_ANY_DOMAIN = object()  # config_backup_for: made while any integration ran
_VERSION = re.compile(r"\d{4}\.\d{1,2}\.\d+(?:b\d+)?")  # a release or a beta, fullmatch: entrypoint.VERSION_RE
# How many of the newest stable releases the System page offers without being asked for more.  Home Assistant
# publishes a release every month and a handful of patches on top of it, and this image's Python only has wheels
# for the newest of them, so a list of every release ever published is a list of entries that answer "no": ten
# covers the last two or three months, which is the range a version change is really chosen from.  Not a
# setting: "show all versions" is one click away and loses nothing, so there is nothing here to get wrong.
RECENT_N = 10
SMOKE_CAUSE_MAX = 400  # characters of the error from the log a smoke-test verdict quotes
SMOKE_RECHECK_S = 60  # the smoke test after a version change looks again this often while it cannot judge


def _floor() -> str:
    """The oldest Home Assistant this image installs.  HA_VERSION_MIN, not
    HA_VERSION_DEFAULT: the version the image installs on a fresh volume is a
    choice (a recent, well-tested one), while the floor is a limit (the oldest
    release this code was measured on).  An image built before the two were
    split has only the one variable, and it stands for both."""
    return os.environ.get("HA_VERSION_MIN") or os.environ.get("HA_VERSION_DEFAULT") or ""


class HaUpdater:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.file = hass.config.path("integration_manager", "ha.json")
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._releases: dict[str, str | None] = {}
        self._stable: list[str] = []  # every stable release PyPI still offers, oldest first (what "show all" lists)
        self._smoke: dict[str, Any] | None = None  # the smoke test of this boot's version change (ha.json "smoke")
        self._smoke_installer: Any = None
        self._smoke_handle: Any = None
        self._smoke_waiting: float | None = None  # when the recorded entries were first seen still setting up
        self._smoke_rechecked = False  # an entry in setup_retry had its one more interval
        self._smoke_restore_waiting: float | None = None  # when the fallback first found a restore scheduled by hand
        self.left_version: str | None = None  # ha.json smoke_fallback.from: never offered as an update (async_start_smoke)

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.file, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _read_for_update(self) -> dict[str, Any]:
        """Like _read, but an unreadable file is not empty: writing over it would drop current and
        previous, and the entrypoint would then prune the venv a rollback needs."""
        try:
            with open(self.file, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            raise ValueError(f"ha.json is unreadable ({err}): restart the container, the entrypoint rebuilds it from the volume") from None
        if not isinstance(data, dict):
            raise ValueError("ha.json is unreadable: restart the container, the entrypoint rebuilds it from the volume")
        return data

    def _installed_venvs(self) -> list[str]:
        out = []
        try:
            for name in os.listdir(self.hass.config.config_dir):
                if name.startswith("venv-") and name != "venv-current" and self._venv_for_this_python(name[5:]):
                    out.append(name[5:])
        except OSError:
            pass
        return sorted(out, key=_key)

    def _venv_for_this_python(self, version: str) -> bool:
        """What entrypoint.venv_ok checks: installed completely, for the Python this image runs."""
        if not _VERSION.fullmatch(version):
            return False
        venv = self.hass.config.path(f"venv-{version}")
        site = os.path.join(venv, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant", "__init__.py")
        return all(os.path.isfile(p) for p in (os.path.join(venv, ".ok"), os.path.join(venv, "bin", "python"), site))

    def venv_removal_refusal(self, version: str) -> VenvRemovalRefused | None:
        """Blocking: why the venv of ``version`` must stay, or None.  Called under views._HA_CHANGE_LOCK with the
        installer busy, so no version change can be scheduled between this answer and the removal."""
        if not _VERSION.fullmatch(version):
            return VenvRemovalRefused(f"{version[:40]!r} is not a Home Assistant version", 400)
        try:
            why = self._venv_needed(version)
        except ValueError as err:  # ha.json unreadable: refused, as a write over it would be
            why = str(err)
        return VenvRemovalRefused(why) if why else None

    def _venv_needed(self, version: str) -> str | None:
        import backupkit

        state = self._read_for_update()
        if version in (HA_VERSION, state.get("current")):
            return f"Home Assistant {version} is the version running now"
        if state.get("desired") == version:
            return f"a switch to Home Assistant {version} is scheduled: cancel it first"
        change = state.get("change")
        if isinstance(change, dict) and change:
            return (f"a version change to Home Assistant {change.get('to')} is scheduled or has not finished booting: "
                    "remove the venv once it has booted, or cancel the change")
        if state.get("smoke"):
            return "the smoke test of the last version change has not finished: remove the venv once it has passed"
        if state.get("fallback_from"):
            return (f"the switch back from Home Assistant {state['fallback_from']} has not finished: remove the venv once "
                    "the version it went back to has booted")
        recovery = state.get("recovery")
        if isinstance(recovery, dict) and recovery:
            return f"a switch back to Home Assistant {recovery.get('for')} is still restoring its configuration"
        if backupkit.pending_for_version(self.hass.config.config_dir) == version:
            return f"a restore scheduled for Home Assistant {version} waits for it to boot: cancel it first"
        cfg = self.hass.config.config_dir
        path = os.path.join(cfg, f"venv-{version}")
        if os.path.islink(path):
            return f"venv-{version} is a link, not a venv this manager installed"
        real = os.path.realpath(path)
        if os.path.dirname(real) != os.path.realpath(cfg) or not os.path.isdir(real):
            return f"Home Assistant {version} is not installed here"
        in_use = {os.path.realpath(os.path.join(cfg, "venv-current")), os.path.realpath(sys.prefix)}
        if real in in_use:
            return f"venv-{version} is the venv running now"
        return None

    def remove_venv(self, version: str) -> tuple[int, int]:
        """Blocking: the venv of ``version`` renamed out of the way (a name the entrypoint's prune does not take for a
        version, and sweeps at every boot), then deleted.  Returns (bytes freed, bytes left for the next boot's
        sweep).  Check venv_removal_refusal first, under the same lock.  An integration preflight (a builder Check)
        may be reading this venv as a target's source at the same time (preflight._target_ha_root): its reads fail
        with an OSError and it says the target could not be read, a warning, never a wrong verdict."""
        import secrets
        import shutil

        cfg = self.hass.config.config_dir
        path = os.path.join(cfg, f"venv-{version}")
        before = _tree_bytes(path)
        aside = os.path.join(cfg, f".venv-removing-{version}-{secrets.token_hex(4)}")
        os.rename(path, aside)  # gone under its name at once: nothing can boot a half-deleted venv
        shutil.rmtree(aside, ignore_errors=True)
        left = _tree_bytes(aside) if os.path.lexists(aside) else 0
        return max(0, before - left), left

    async def available(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._cache and now - self._cache[0] < CACHE_S:
            return self._cache[1]
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(PYPI_URL, timeout=20) as resp:
                resp.raise_for_status()
                raw = await resp.read()
            data = await self.hass.async_add_executor_job(json.loads, raw)  # several MB: not on the loop
            # a release whose files are all yanked was withdrawn by Home Assistant: never offered, installed or the newest
            releases = {v: kept for v, files in data["releases"].items() if (kept := _installable_files(files))}
            stable = sorted((v for v in releases if _STABLE.match(v)), key=_key)
            latest = stable[-1] if stable else None
            self._releases = {v: files[0].get("requires_python") for v, files in releases.items()}
            self._stable = stable
            info = {
                "latest_stable": latest,
                "latest_published": (releases[latest][0]["upload_time"][:10] if latest else None),
                "requires_python": data["info"].get("requires_python"),
                "recent": stable[-RECENT_N:],
                "error": "",
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        except Exception as err:  # noqa: BLE001
            info = {"latest_stable": None, "recent": [], "error": f"{type(err).__name__}: {err}", "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._cache = (now, info)
        return info

    def config_backup_for(self, version: str, backups: list[dict[str, Any]] | None = None,
                          running: Any = _ANY_DOMAIN, skipped: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """The newest backup made on a Home Assistant that ``version`` can
        read (that version or an older one): the last moment the
        configuration existed in a format a downgrade to it understands.
        Home Assistant only migrates storage forward.  ``running``: the
        integration that runs now (None: none); only a backup made while it
        ran qualifies, since only its .storage is restored and the manager
        state stays.  ``skipped`` collects the backups that fitted the version
        but were made while another integration ran (or none), or say nothing."""
        import backupkit

        cfg = self.hass.config.config_dir if backups is None or running is not _ANY_DOMAIN else None
        listed = backups if backups is not None else backupkit.list_backups(cfg)  # newest first
        # a pre-restore copy holds whatever was on the volume at that moment (a half-migrated or crashed
        # configuration too): never the configuration a downgrade restores
        fits = [b for b in listed if b.get("ha_version") and _key(b["ha_version"]) <= _key(version)
                and "pre-restore" not in str(b.get("name") or "")]
        # this volume's own backups first: an uploaded one comes from somewhere else
        own = [b for b in fits if not str(b.get("name") or "").startswith("upload-")]
        for b in own + [b for b in fits if b not in own]:
            if running is _ANY_DOMAIN:
                return b
            # recorded in backup-info.json (describe); an older backup's state.json is opened, only while
            # nothing newer qualified.  An archive that does not say is never picked on its own: a wrong pick
            # restores another integration's configuration, a refusal only asks for rebuild or keep
            domain = b["domain"] if "domain" in b else backupkit.backup_domain(cfg, str(b.get("name") or ""))
            if domain is not backupkit.UNKNOWN_DOMAIN and domain == running:
                return b
            if skipped is not None:
                skipped.append({**b, "domain": None if domain is backupkit.UNKNOWN_DOMAIN else domain,
                                "domain_known": domain is not backupkit.UNKNOWN_DOMAIN})
        return None

    def running_domain(self) -> Any:
        """Blocking: the integration state.json on the volume names as running (the installer saves every change
        there); _ANY_DOMAIN when it cannot be read."""
        import backupkit

        path = self.hass.config.path(backupkit.MARKER)
        domain = backupkit._domain_of_state_file(path) if os.path.isfile(path) else backupkit.UNKNOWN_DOMAIN  # noqa: SLF001
        return _ANY_DOMAIN if domain is backupkit.UNKNOWN_DOMAIN else domain

    def _config_backups(self, versions: list[str], current: str) -> dict[str, dict[str, Any]]:
        import backupkit

        backups = backupkit.list_backups(self.hass.config.config_dir)
        running = self.running_domain()  # what a switch with restore picks (views.async_change_ha_version)
        out = {}
        for v in versions:
            if _key(v) < _key(current) and (b := self.config_backup_for(v, backups, running)):
                out[v] = {k: b.get(k) for k in ("name", "created", "label", "ha_version")}
        return out

    def previous_version(self) -> str:
        """Blocking: reads ha.json."""
        prev = self._read().get("previous")
        if not prev:
            raise ValueError("no previous Home Assistant version recorded")
        return prev

    async def status(self, force: bool = False, all_versions: bool = False) -> dict[str, Any]:
        state, venvs = await self.hass.async_add_executor_job(lambda: (self._read(), self._installed_venvs()))
        avail = await self.available(force)
        current = homeassistant.const.__version__
        # Nothing this box already has may fall off the offered list, however old it is: a venv on the volume
        # boots without downloading anything, the running version is how a scheduled change is cancelled, and
        # a scheduled or previous version is what a rollback goes back to.
        kept = set(venvs) | {current} | {v for v in (state.get("desired"), state.get("previous")) if v}
        offered = set(avail.get("recent") or []) | kept
        everything = set(self._stable) | kept
        versions = sorted(offered, key=_key)
        listed = sorted(everything, key=_key) if all_versions else versions
        # a downgrade plan only for what this image would take; the default list always keeps its own entries
        plan_for = [v for v in listed if v in offered or not self._image_refusal(v)]
        config_backups = await self.hass.async_add_executor_job(self._config_backups, plan_for, current)
        return {
            "versions": versions,  # what the page offers by default
            "recent_n": RECENT_N,
            "versions_total": len(everything),  # what "show all" would list
            **({"all_versions": listed} if all_versions else {}),
            "baseline": _floor(),  # anything older is refused: the page marks those itself
            "default_version": os.environ.get("HA_VERSION_DEFAULT") or "",  # what a fresh volume installs, which is not the floor
            "verdicts": self.verdicts(listed),
            "current": current,
            "python": sys.version.split()[0],
            "venv": sys.prefix,
            "in_venv": sys.prefix != sys.base_prefix,
            "desired": state.get("desired") or current,
            "previous": state.get("previous"),
            # a previous version whose venv was removed: a rollback to it installs it again from PyPI first
            "previous_installed": bool(state.get("previous")) and state.get("previous") in venvs,
            "change": state.get("change"),  # {to, mode, backup, applied?, smoke?} until that version booted
            "smoke": state.get("smoke"),  # the smoke test this version change still owes the integration
            "smoke_fallback": state.get("smoke_fallback"),  # the last automatic switch back after one failed
            "installed_venvs": venvs,
            "apt": state.get("apt"),  # what the entrypoint did with HRI_APT_PACKAGES at this boot (None: unset)
            "last_error": state.get("last_error", ""),
            "pending": bool(state.get("desired")) and state.get("desired") != current,
            # a version the smoke test left is not offered: switching to it again is chosen in the list, on purpose
            "update_available": bool(avail.get("latest_stable")) and _key(avail["latest_stable"]) > _key(current)
            and not (isinstance(state.get("smoke_fallback"), dict) and state["smoke_fallback"].get("from") == avail["latest_stable"]),
            "config_backups": config_backups,  # downgrade target -> the backup its configuration can come from
            **avail,
        }

    async def validate(self, version: str) -> None:
        """Raise ValueError unless ``version`` exists on PyPI (or is installed
        for this Python), is not older than the image's floor and supports
        this Python.  A venv already on the volume is an exception to the PyPI
        check only, never to the floor: the entrypoint boots what is there,
        but nothing may be scheduled below what was measured."""
        version = version.strip()
        avail = await self.available()
        if version not in self._releases:
            # without the release list neither that the version exists nor the Python it needs is known: the
            # entrypoint would spend minutes in pip, or install a version this image's Python cannot run.  A venv
            # already installed for this Python needs neither (the entrypoint boots it as it is), also when PyPI
            # no longer lists it or yanked it after it was installed
            if not await self.hass.async_add_executor_job(self._venv_for_this_python, version):
                if not self._releases:
                    raise ValueError(f"cannot check Home Assistant {version} against PyPI ({avail.get('error') or 'no release list'}): try again")
                raise ValueError(f"{version} is not a Home Assistant release on PyPI")
        if reason := self._image_refusal(version):
            raise ValueError(reason)

    def _image_refusal(self, version: str) -> str:
        """Why this image cannot install ``version``, decided from what is
        already in memory - no PyPI call, no pip run - or "" when nothing
        there objects.  What ``validate`` raises, and what the System page
        marks a version with before anyone clicks anything."""
        floor = _floor()
        if floor and _key(version) < _key(floor):
            # older releases have no wheels for this image's Python: pip would
            # grind for minutes and the entrypoint would fall back
            return f"{version} is older than this image's floor {floor}; not installable here"
        spec = self._releases.get(version)
        if spec:
            from packaging.specifiers import SpecifierSet

            py = ".".join(str(x) for x in sys.version_info[:3])
            if not SpecifierSet(spec).contains(py, prereleases=True):
                # reads like the dependency refusal in preflight.ha_version_report: what the version needs,
                # what this image has, what to do about it
                return (f"Home Assistant {version} needs Python {spec}; this image has {py} "
                        "(rebuild the image with that Python, or choose a newer Home Assistant version)")
        return ""

    def verdicts(self, versions: list[str]) -> dict[str, dict[str, Any]]:
        """What is known about each version without resolving anything, in the
        shape ``dependency_check`` answers in: the Python a release needs, and
        a report ``preflight`` still holds from this hour (its own check, or
        the one ``POST /api/ha/update`` ran before it refused).  A version
        older than the image's floor is left out on purpose: the answer
        carries ``baseline`` once and the page does that arithmetic itself,
        which keeps "show all versions" from repeating the same sentence a
        thousand times.  Rendering the System page must never start a pip run,
        so nothing here resolves anything."""
        from . import preflight

        floor = _floor()
        out: dict[str, dict[str, Any]] = {}
        for version in versions:
            if floor and _key(version) < _key(floor):
                continue
            if reason := self._image_refusal(version):
                out[version] = {"version": version, "ok": False, "checked": True, "blockers": [reason],
                                "warnings": [], "notes": [], "missing": []}
            elif (hit := preflight.ha_recent(version)) is not None:
                out[version] = hit
        return out

    async def dependency_check(self, version: str) -> dict[str, Any]:
        """The other half of ``validate``: that one refuses a version this
        image's Python cannot run (``requires_python``), this one a version
        whose pinned requirements have no wheel for it.  Both are about the
        image's Python, not about policy.  A venv already installed for this
        Python is not resolved again: the entrypoint boots it as it is."""
        from . import preflight

        version = version.strip()
        if await self.hass.async_add_executor_job(self._venv_for_this_python, version):
            return {"version": version, "ok": True, "checked": False, "blockers": [], "warnings": [],
                    "notes": [f"Home Assistant {version} is already installed for this Python: nothing to resolve"],
                    "missing": [], "python": sys.version.split()[0]}
        return await preflight.ha_version_report(self.hass, version)

    async def switch_blockers(self, installer: Any, version: str) -> tuple[list[str], list[str]]:
        """(blockers, warnings) of a switch to ``version`` beyond its requirements: the running integration reads
        an attribute of a Home Assistant module ``version`` does not have (the preflight's check, against that
        version's own source), its requirements do not resolve against ``version``'s pins
        (preflight.switch_requirement_checks), or the smoke test left ``version`` before.  Refused unless forced
        from the UI."""
        from . import preflight

        version = version.strip()
        blockers: list[str] = []
        warnings: list[str] = []
        left = (await self.hass.async_add_executor_job(self._read)).get("smoke_fallback")
        if isinstance(left, dict) and left.get("from") == version:
            blockers.append(f"Home Assistant {version} was left automatically on {left.get('at')}: {left.get('domain')} did not set up "
                            f"on it ({left.get('reason')})")
        domain = getattr(installer, "running", None)
        comp = installer.component_dir(domain) if domain else None
        if comp and await self.hass.async_add_executor_job(os.path.isdir, comp):
            hits, note = await self.hass.async_add_executor_job(
                preflight._ha_attribute_checks, comp, version, sys.executable, installer.config_dir)  # noqa: SLF001
            if hits:
                blockers.append(f"{domain} {installer.running_tag or ''} uses names Home Assistant {version} no longer has "
                                f"({'; '.join(hits[:3])}" + (f" (+{len(hits) - 3} more)" if len(hits) > 3 else "")
                                + f"): it fails with an AttributeError there; switch {domain} to a release that supports {version} first")
            if note:
                warnings.append(f"the attributes {domain} reads from Home Assistant's modules were not checked against {version}: {note}")
            req_blockers, req_warnings = await preflight.switch_requirement_checks(self.hass, installer, version)
            blockers += req_blockers
            warnings += req_warnings
        return blockers, warnings

    async def async_set_desired(self, version: str) -> dict[str, Any]:
        """Like set_desired, but validated first (otherwise the entrypoint
        spends minutes in pip and falls back), and written in the executor
        (set_desired fsyncs the file and its directory)."""
        await self.validate(version)
        return await self.hass.async_add_executor_job(self.set_desired, version.strip())

    def set_desired(self, version: str, change: dict[str, Any] | None = None) -> dict[str, Any]:
        """Blocking, for the executor.  Not only for the fsyncs: the per-file lock is shared with writers that run
        in executor threads (run.py's boot-ok mark, an async_set_desired), so on the loop it would wait for their
        fsync too, with nothing bounding it on a stalled disk, and every view, MQTT command and timer waits with it."""
        version = version.strip()
        if not _VERSION.fullmatch(version):
            raise ValueError(f"not a Home Assistant version: {version!r}")

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            state["desired"] = version
            state["last_error"] = ""
            state.pop("recovery", None)  # a new intention supersedes what a failed fallback left behind
            if change:
                state["change"] = change
            else:
                state.pop("change", None)
            return state

        # under the per-file lock: run.py (loop) and a restart (executor) update ha.json too; fsynced
        return update_json(self.file, apply, read=lambda _path: self._read_for_update())

    # ----- smoke test after a Home Assistant version change ---------------
    #
    # A new version that fails to boot three times is left by the entrypoint.  One that boots while the integration
    # that ran before no longer sets up on it (an import error, setup_error, not loaded) used to stay, with the
    # integration down.  A switch with "keep" records which of the integration's config entries were loaded
    # (views.async_change_ha_version, change["smoke"]); the first boot of the new version turns that into
    # ha.json "smoke", kept until the verdict so a restart in between does not lose it, and checks those entries
    # `smoke_test_s` later, the way the integration's own smoke test does after a switch.  Loaded (also degraded):
    # kept, a degraded one reported; one still waiting for its device (setup_retry) kept unconfirmed.  Not loaded:
    # with `auto_rollback` on, back to the version before the switch, with the configuration as it is now (saved
    # first) but for the stores the older version cannot read, from the switch's backup - once:
    # the version it goes back to is not smoke-tested, and ha.json "smoke_fallback" records what happened: a later
    # switch to the same version is refused unless forced (switch_blockers), and smoke-tested again.

    async def async_start_smoke(self, installer: Any, ha_state: Any) -> None:
        """Boot: the smoke test this boot owes a version change, if any, is scheduled."""
        state = ha_state if isinstance(ha_state, dict) else {}
        left = state.get("smoke_fallback") if isinstance(state.get("smoke_fallback"), dict) else {}
        self.left_version = str(left["from"]) if left.get("from") and left.get("from") != HA_VERSION else None
        rec = state.get("smoke") if isinstance(state.get("smoke"), dict) else None
        change = state.get("change") if isinstance(state.get("change"), dict) else {}
        planned = change.get("smoke") if isinstance(change.get("smoke"), dict) else None
        if rec is not None and rec.get("version") != HA_VERSION:
            rec = None  # another version boots: that switch's smoke test is moot
            await self.hass.async_add_executor_job(self._drop_smoke, None)
        if rec is None and planned is not None and change.get("to") == HA_VERSION and change.get("mode") == "keep":
            rec = {"version": HA_VERSION, "from": planned.get("from"), "domain": planned.get("domain"),
                   "entries": [str(e) for e in planned.get("entries") or []], "loaded": bool(planned.get("loaded")),
                   "backup": change.get("backup"), "parts": change.get("parts") or ["storage"],
                   "switched": change.get("at"), "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   # the environment builder's start of another integration version at this boot (views.async_change_ha_version)
                   **({"start": change["start"]} if isinstance(change.get("start"), dict) else {})}

            def record(st: dict[str, Any]) -> dict[str, Any] | None:
                ch = st.get("change") if isinstance(st.get("change"), dict) else {}
                if ch.get("to") != HA_VERSION:
                    return None  # run.py settled the change meanwhile: nothing to keep it from
                st["smoke"] = rec
                return st

            await self.hass.async_add_executor_job(lambda: update_json(self.file, record, read=lambda _p: self._read_for_update()))
        delay = installer.settings.int_("smoke_test_s", 0, 86400)
        if left.get("from") == HA_VERSION and left.get("domain") and (rec is None or rec.get("domain") != installer.running or delay <= 0):
            # the version a smoke test left boots with no smoke test (a forced switch with restore or clean start,
            # or the smoke test off): it is no longer marked as left once the integration's entries load on it
            domain = str(left["domain"])
            self.hass.loop.call_later(max(SMOKE_RECHECK_S, delay), lambda: self.hass.async_create_task(self.async_left_version_check(installer, domain)))
        if rec is None:
            return
        if rec.get("domain") != installer.running or delay <= 0:
            # another integration runs now, or the smoke test is off (as for an integration switch)
            await self.hass.async_add_executor_job(self._drop_smoke, None)
            return
        self._smoke, self._smoke_installer = rec, installer
        self._smoke_waiting, self._smoke_rechecked, self._smoke_restore_waiting = None, False, None
        events.emit("ha", f"Home Assistant {HA_VERSION}: smoke test of {rec['domain']} in {delay} s "
                    f"(loaded before the switch from {rec.get('from')})", version=HA_VERSION)
        self._schedule_smoke(delay)

    async def async_left_version_check(self, installer: Any, domain: str) -> None:
        """The boot of the version a smoke test left, not smoke-tested: ha.json "smoke_fallback" goes once
        ``domain``'s config entries load (as a passing smoke test clears it); entries still setting up are waited for."""
        if installer.running != domain:
            return
        entries = [e for e in self.hass.config_entries.async_entries(domain) if not e.disabled_by]
        if not self.hass.is_running or any(e.state.value == "setup_in_progress" for e in entries):
            self.hass.loop.call_later(SMOKE_RECHECK_S, lambda: self.hass.async_create_task(self.async_left_version_check(installer, domain)))
            return
        if not (all(e.state.value == "loaded" for e in entries) if entries else domain in self.hass.config.components):
            return

        def proven(st: dict[str, Any]) -> dict[str, Any] | None:
            fb = st.get("smoke_fallback")
            if not (isinstance(fb, dict) and fb.get("from") == HA_VERSION):
                return None
            st.pop("smoke_fallback", None)
            return st

        if await self.hass.async_add_executor_job(lambda: update_json(self.file, proven, read=lambda _p: self._read_for_update())):
            events.emit("ha", f"Home Assistant {HA_VERSION}: {domain} sets up on it; no longer marked as left by the smoke test", version=HA_VERSION)

    def _schedule_smoke(self, delay: float) -> None:
        if self._smoke_handle is not None:
            self._smoke_handle.cancel()
        self._smoke_handle = self.hass.loop.call_later(delay, lambda: self.hass.async_create_task(self.async_smoke_check()))

    def _drop_smoke(self, extra: Any) -> None:
        """Blocking: ha.json without "smoke" (and with ``extra(state)`` applied, when given)."""
        def apply(st: dict[str, Any]) -> dict[str, Any]:
            st.pop("smoke", None)
            if extra is not None:
                extra(st)
            return st

        update_json(self.file, apply, read=lambda _p: self._read_for_update())

    async def async_smoke_check(self) -> None:
        """The verdict on the integration's config entries that were loaded before the version change."""
        from homeassistant.components import persistent_notification as pn

        from .diagnostics import scrub_text
        from .views import _ha_change_lock_taken

        self._smoke_handle = None
        rec, installer = self._smoke, self._smoke_installer
        if rec is None or installer is None:
            return
        domain = str(rec.get("domain"))
        state = await self.hass.async_add_executor_job(self._read)
        current = state.get("smoke") if isinstance(state.get("smoke"), dict) else {}
        if current.get("version") != HA_VERSION or (state.get("desired") or HA_VERSION) != HA_VERSION or installer.running != domain:
            # another switch is scheduled, or another integration (or none) runs: this verdict would judge something else
            self._smoke = None
            await self.hass.async_add_executor_job(self._drop_smoke, None)
            return
        smoke_s = installer.settings.int_("smoke_test_s", 0, 86400)
        entries = [e for e in (self.hass.config_entries.async_get_entry(i) for i in rec.get("entries") or [])
                   if e is not None and not e.disabled_by]  # an entry removed or disabled since is not the version's doing
        setting_up = [e for e in entries if e.state.value == "setup_in_progress"]
        if setting_up:
            if self._smoke_waiting is None:
                self._smoke_waiting = time.monotonic()
        else:
            self._smoke_waiting = None
        waited = time.monotonic() - self._smoke_waiting if self._smoke_waiting is not None else 0.0
        busy = installer.busy or _ha_change_lock_taken()
        hung = bool(setting_up) and not busy and self.hass.is_running and waited > max(installer.SETUP_WAIT_S, smoke_s)
        if (busy or not self.hass.is_running or setting_up) and not hung:
            self._schedule_smoke(SMOKE_RECHECK_S)  # judging now would be a false failure
            return
        failed = [e for e in entries if e.state.value != "loaded"]
        yaml_down = not rec.get("entries") and rec.get("loaded") and domain not in self.hass.config.components
        retrying = [e for e in failed if e.state.value == "setup_retry"]
        if not hung and retrying and not self._smoke_rechecked:
            # ConfigEntryNotReady (a device or a broker not reachable yet): Home Assistant retries on its own
            self._smoke_rechecked = True
            delay = max(SMOKE_RECHECK_S, smoke_s)
            events.emit("ha", f"Home Assistant {HA_VERSION}: config entry '{retrying[0].title}' of {domain} is retrying its setup; "
                        f"checked again in {delay} s", version=HA_VERSION)
            self._schedule_smoke(delay)
            return
        self._smoke = None
        if not hung and failed and len(retrying) == len(failed):
            # still ConfigEntryNotReady after the extra interval: a device, a bridge or a broker that is not there.
            # It raises that on any version, so it says nothing against this one: kept, the verdict left open
            bad = retrying[0]
            note = (f"Home Assistant {HA_VERSION}: smoke test of {domain}: could not confirm: the device was not ready "
                    f"(config entry '{bad.title}' is still {bad.state.value}" + (f": {scrub_text(str(bad.reason))}" if bad.reason else "")
                    + f"); Home Assistant version kept, {domain} degraded until the entry sets up")
            _LOGGER.warning("%s", note)
            pn.async_create(self.hass, note + ".", title="Home Assistant version", notification_id="hri_ha_smoke")
            events.emit("ha", note, version=HA_VERSION, state="degraded")
            await self.hass.async_add_executor_job(self._drop_smoke, None)
            return
        if hung:
            reason = f"config entry '{setting_up[0].title}' still setting up after {int(waited)} s"
        elif failed:
            bad = next(e for e in failed if e.state.value != "setup_retry")  # what did not load for a reason of its own
            reason = f"config entry '{bad.title}' is {bad.state.value}" + (f": {scrub_text(str(bad.reason))}" if bad.reason else "")
        elif yaml_down:
            reason = f"{domain} is not loaded (no config entry; its YAML setup did not load it)"
        if not hung and (failed or yaml_down):
            # not_loaded carries no reason: an AttributeError at import is only in the log (homeassistant.loader)
            cause = await self.hass.async_add_executor_job(_logged_cause, domain, rec.get("switched") or rec.get("at") or "")
            if cause:
                reason += f"; logged: {scrub_text(cause)[:SMOKE_CAUSE_MAX]}"
        else:
            try:
                h = installer.health_source(grace=False) if installer.health_source else installer.health()
            except Exception as err:  # noqa: BLE001 - the entries are loaded: the version is kept whatever this says
                h = {"state": "unknown", "reason": f"{type(err).__name__}: {err}"}
            verdict = h.get("state") or "unknown"
            note = f"Home Assistant {HA_VERSION}: smoke test of {domain}: {verdict}" + (f" ({h.get('reason')})" if h.get("reason") else "")
            if verdict == "degraded":
                note += "; Home Assistant version kept: a degraded integration is not switched back automatically"
                pn.async_create(self.hass, note + ".", title="Home Assistant version", notification_id="hri_ha_smoke")
            events.emit("ha", note, version=HA_VERSION, state=verdict)

            def proven(st: dict[str, Any]) -> None:
                fb = st.get("smoke_fallback")
                if isinstance(fb, dict) and fb.get("from") == HA_VERSION:
                    st.pop("smoke_fallback", None)  # the version that was left sets the integration up now

            await self.hass.async_add_executor_job(self._drop_smoke, proven)
            return
        await self._smoke_fall_back(rec, installer, reason)

    async def _smoke_fall_back(self, rec: dict[str, Any], installer: Any, reason: str) -> None:
        """The integration did not set up on this version: back to the one before the switch (once), or say why not."""
        import backupkit
        from homeassistant.components import persistent_notification as pn

        from .views import _HA_CHANGE_LOCK, _ha_change_lock_taken

        domain, back, backup = str(rec.get("domain")), rec.get("from"), rec.get("backup")
        parts = rec.get("parts") or ["storage"]
        # The environment builder switched Home Assistant AND started another version of the integration at this boot
        # (its deferred start): the integration version that ran before goes back with Home Assistant, from the
        # switch's own backup, which was taken with it deployed.  The integration's smoke test of that start does not
        # roll back on its own (Installer.async_run_pending_start): this verdict is the one that acts, once.
        start = rec.get("start") if isinstance(rec.get("start"), dict) else {}
        from_tag, left_tag = start.get("from_tag"), start.get("tag")
        revert = bool(from_tag and left_tag and start.get("domain") == domain and installer.running == domain
                      and installer.running_tag == left_tag
                      and from_tag in ((installer.state.installed.get(domain) or {}).get("versions") or {}))
        failed = f"Home Assistant {HA_VERSION} started, but {domain}{f' {left_tag}' if revert else ''} did not set up on it ({reason})"
        why_not = ""
        if not installer.settings.bool_("auto_rollback"):
            why_not = "automatic rollback is off"
        elif not back or not _VERSION.fullmatch(str(back)) or back == HA_VERSION:
            why_not = "the version before the switch is not recorded"
        elif not await self.hass.async_add_executor_job(self._venv_for_this_python, back):
            why_not = f"Home Assistant {back} is no longer installed here"
        elif not backup:
            why_not = "the switch's backup is not recorded"
        elif installer.busy or _ha_change_lock_taken():
            self._smoke = rec  # an action or a version change is being prepared: judged again once it is done
            self._schedule_smoke(SMOKE_RECHECK_S)
            return
        elif await self.hass.async_add_executor_job(backupkit.pending, self.hass.config.config_dir):
            # a restore the user scheduled and has not restarted for: the fallback's own schedule would replace it
            if self._smoke_restore_waiting is None:
                self._smoke_restore_waiting = time.monotonic()
                events.emit("ha", f"{failed}; switching back to Home Assistant {back} waits: a configuration restore is scheduled "
                            f"and has not been applied (restart, or cancel it on Backup)", version=HA_VERSION)
            if time.monotonic() - self._smoke_restore_waiting <= installer.SETUP_WAIT_S:
                self._smoke = rec
                self._schedule_smoke(SMOKE_RECHECK_S)
                return
            why_not = f"a configuration restore is scheduled (still not applied after {installer.SETUP_WAIT_S // 60} min), and switching back would replace it"
        if why_not:
            msg = f"{failed}; not switched back: {why_not}"
            res: dict[str, Any] = {}
            if revert and installer.settings.bool_("auto_rollback"):
                # Home Assistant stays; the integration still goes back, by its own full rollback (the backup its start
                # took on this version), which its smoke test left to this verdict
                res = await installer.rollback_full(domain, rejected=True)
                msg += (f"; {domain} rolled back to {res.get('tag')} on Home Assistant {HA_VERSION} instead (its backup "
                        f"{res.get('restore')} restored at the restart)" if res.get("ok")
                        else f"; {domain} not rolled back either: {res.get('error')}")
            _LOGGER.error("%s", msg)
            events.emit("ha", msg, version=HA_VERSION, state="error")
            pn.async_create(self.hass, msg + ".", title="Home Assistant version", notification_id="hri_ha_smoke")
            await self.hass.async_add_executor_job(self._drop_smoke, None)
            if res.get("ok"):
                await installer.restart()
            return
        cfg = self.hass.config.config_dir
        async with _HA_CHANGE_LOCK:  # no version change is scheduled while this one is written
            saved: str | None = None
            installer.busy = True  # no install or start replaces files while the backups are taken
            try:
                # what this version ran with since the switch (the switch's backup may be days old): saved first,
                # and kept but for the stores the older version cannot read (backupkit.storage_for_version)
                saved = (await installer.async_backup(label=f"before-fallback-from-{HA_VERSION}"))["name"]
                if revert:
                    # the integration goes back too: the configuration both ran with before the builder's change, whole
                    # (.storage as that version left it, and its files), not merged with what the new one migrated
                    merged, restored = {"name": str(backup)}, []
                    parts = sorted({*parts, "storage", "custom_components"})
                else:
                    merged, restored = await self.hass.async_add_executor_job(
                        backupkit.storage_for_version, cfg, saved, str(backup), back, f"fallback-config-for-{back}")
                await self.hass.async_add_executor_job(
                    lambda: backupkit.schedule_restore(cfg, merged["name"], parts, for_version=back, force=True))
                if revert:
                    installer.revert_for_ha_fallback(domain, str(from_tag), str(left_tag), str(back))
            except Exception as err:  # noqa: BLE001
                msg = (f"{failed}; not switched back to Home Assistant {back}: its configuration could not be prepared from {backup} ({err})"
                       + (f"; the configuration as it is now is saved in backup {saved}" if saved else ""))
                _LOGGER.error("%s", msg)
                events.emit("ha", msg, version=HA_VERSION, state="error")
                pn.async_create(self.hass, msg + ".", title="Home Assistant version", notification_id="hri_ha_smoke")
                await self.hass.async_add_executor_job(self._drop_smoke, None)
                return
            finally:
                installer.busy = False
            backup = merged["name"]
            if revert:
                msg = (f"{failed}; switched back to Home Assistant {back} with {domain} {from_tag}, the version that ran before the "
                       f"environment builder's change, automatically. Both start with the configuration from the switch's backup "
                       f"{backup}; the configuration as it was on {HA_VERSION} is saved in backup {saved}. Switching to {HA_VERSION} "
                       "again is refused unless forced, and is smoke-tested again")
            else:
                msg = (f"{failed}; switched back to Home Assistant {back} automatically. The configuration as it was on {HA_VERSION} "
                       f"is saved in backup {saved}, and {back} starts with it, "
                       + (f"except {', '.join(restored)}, which {HA_VERSION} migrated to a format {back} cannot read (or removed): "
                          f"those come back from the switch's backup {rec.get('backup')}, and what changed in them since the switch is "
                          f"only in {saved}" if restored else f"unchanged: nothing in it needed the switch's backup {rec.get('backup')}")
                       + f". Switching to {HA_VERSION} again is refused unless forced, and is smoke-tested again")
            at = time.strftime("%Y-%m-%dT%H:%M:%S")

            def fall_back(st: dict[str, Any]) -> None:
                st["desired"] = back
                st["fallback_from"] = HA_VERSION  # its venv stays until the version it goes back to booted
                st["recovery"] = {"backup": backup, "from": HA_VERSION, "for": back, "parts": parts, "at": at}
                st["last_error"] = msg  # announced after the restart (async_announce_ha_error), as a crash fallback's
                st["smoke_fallback"] = {"from": HA_VERSION, "to": back, "domain": domain, "reason": reason, "at": at,
                                        "saved": saved, "restored": restored,
                                        **({"integration": {"from": left_tag, "to": from_tag}} if revert else {})}
                st.pop("change", None)

            await self.hass.async_add_executor_job(self._drop_smoke, fall_back)
        _LOGGER.error("%s", msg)
        events.emit("ha", msg, version=HA_VERSION, state="error", fallback=back)
        result = await installer.restart()
        if not result.get("ok"):
            _LOGGER.error("switching back to Home Assistant %s waits for the next restart: %s", back, result.get("error"))

    def cancel_config_change(self) -> list[str]:
        """Blocking: what a scheduled version change prepared for its boot (a
        restore of .storage, a clean start) goes when that change is replaced
        or cancelled; a restore scheduled by hand on System stays."""
        import backupkit

        from . import ha_import

        cfg = self.hass.config.config_dir
        dropped = []
        if backupkit.pending(cfg) and backupkit.pending_for_version(cfg):
            backupkit.cancel_restore(cfg)
            dropped.append("configuration restore")
        if ha_import.drop_rebuild(cfg):
            dropped.append("clean start")
        return dropped



def _installable_files(files: Any) -> list[dict[str, Any]]:
    return [f for f in files if isinstance(f, dict) and not f.get("yanked")] if isinstance(files, list) else []


_key = ha_vkey


class VenvRemovalRefused(ValueError):
    """Why a venv is not removed, with the HTTP status it answers: 400 for a request that names no version, 409 for a
    venv something still needs."""

    def __init__(self, reason: str, status: int = 409) -> None:
        super().__init__(reason)
        self.status = status


def _tree_bytes(path: str) -> int:
    size = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                size += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return size


def _logged_cause(domain: str, since: str) -> str:
    """Blocking: the newest error the process log holds about ``domain`` since ``since`` (a timestamp, the switch):
    its message and the last line of its traceback ("AttributeError: module ... has no attribute 'vol'"), or ""."""
    try:
        import logbuffer
    except ImportError:
        return ""
    handler = logbuffer.find()
    if handler is None:
        return ""
    needle = re.compile(rf"(?<![\w]){re.escape(domain)}(?![\w])")
    try:
        records, _ = handler.query(min_level=logging.ERROR, limit=1, keep=lambda r: str(r.get("ts", ""))[:19] >= since[:19]
                                   and bool(needle.search(f"{r.get('logger', '')} {r.get('message', '')}")))
    except Exception:  # noqa: BLE001 - the verdict stands without its cause
        return ""
    if not records:
        return ""
    rec = records[-1]
    lines = [ln.strip() for ln in str(rec.get("exc") or "").splitlines() if ln.strip()]
    return " ".join(x for x in (str(rec.get("message") or "").splitlines()[0] if rec.get("message") else "", lines[-1] if lines else "") if x)
