"""The manager as a device of the consuming Home Assistant, over MQTT.

``<base>/manager`` (retained JSON, refreshed with the health document every
60 s) carries what the manager knows beyond the integration's health:

* ``updates``: for the running integration (the newest stable GitHub release
  not in the store yet, from the release check), for Home Assistant in this
  container (PyPI) and for hass-remote-integration itself (its GitHub
  releases), each in the JSON form Home Assistant's MQTT ``update`` platform
  reads;
* ``resources``: resident memory, CPU share of the process, event-loop lag
  (mean and worst delay of a 1 s timer since the previous sample: an
  integration that blocks the loop shows up here), threads, open files and
  the volume's usage.

With ``manager_discovery`` (or entity discovery) the consuming HA gets a
device with those as entities (discovery.manager_device).  With
``manager_commands`` it can act through ``<base>/manager/cmd/<action>``
(never retained; each action accepts exactly one payload, see
discovery.MANAGER_ACTIONS):

* ``install_integration``: preflight, install and start the newest release
  (a start takes a backup, runs the smoke test and rolls back on its own,
  as from the UI), then a restart if the running code has to be replaced;
* ``install_home_assistant``: the newest stable Home Assistant (upgrades
  only, backup first, configuration kept), then a restart;
* ``restart``, ``backup``, ``check_updates``.

The outcome goes to ``<base>/manager/result`` (not retained), the MQTT
command history and the timeline.  ``GET /api/manager`` returns the
document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from jsonio import ha_vkey, read_json, vkey

from . import events, preflight
from .discovery import MANAGER_ACTIONS
from .http_util import ManagerView
from .memdiag import _proc_status

if TYPE_CHECKING:
    from .ha_updater import HaUpdater
    from .installer import Installer
    from .mqtt_publisher import MqttPublisher

_LOGGER = logging.getLogger(__name__)

MANAGER_REPO = "trailro/hass-remote-integration"
VERSION_CHECK_S = 12 * 3600
LAG_TICK_S = 1.0
RESOURCE_KEYS = ("memory_mb", "cpu_pct", "loop_lag_ms", "loop_lag_max_ms", "threads", "open_files", "volume_used_pct", "volume_free_gb")


def _manifest_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), "manifest.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "")
    except (OSError, ValueError):
        return ""


MANAGER_VERSION = _manifest_version()


def _update(installed: str | None, latest: str | None, title: str, url: str | None, key, in_progress: bool = False) -> dict[str, Any]:
    """One update entity's payload; {} (ignored by the consumer) when nothing is installed."""
    if not installed:
        return {}
    newer = bool(latest) and key(latest) > key(installed)
    doc: dict[str, Any] = {"installed_version": installed, "latest_version": latest if newer else installed, "title": title, "in_progress": in_progress}
    if newer and url:
        doc["release_url"] = url
    return doc


class LoopLag:
    """Mean and worst lateness of a 1 s timer on the event loop since the last take()."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._handle: asyncio.TimerHandle | None = None
        self._due = 0.0
        self._max = self._sum = 0.0
        self._n = 0

    def start(self) -> None:
        self._due = self.hass.loop.time() + LAG_TICK_S
        self._handle = self.hass.loop.call_at(self._due, self._tick)

    def _tick(self) -> None:
        now = self.hass.loop.time()
        lag = max(0.0, now - self._due)
        self._max = max(self._max, lag)
        self._sum += lag
        self._n += 1
        self._due = now + LAG_TICK_S
        self._handle = self.hass.loop.call_at(self._due, self._tick)

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def take(self) -> tuple[float | None, float | None]:
        if not self._n:
            return None, None
        out = (round(self._sum / self._n * 1000, 1), round(self._max * 1000, 1))
        self._max = self._sum = 0.0
        self._n = 0
        return out


class ManagerDevice:
    version = MANAGER_VERSION

    def __init__(self, hass: HomeAssistant, installer: Installer, updater: HaUpdater, publisher: MqttPublisher) -> None:
        self.hass = hass
        self.installer = installer
        self.updater = updater
        self.publisher = publisher
        self.resources: dict[str, Any] = dict.fromkeys(RESOURCE_KEYS)
        self.manager_latest: str | None = None
        self.last_action: dict[str, Any] | None = None
        self._ha_desired: str | None = None
        self._cpu_at: tuple[float, float] | None = None
        self._lag = LoopLag(hass)
        self._action_lock = asyncio.Lock()
        self._running: str | None = None
        self._unsub: list[Any] = []

    def start(self) -> None:
        self._lag.start()
        self._unsub.append(async_call_later(self.hass, 5, self._first_sample))  # the document has resources before the first health tick
        self._unsub.append(async_call_later(self.hass, 180, self._scheduled_check))
        self._unsub.append(async_track_time_interval(self.hass, self._scheduled_check, timedelta(seconds=VERSION_CHECK_S)))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)

    async def _on_stop(self, _event: Event) -> None:
        self._lag.stop()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    async def _first_sample(self, _now: Any) -> None:
        await self.async_sample()
        self.publisher.publish_manager()

    # ----- versions ------------------------------------------------------------

    async def _scheduled_check(self, _now: Any = None) -> None:
        await self.async_check_versions(force=False)

    async def async_check_versions(self, force: bool) -> None:
        """The newest hass-remote-integration release and the newest stable
        Home Assistant; a failed check keeps what was known."""
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(f"https://api.github.com/repos/{MANAGER_REPO}/releases/latest",
                                   headers=self.installer.settings.github_headers(), timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    tag = str((await resp.json()).get("tag_name") or "")
                    self.manager_latest = tag[1:] if tag.startswith(("v", "V")) else tag or None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("hass-remote-integration release check failed: %s", err)
        await self.updater.available(force=force)  # records its own error

    # ----- resources -----------------------------------------------------------

    async def async_sample(self) -> None:
        lag_avg, lag_max = self._lag.take()
        data = await self.hass.async_add_executor_job(self._sample_blocking)
        self.resources = {**data, "loop_lag_ms": lag_avg, "loop_lag_max_ms": lag_max}

    def _sample_blocking(self) -> dict[str, Any]:
        now, cpu = time.monotonic(), time.process_time()
        out: dict[str, Any] = dict.fromkeys(RESOURCE_KEYS)
        if self._cpu_at and now > self._cpu_at[0]:
            out["cpu_pct"] = round((cpu - self._cpu_at[1]) / (now - self._cpu_at[0]) * 100, 1)
        self._cpu_at = (now, cpu)
        proc = _proc_status()
        if "VmRSS" in proc:
            out["memory_mb"] = round(proc["VmRSS"] / 1024, 1)
        out["threads"] = threading.active_count()
        try:
            out["open_files"] = len(os.listdir("/proc/self/fd"))
        except OSError:
            pass
        try:
            du = shutil.disk_usage(self.hass.config.config_dir)
            out["volume_used_pct"] = round((du.total - du.free) / du.total * 100, 1)
            out["volume_free_gb"] = round(du.free / 1e9, 2)
        except (OSError, ZeroDivisionError):
            pass
        ha = read_json(self.hass.config.path("integration_manager", "ha.json"), {})
        self._ha_desired = ha.get("desired") if isinstance(ha, dict) else None
        return out

    # ----- document ------------------------------------------------------------

    def document(self) -> dict[str, Any]:
        inst = self.installer
        domain, tag = inst.running, inst.running_tag
        spec = inst.spec(domain) if domain else {}
        repo = spec.get("repo")
        integ_latest = inst.updates.get(domain) if domain else None
        ha_info = self.updater._cache[1] if self.updater._cache else {}  # noqa: SLF001 - what the last PyPI check found, no request here
        ha_latest = ha_info.get("latest_stable")
        return {
            "manager_version": self.version,
            "integration": domain,
            "integration_tag": tag,
            "ha_version": HA_VERSION,
            "updates": {
                "integration": _update(tag, integ_latest, spec.get("name") or domain or "", f"https://github.com/{repo}/releases/tag/{integ_latest}"
                                       if repo and integ_latest else None, vkey, self._running == "install_integration"),
                "home_assistant": _update(HA_VERSION, ha_latest, "Home Assistant (in the container)",
                                          f"https://github.com/home-assistant/core/releases/tag/{ha_latest}" if ha_latest else None, ha_vkey,
                                          self._running == "install_home_assistant" or bool(self._ha_desired and self._ha_desired != HA_VERSION)),
                "manager": _update(self.version, self.manager_latest, "hass-remote-integration",
                                   f"https://github.com/{MANAGER_REPO}/releases/tag/v{self.manager_latest}" if self.manager_latest else None, vkey),
            },
            "resources": self.resources,
            "patches": inst._patch_status(domain) or "none",  # noqa: SLF001
            "running_action": self._running,
            "last_action": self.last_action,
            "commands": self.publisher.config.manager_commands,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    # ----- actions -------------------------------------------------------------

    async def async_action(self, action: str, rec: dict[str, Any] | None = None) -> dict[str, Any]:
        if action not in MANAGER_ACTIONS:
            res: dict[str, Any] = {"ok": False, "error": f"unknown action {action!r}"}
        elif self._action_lock.locked():
            res = {"ok": False, "error": f"{self._running} is still running"}
        else:
            async with self._action_lock:
                self._running = action
                self.publisher.publish_manager()  # in_progress shows at once
                try:
                    res = await getattr(self, f"_do_{action}")()
                except (ValueError, OSError) as err:
                    res = {"ok": False, "error": str(err)}
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("manager action %s failed", action)
                    res = {"ok": False, "error": f"{type(err).__name__}: {err}"}
                finally:
                    self._running = None
        restart = bool(res.pop("restart", False))
        res = {"action": action, **res, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.last_action = res
        if rec is not None:
            self.publisher._finish(rec, "ok" if res.get("ok") else "failed", res.get("error"))  # noqa: SLF001
        self.publisher.publish_manager_result(res)
        self.publisher.publish_manager()
        events.emit("mqtt", f"manager action {action} from MQTT: "
                    + (("ok" + (f", {res['note']}" if res.get("note") else "") + ("; restarting" if restart else "")) if res.get("ok") else f"failed: {res.get('error')}"),
                    action=action)
        if restart:
            await self.installer.restart()
        return res

    async def _do_install_integration(self) -> dict[str, Any]:
        inst = self.installer
        domain = inst.running
        if not domain:
            raise ValueError("no integration is running")
        tag = inst.updates.get(domain)
        if not tag:
            raise ValueError(f"no newer release of {domain} is known: check for updates first")
        async with preflight.LOCK:
            report = await preflight.run(self.hass, inst, domain, tag)
        if not report["ok"]:
            raise ValueError(f"preflight of {domain} {tag} blocked: {'; '.join(report['blockers'])}")
        res = await inst.install(tag, domain=domain)
        if not res.get("ok"):
            raise ValueError(f"install of {domain} {tag} failed: {res.get('error')}")
        res = await inst.start(domain, tag)
        if not res.get("ok"):
            raise ValueError(f"start of {domain} {tag} failed: {res.get('error')}")
        return {"ok": True, "note": f"{domain} {tag} started", "restart": bool(res.get("restart_required"))}

    async def _do_install_home_assistant(self) -> dict[str, Any]:
        from .views import async_change_ha_version

        target = (await self.updater.available()).get("latest_stable")
        if not target or ha_vkey(target) <= ha_vkey(HA_VERSION):
            raise ValueError(f"Home Assistant {HA_VERSION} is the newest stable version")
        await self.updater.validate(target)
        result = await async_change_ha_version(self.installer, self.updater, target, "keep", "mqtt")
        return {"ok": True, "note": f"Home Assistant {target}, backup {result['backup']}", "restart": True}

    async def _do_restart(self) -> dict[str, Any]:
        if self.installer.busy:
            raise ValueError("an install/start is running")
        return {"ok": True, "restart": True}

    async def _do_backup(self) -> dict[str, Any]:
        import backupkit

        if self.installer.busy:
            raise ValueError("an install/start is running")
        rec = await self.installer.async_backup("mqtt")
        await self.hass.async_add_executor_job(backupkit.prune, self.installer.config_dir, self.installer.settings.backup_keep,
                                               self.installer.protected_backups() | {rec["name"]})
        return {"ok": True, "note": f"backup {rec['name']}"}

    async def _do_check_updates(self) -> dict[str, Any]:
        found = await self.installer.check_updates(force=True)
        await self.async_check_versions(force=True)
        return {"ok": True, "note": ", ".join(f"{d} {t}" for d, t in found.items()) or "integration up to date"}


class ManagerStatusView(ManagerView):
    url = "/api/manager"

    def __init__(self, device: ManagerDevice) -> None:
        self.device = device

    async def get(self, request: web.Request) -> web.Response:
        return self.json(self.device.document())
