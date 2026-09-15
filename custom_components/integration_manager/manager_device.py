"""The manager as a device of the consuming Home Assistant, over MQTT.

``<base>/manager`` (retained JSON, refreshed with the health document every
60 s) carries what the manager knows beyond the integration's health:

* ``updates``: for the running integration (the newest of the release
  check's result and the versions already in the store), for Home Assistant
  in this container (PyPI) and for hass-remote-integration itself (its
  GitHub releases), each in the JSON form Home Assistant's MQTT ``update``
  platform reads; a failed check keeps what was known, a dev build
  (``local``) never shows an update;
* ``resources``: resident memory, CPU share of the process, event-loop lag
  (mean and worst delay of a 1 s timer since the previous sample: an
  integration that blocks the loop shows up here), threads, open files and
  the volume's usage.

With ``manager_discovery`` (or entity discovery) the consuming HA gets a
device with those as entities (discovery.manager_device).  With
``manager_commands`` it can act through ``<base>/manager/cmd/<action>``
(never retained; each action accepts exactly one payload, see
discovery.MANAGER_ACTIONS):

* ``install_integration``: preflight, install (unless that version is in
  the store already) and start the newest release (a start takes a backup,
  runs the smoke test and rolls back on its own, and MQTT follows it, as
  from the UI), then a restart if the running code has to be replaced;
* ``install_home_assistant``: the newest stable Home Assistant (upgrades
  only, backup first, configuration kept), then a restart;
* ``restart``, ``backup`` (at most every 10 min), ``check_updates`` (every 5 min).

Every sample also goes to a resource history (one a minute, kept for
``resource_history_h`` hours, 48 by default and at most 120, saved on the
volume): ``GET /api/manager/history`` and the Overview.  Memory that keeps
growing over hours, or an event loop held for 500 ms or more in several
minutes of the last hour, raises a notification.

The outcome goes to ``<base>/manager/result`` (not retained), the MQTT
command history and the timeline.  ``GET /api/manager`` returns the
document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from collections import deque
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from jsonio import ha_vkey, read_json, vkey, write_json

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
MIN_INTERVAL_S = {"backup": 600, "check_updates": 300}  # a flood of presses must not rotate every backup away
LAG_TICK_S = 1.0
HISTORY_FILE = "resource_history.json"
LATEST_FILE = "latest_versions.json"
STABLE_TAG = re.compile(r"[vV]?\d+(?:\.\d+){0,3}")  # 1.2, v1.2.3; not 1.2.0b1, 1.3.0rc1, feature/x or a SHA  # last known releases: update entities do not flap after a restart or a restore
HISTORY_SAVE_S = 600
HISTORY_POINTS = 360         # at most this many points per series in an answer
LEAK_MIN_SPAN_H = 6          # memory growth is judged over at least this much history
LEAK_MIN_MIB = 50
LAG_ALERT_MS = 500
LAG_ALERT_MINUTES = 5        # held this long in at least this many of the last 60 samples
NOTIFY_MEMORY = "integration_manager_resources_memory"
NOTIFY_LAG = "integration_manager_resources_lag"
RESOURCE_KEYS = ("memory_mb", "cpu_pct", "loop_lag_ms", "loop_lag_max_ms", "threads", "open_files", "volume_used_pct", "volume_free_gb")


def _manifest_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), "manifest.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "")
    except (OSError, ValueError):
        return ""


MANAGER_VERSION = _manifest_version()


def _mean(rows: list[list[Any]], i: int) -> float | None:
    vals = [r[i] for r in rows if r[i] is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _worst(rows: list[list[Any]], i: int) -> float | None:
    vals = [r[i] for r in rows if r[i] is not None]
    return max(vals) if vals else None


def memory_trend(rows: list[list[Any]]) -> dict[str, Any]:
    """Least-squares slope of resident memory (MiB/h), the mean of the first
    and of the last tenth of the window, and the share of half hours whose
    mean is above the previous one (steady growth rises in most of them)."""
    pts = [(r[0], r[1]) for r in rows if r[1] is not None]
    if len(pts) < 30:
        return {}
    t0 = pts[0][0]
    xs = [(t - t0) / 3600 for t, _ in pts]
    ys = [m for _, m in pts]
    n = len(pts)
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.0
    k = max(1, n // 10)
    halves: dict[int, list[float]] = {}
    for t, m in pts:
        halves.setdefault(int((t - t0) // 1800), []).append(m)
    means = [sum(v) / len(v) for _, v in sorted(halves.items())]
    rising = sum(1 for a, b in zip(means, means[1:]) if b > a) / (len(means) - 1) if len(means) > 1 else 0.0
    return {"span_h": round(xs[-1], 1), "memory_mib_per_h": round(slope, 2), "memory_start_mb": round(sum(ys[:k]) / k, 1),
            "memory_end_mb": round(sum(ys[-k:]) / k, 1), "rising_share": round(rising, 2)}


def _update(installed: str | None, latest: str | None, title: str, url: str | None, key, in_progress: bool = False) -> dict[str, Any]:
    """One update entity's payload; {} (ignored by the consumer) when nothing is installed."""
    if not installed:
        return {}
    newer = bool(latest) and installed != "local" and key(latest) > key(installed)  # a dev build is not "older"
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
        config_dir = getattr(installer, "config_dir", None)
        self._latest_file = os.path.join(config_dir, "integration_manager", LATEST_FILE) if config_dir else None
        known = read_json(self._latest_file, {}) if self._latest_file else {}
        known = known if isinstance(known, dict) else {}
        self._latest_saved = dict(known)
        self.manager_latest: str | None = known.get("manager")
        self.manager_tag: str | None = known.get("manager_tag")
        self._ha_latest: str | None = known.get("home_assistant")
        self._last_run: dict[str, float] = {}
        self.last_action: dict[str, Any] | None = None
        self._ha_desired: str | None = None
        self._cpu_at: tuple[float, float] | None = None
        self._lag = LoopLag(hass)
        self._action_lock = asyncio.Lock()
        self._running: str | None = None
        self._unsub: list[Any] = []
        self._history: deque[list[Any]] = deque()  # [epoch s, memory, cpu, lag mean, lag max, volume used %]
        self._history_loaded = False
        self._history_saved = time.time()
        self._alert_memory = self._alert_lag = False

    def start(self) -> None:
        self._lag.start()
        self._unsub.append(async_call_later(self.hass, 5, self._first_sample))  # the document has resources before the first health tick
        self._unsub.append(async_call_later(self.hass, 180, self._scheduled_check))
        self._unsub.append(async_track_time_interval(self.hass, self._scheduled_check, timedelta(seconds=VERSION_CHECK_S)))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)

    async def _on_stop(self, _event: Event) -> None:
        if self._history:
            await self._save_history(time.time())
        self._lag.stop()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    def _remember_latest(self) -> None:
        known = {"manager": self.manager_latest, "manager_tag": self.manager_tag, "home_assistant": self._ha_latest}
        known = {k: v for k, v in known.items() if v}
        if known != self._latest_saved and self._latest_file:
            self._latest_saved = known
            self.hass.async_add_executor_job(write_json, self._latest_file, known)

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
                    if tag:
                        self.manager_tag = tag
                        self.manager_latest = tag[1:] if tag.startswith(("v", "V")) else tag
                        self._remember_latest()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("hass-remote-integration release check failed: %s", err)
        await self.updater.available(force=force)  # records its own error

    # ----- resources -----------------------------------------------------------

    async def async_sample(self) -> None:
        lag_avg, lag_max = self._lag.take()
        data = await self.hass.async_add_executor_job(self._sample_blocking)
        self.resources = {**data, "loop_lag_ms": lag_avg, "loop_lag_max_ms": lag_max}
        await self._record(time.time())

    # ----- history -------------------------------------------------------------

    def history_hours(self) -> int:
        return self.installer.settings.int_("resource_history_h", 1, 120)

    def _history_path(self) -> str:
        return self.hass.config.path("integration_manager", HISTORY_FILE)

    async def _record(self, now: float) -> None:
        if not self._history_loaded:
            self._history_loaded = True
            saved = await self.hass.async_add_executor_job(read_json, self._history_path(), {})
            for row in (saved.get("rows") if isinstance(saved, dict) else None) or []:
                if isinstance(row, list) and len(row) == 6 and isinstance(row[0], (int, float)) and row[0] < now:
                    self._history.append(row)
        r = self.resources
        self._history.append([int(now), r.get("memory_mb"), r.get("cpu_pct"), r.get("loop_lag_ms"), r.get("loop_lag_max_ms"), r.get("volume_used_pct")])
        cutoff = now - self.history_hours() * 3600
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()
        self._check_resources(now)
        if now - self._history_saved >= HISTORY_SAVE_S:
            await self._save_history(now)

    async def _save_history(self, now: float) -> None:
        self._history_saved = now
        rows = list(self._history)
        try:
            await self.hass.async_add_executor_job(lambda: write_json(self._history_path(), {"rows": rows}, fsync=False))
        except OSError as err:
            _LOGGER.warning("resource history not saved: %s", err)

    def history(self, hours: int) -> dict[str, Any]:
        """The last ``hours`` (capped by the retention), averaged into at most
        HISTORY_POINTS points; the loop lag keeps each bucket's worst."""
        hours = min(max(1, hours), self.history_hours())
        cutoff = time.time() - hours * 3600
        rows = [r for r in self._history if r[0] >= cutoff]
        size = max(1, math.ceil(len(rows) / HISTORY_POINTS))
        points = []
        for i in range(0, len(rows), size):
            chunk = rows[i:i + size]
            points.append([chunk[-1][0], _mean(chunk, 1), _mean(chunk, 2), _mean(chunk, 3), _worst(chunk, 4), chunk[-1][5]])
        return {"retention_h": self.history_hours(), "hours": hours, "samples": len(rows), "sample_s": 60,
                "fields": ["t", "memory_mb", "cpu_pct", "loop_lag_ms", "loop_lag_max_ms", "volume_used_pct"],
                "rows": points, "trend": memory_trend(rows)}

    def _check_resources(self, now: float) -> None:
        from homeassistant.components import persistent_notification as pn

        trend = memory_trend(list(self._history))
        leak = bool(trend) and trend["span_h"] >= LEAK_MIN_SPAN_H and trend["memory_mib_per_h"] > 0 and trend["rising_share"] >= 0.75 \
            and trend["memory_end_mb"] - trend["memory_start_mb"] >= max(LEAK_MIN_MIB, 0.25 * trend["memory_start_mb"])
        if leak and not self._alert_memory:
            pn.async_create(self.hass, f"Resident memory grew from {trend['memory_start_mb']:.0f} to {trend['memory_end_mb']:.0f} MiB over the last "
                            f"{trend['span_h']:.0f} h ({trend['memory_mib_per_h']:+.1f} MiB/h), rising in most half hours. A leak in the integration "
                            "or one of its libraries is likely: the Overview shows the history, /api/diag/memory what the process holds.",
                            title="Memory keeps growing", notification_id=NOTIFY_MEMORY)
        elif not leak and self._alert_memory:
            pn.async_dismiss(self.hass, NOTIFY_MEMORY)
        self._alert_memory = leak
        recent = [r[4] for r in self._history if r[0] >= now - 3600 and r[4] is not None]
        held = [v for v in recent if v >= LAG_ALERT_MS]
        lag = len(held) >= LAG_ALERT_MINUTES
        if lag and not self._alert_lag:
            pn.async_create(self.hass, f"The event loop was held for {LAG_ALERT_MS} ms or longer in {len(held)} of the last {len(recent)} minutes "
                            f"(worst {max(held):.0f} ms). Something runs blocking code on the loop, usually the integration or one of its "
                            "libraries; the Logs page may show \"Detected blocking call\".", title="Event loop blocked", notification_id=NOTIFY_LAG)
        elif not lag and self._alert_lag:
            pn.async_dismiss(self.hass, NOTIFY_LAG)
        self._alert_lag = lag

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

    def integration_latest(self) -> str | None:
        """The newest stable version of the running integration known here:
        the release check's result or a version already in the store (one
        installed but not started, or rolled back from)."""
        inst = self.installer
        domain = inst.running
        if not domain:
            return None
        # store tags count only when they are plain release numbers: a beta, a branch or a commit kept
        # for testing must never be what the update button installs (the release check is stable-only)
        known = [t for t in (inst.state.installed.get(domain) or {}).get("versions", {}) if STABLE_TAG.fullmatch(t)]
        if inst.updates.get(domain):
            known.append(inst.updates[domain])
        return max(known, key=vkey) if known else None

    def document(self) -> dict[str, Any]:
        inst = self.installer
        domain, tag = inst.running, inst.running_tag
        spec = inst.spec(domain) if domain else {}
        repo = spec.get("repo")
        integ_latest = self.integration_latest()
        ha_info = self.updater._cache[1] if self.updater._cache else {}  # noqa: SLF001 - what the last PyPI check found, no request here
        if ha_info.get("latest_stable"):
            if ha_info["latest_stable"] != self._ha_latest:
                self._ha_latest = ha_info["latest_stable"]  # a failed check (latest_stable None) keeps what was known
                self._remember_latest()
        ha_latest = self._ha_latest
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
                                          self._running == "install_home_assistant"
                                          or bool(self._ha_desired and ha_vkey(self._ha_desired) > ha_vkey(HA_VERSION))),
                "manager": _update(self.version, self.manager_latest, "hass-remote-integration",
                                   f"https://github.com/{MANAGER_REPO}/releases/tag/{self.manager_tag}" if self.manager_tag else None, vkey),
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
        elif (wait := MIN_INTERVAL_S.get(action, 0) - (time.monotonic() - self._last_run.get(action, -1e9))) > 0:
            res = {"ok": False, "error": f"{action} ran moments ago: try again in {int(wait) + 1} s"}
        else:
            async with self._action_lock:
                self._running = action
                self._last_run[action] = time.monotonic()
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
        started = res.pop("started", None)
        res = {"action": action, **res, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.last_action = res
        if rec is not None:
            self.publisher._finish(rec, "ok" if res.get("ok") else "failed", res.get("error"))  # noqa: SLF001
        # outcome first: the reconnect after a start and the restart would drop it
        await self.publisher.async_publish_manager_result(res)
        if started is not None:
            await self.publisher.async_after_start(started)
        events.emit("mqtt", f"manager action {action} from MQTT: "
                    + (("ok" + (f", {res['note']}" if res.get("note") else "") + ("; restarting" if restart else "")) if res.get("ok") else f"failed: {res.get('error')}"),
                    action=action)
        if restart:
            for _ in range(600):  # an install or start clicked meanwhile finishes first (at most 5 min)
                if not self.installer.busy:
                    break
                await asyncio.sleep(0.5)
            await self.installer.restart()
        return res

    async def _do_install_integration(self) -> dict[str, Any]:
        inst = self.installer
        domain = inst.running
        if not domain:
            raise ValueError("no integration is running")
        running = inst.running_tag
        tag = self.integration_latest()
        if running == inst.LOCAL_TAG:
            raise ValueError(f"{domain} runs a dev build: install releases from the UI")
        if not tag or (running and vkey(tag) <= vkey(running)):
            raise ValueError(f"no newer release of {domain} is known: check for updates first")
        async with preflight.LOCK:
            report = await preflight.run(self.hass, inst, domain, tag)
        if not report["ok"]:
            raise ValueError(f"preflight of {domain} {tag} blocked: {'; '.join(report['blockers'])}")
        if tag not in (inst.state.installed.get(domain) or {}).get("versions", {}):
            res = await inst.install(tag, domain=domain)
            if not res.get("ok"):
                raise ValueError(f"install of {domain} {tag} failed: {res.get('error')}")
        res = await inst.start(domain, tag)
        if not res.get("ok"):
            raise ValueError(f"start of {domain} {tag} failed: {res.get('error')}")
        return {"ok": True, "note": f"{domain} {tag} started", "restart": bool(res.get("restart_required")), "started": res}

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

        rec = await self.installer.async_backup_exclusive("mqtt")
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


class ManagerHistoryView(ManagerView):
    url = "/api/manager/history"

    def __init__(self, device: ManagerDevice) -> None:
        self.device = device

    async def get(self, request: web.Request) -> web.Response:
        try:
            hours = int(request.query.get("hours") or self.device.history_hours())
        except ValueError:
            hours = self.device.history_hours()
        return self.json(self.device.history(hours))
