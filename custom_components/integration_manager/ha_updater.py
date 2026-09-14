"""Home Assistant version management for the manager UI.

HA runs from ``<config>/venv-<version>`` (see /app/entrypoint.py).  This
module only reads/writes ``<config>/integration_manager/ha.json`` and asks
PyPI which versions exist; the entrypoint does the installing on the next
restart, keeps the previous venv, and reports a failed install through
``last_error`` in the same file.
"""

from __future__ import annotations

import json

from jsonio import ha_vkey, write_json
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
_STABLE = re.compile(r"^\d{4}\.\d{1,2}\.\d+$")


class HaUpdater:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.file = hass.config.path("integration_manager", "ha.json")
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._releases: dict[str, str | None] = {}

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

    def _write(self, data: dict[str, Any]) -> None:
        write_json(self.file, data, fsync=False)  # called on the loop; the replace stays atomic

    def _installed_venvs(self) -> list[str]:
        out = []
        try:
            for name in os.listdir(self.hass.config.config_dir):
                if name.startswith("venv-") and name != "venv-current" and os.path.isfile(self.hass.config.path(name, ".ok")):
                    out.append(name[5:])
        except OSError:
            pass
        return sorted(out, key=_key)

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
            stable = sorted((v for v, files in data["releases"].items() if _STABLE.match(v) and files), key=_key)
            latest = stable[-1] if stable else None
            self._releases = {v: (files[0].get("requires_python") if files else None) for v, files in data["releases"].items()}
            info = {
                "latest_stable": latest,
                "latest_published": (data["releases"][latest][0]["upload_time"][:10] if latest else None),
                "requires_python": data["info"].get("requires_python"),
                "recent": stable[-8:],
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

        for b in backups if backups is not None else backupkit.list_backups(self.hass.config.config_dir):  # newest first
            if b.get("ha_version") and _key(b["ha_version"]) <= _key(version):
                return b
        return None

    def _config_backups(self, versions: list[str], current: str) -> dict[str, dict[str, Any]]:
        import backupkit

        backups = backupkit.list_backups(self.hass.config.config_dir)
        out = {}
        for v in versions:
            if _key(v) < _key(current) and (b := self.config_backup_for(v, backups)):
                out[v] = {k: b.get(k) for k in ("name", "created", "label", "ha_version")}
        return out

    def previous_version(self) -> str:
        prev = self._read().get("previous")
        if not prev:
            raise ValueError("no previous Home Assistant version recorded")
        return prev

    async def status(self, force: bool = False) -> dict[str, Any]:
        state, venvs = await self.hass.async_add_executor_job(lambda: (self._read(), self._installed_venvs()))
        avail = await self.available(force)
        current = homeassistant.const.__version__
        versions = sorted(set(avail.get("recent") or []) | set(venvs) | ({state["previous"]} if state.get("previous") else set()), key=_key)
        config_backups = await self.hass.async_add_executor_job(self._config_backups, versions, current)
        return {
            "current": current,
            "python": sys.version.split()[0],
            "venv": sys.prefix,
            "in_venv": sys.prefix != sys.base_prefix,
            "desired": state.get("desired") or current,
            "previous": state.get("previous"),
            "change": state.get("change"),  # {to, mode, backup, applied?} until that version booted
            "installed_venvs": venvs,
            "last_error": state.get("last_error", ""),
            "pending": bool(state.get("desired")) and state.get("desired") != current,
            "update_available": bool(avail.get("latest_stable")) and _key(avail["latest_stable"]) > _key(current),
            "config_backups": config_backups,  # downgrade target -> the backup its configuration can come from
            **avail,
        }

    async def validate(self, version: str) -> None:
        """Raise ValueError unless ``version`` exists on PyPI, is not older
        than the image's baseline and supports this Python."""
        version = version.strip()
        await self.available()
        if self._releases and version not in self._releases:
            raise ValueError(f"{version} is not a Home Assistant release on PyPI")
        floor = os.environ.get("HA_VERSION_DEFAULT")
        if floor and _key(version) < _key(floor):
            # older releases have no wheels for this image's Python: pip would
            # grind for minutes and the entrypoint would fall back
            raise ValueError(f"{version} is older than this image's baseline {floor}; not installable here")
        spec = self._releases.get(version)
        if spec:
            from packaging.specifiers import SpecifierSet

            py = ".".join(str(x) for x in sys.version_info[:3])
            if not SpecifierSet(spec).contains(py, prereleases=True):
                raise ValueError(f"{version} needs Python {spec}; this image has {py} (rebuild the image first)")

    async def async_set_desired(self, version: str) -> dict[str, Any]:
        """Like set_desired, but validated first (otherwise the entrypoint
        spends minutes in pip and falls back)."""
        await self.validate(version)
        return self.set_desired(version.strip())

    def set_desired(self, version: str, change: dict[str, Any] | None = None) -> dict[str, Any]:
        version = version.strip()
        if not _STABLE.match(version) and not re.match(r"^\d{4}\.\d{1,2}\.\d+(b\d+)?$", version):
            raise ValueError(f"not a Home Assistant version: {version!r}")
        state = self._read_for_update()
        state["desired"] = version
        state["last_error"] = ""
        if change:
            state["change"] = change
        else:
            state.pop("change", None)
        self._write(state)
        return state

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



_key = ha_vkey
