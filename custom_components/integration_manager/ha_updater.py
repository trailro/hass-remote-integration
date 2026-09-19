"""Home Assistant version management for the manager UI.

HA runs from ``<config>/venv-<version>`` (see /app/entrypoint.py).  This
module only reads/writes ``<config>/integration_manager/ha.json`` and asks
PyPI which versions exist; the entrypoint does the installing on the next
restart, keeps the previous venv, and reports a failed install through
``last_error`` in the same file.
"""

from __future__ import annotations

import json

from jsonio import ha_vkey, update_json
import os
import re
import sys
import time
from typing import Any

import homeassistant
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

PYPI_URL = "https://pypi.org/pypi/homeassistant/json"
CACHE_S = 600
_STABLE = re.compile(r"^\d{4}\.\d{1,2}\.\d+\Z")  # \Z: "$" also matches before a trailing newline
# How many of the newest stable releases the System page offers without being asked for more.  Home Assistant
# publishes a release every month and a handful of patches on top of it, and this image's Python only has wheels
# for the newest of them, so a list of every release ever published is a list of entries that answer "no": ten
# covers the last two or three months, which is the range a version change is really chosen from.  Not a
# setting: "show all versions" is one click away and loses nothing, so there is nothing here to get wrong.
RECENT_N = 10


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
                if name.startswith("venv-") and name != "venv-current" and os.path.isfile(self.hass.config.path(name, ".ok")):
                    out.append(name[5:])
        except OSError:
            pass
        return sorted(out, key=_key)

    def _venv_for_this_python(self, version: str) -> bool:
        """What entrypoint.venv_ok checks: installed completely, for the Python this image runs."""
        if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d+(b\d+)?", version):
            return False
        venv = self.hass.config.path(f"venv-{version}")
        site = os.path.join(venv, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant", "__init__.py")
        return all(os.path.isfile(p) for p in (os.path.join(venv, ".ok"), os.path.join(venv, "bin", "python"), site))

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

    def config_backup_for(self, version: str, backups: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """The newest backup made on a Home Assistant that ``version`` can
        read (that version or an older one): the last moment the
        configuration existed in a format a downgrade to it understands.
        Home Assistant only migrates storage forward."""
        import backupkit

        listed = backups if backups is not None else backupkit.list_backups(self.hass.config.config_dir)  # newest first
        # a pre-restore copy holds whatever was on the volume at that moment (a half-migrated or crashed
        # configuration too): never the configuration a downgrade restores
        fits = [b for b in listed if b.get("ha_version") and _key(b["ha_version"]) <= _key(version)
                and "pre-restore" not in str(b.get("name") or "")]
        # this volume's own backups first: an uploaded one comes from somewhere else
        own = [b for b in fits if not str(b.get("name") or "").startswith("upload-")]
        return (own or fits or [None])[0]

    def _config_backups(self, versions: list[str], current: str) -> dict[str, dict[str, Any]]:
        import backupkit

        backups = backupkit.list_backups(self.hass.config.config_dir)
        out = {}
        for v in versions:
            if _key(v) < _key(current) and (b := self.config_backup_for(v, backups)):
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
            "change": state.get("change"),  # {to, mode, backup, applied?} until that version booted
            "installed_venvs": venvs,
            "apt": state.get("apt"),  # what the entrypoint did with HRI_APT_PACKAGES at this boot (None: unset)
            "last_error": state.get("last_error", ""),
            "pending": bool(state.get("desired")) and state.get("desired") != current,
            "update_available": bool(avail.get("latest_stable")) and _key(avail["latest_stable"]) > _key(current),
            "config_backups": config_backups,  # downgrade target -> the backup its configuration can come from
            **avail,
        }

    async def validate(self, version: str) -> None:
        """Raise ValueError unless ``version`` exists on PyPI (or is installed
        for this Python), is not older than the image's baseline and supports
        this Python."""
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
        older than the image's baseline is left out on purpose: the answer
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
        if not _STABLE.match(version) and not re.match(r"^\d{4}\.\d{1,2}\.\d+(b\d+)?\Z", version):
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
