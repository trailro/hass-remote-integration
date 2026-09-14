"""Publish every entity of this headless HA to MQTT, with full metadata.

Topic layout (retained JSON unless noted):
  <base>/status                                  "online" | "offline" (LWT)
  <base>/<integration>/<domain>/<object_id>      one document per entity
  <base>/<integration>/event_stream/<object_id>  event entities, NOT retained
  <base>/services/<domain>                       service catalog per domain
  <base>/cmd/<domain>/<object_id>/<field>        entity commands (subscribed)
  <base>/call/<domain>/<service>                 any service call, JSON payload (subscribed)
  <base>/result/<domain>/<service>               call outcome, NOT retained
  <base>/health                                  health verdict of the running integration
  <base>/manager                                 versions, updates, resources (manager_device.py)
  <base>/manager/cmd/<action>                    manager actions, with manager_commands (subscribed)
  <base>/manager/result                          action outcome, NOT retained

The document carries the live state, all attributes, timestamps and the
registry metadata (unique_id, names, device_class, unit, icon, category,
area, device block) plus ``integration`` = the entity's platform, i.e. the
integration's domain.  Removed entities get an empty retained payload so the topic
is cleared on the broker.

paho-mqtt is used directly (no HA mqtt integration) so the container stays
minimal; publishing is thread-safe, reconnects are paho's job, and a
full republish runs on every (re)connect and hourly; the periodic pass
only re-sends documents whose content changed.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import re
import math
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

import paho.mqtt.client as mqtt

from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import (
    EVENT_COMPONENT_LOADED,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_SERVICE_REGISTERED,
    EVENT_SERVICE_REMOVED,
    EVENT_STATE_CHANGED,
)
from homeassistant.core import CoreState, Event, HomeAssistant, State, SupportsResponse, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM, async_get_platforms
from homeassistant.const import __version__ as ha_version_str
from homeassistant.helpers.event import async_track_time_interval

from . import discovery as disc
from . import events
from jsonio import read_json, write_json

from .mqtt_rules import MqttRules
from .services_catalog import service_rows

_LOGGER = logging.getLogger(__name__)
HEALTH_INTERVAL_S = 60
REPUBLISH_BATCH = 200          # documents per batch before yielding to the event loop
REPUBLISH_BATCH_PAUSE_S = 0.02
HEALTH_GRACE_S = 900  # after a (re)start, at most this long before unavailable / silent entities count
HEALTH_STALE_S = 900  # no state written by the integration's entities (last_reported, value changed or not) for this long = degraded

CONFIG_FILE = "integration_manager/mqtt.json"
# A generic service call always answers: a service that blocks (e.g. an RF
# request that cannot be sent in read-only mode) is reported as a timeout.
CALL_TIMEOUT_S = int(os.environ.get("HRI_CALL_TIMEOUT", "60"))
HISTORY_MAX = 200      # commands and calls remembered (in memory)
DEDUP_WINDOW_S = 300   # a call repeating an _id seen this recently is answered from history, not run again
# Never callable over MQTT (anyone with broker credentials could otherwise
# stop this instance or run arbitrary commands); the catalog hides them too.
CALL_DENY_DOMAINS = frozenset({"homeassistant", "shell_command", "python_script", "hassio", "integration_manager"})


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = "mosquitto"
    port: int = 1883
    username: str = ""
    password: str = ""
    force_base_topic: bool = False  # connect even if foreign retained data sits under the base topic
    republish_interval_s: int = 300          # incremental pass: only documents whose content changed
    full_republish_interval_min: int = 60   # everything, no matter what (retained docs re-asserted)
    qos: int = 0
    exclude_integrations: list[str] = field(default_factory=lambda: ["integration_manager"])
    # HA MQTT discovery for the consuming Home Assistant (device-based format).
    # Off by default on purpose: turn it on only once the integration is
    # fully configured here, otherwise the consumer mirrors half-done state
    # (entities you are still renaming or deleting stay there as zombies).
    discovery_enabled: bool = False
    discovery_prefix: str = "homeassistant"
    # The manager as a device on the consuming HA (health, updates, resources)
    # even while entity discovery is off, e.g. in shadow mode; manager_commands
    # lets that HA install updates, restart and back up through it.
    manager_discovery: bool = False
    manager_commands: bool = False


def _notification_count(hass: HomeAssistant) -> int:
    """Persistent notifications the integration raised (a headless HA shows
    them nowhere else): part of the health document for the parent."""
    try:
        from homeassistant.components import persistent_notification as pn

        return len(pn._async_get_or_create_notifications(hass))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return 0


def platform_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """The integration an entity belongs to: its registry entry, or, for an
    entity without a unique_id (never in the registry, typical of YAML
    platforms), the entity platform that added it."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        return entry.platform
    # keyed by integration name, not by entity domain: look through all of them
    for platforms in (hass.data.get(DATA_ENTITY_PLATFORM) or {}).values():
        for platform in platforms:
            if entity_id in platform.entities:
                return platform.platform_name
    return None


def _comp_key(entity_id: str) -> str:
    return entity_id.replace(".", "_", 1)


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def _clean_json(value: Any) -> Any:
    """JSON-safe copy: keys as strings (json.dumps fails on mixed or tuple
    keys), NaN/Infinity as null (json.dumps would write invalid JSON),
    sets and tuples as lists, everything else through _json_default."""
    if isinstance(value, dict):
        return {str(k): _clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean_json(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return _clean_json(_json_default(value))


def _dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(_clean_json(value), allow_nan=False, **kwargs)


_SERVICE_NAME = re.compile(r"[a-z0-9_]+")


class MqttPublisher:
    def __init__(self, hass: HomeAssistant, key_provider=None, health_provider=None, rules_provider=None) -> None:
        self._health_provider = health_provider
        # settings.health_for(domain): stale seconds, mode, unavailable share
        self._rules_provider = rules_provider or (lambda domain: {"stale_s": HEALTH_STALE_S, "mode": "periodic", "unavailable_pct": 50})
        self._pending_clears: set[str] = set()  # topics we could not clear while disconnected
        self._blocks: dict[str, dict[str, Any]] = {}  # discovery_id -> last published device block
        self._probed_ok: set[str] = set()  # "host:port/base" namespaces probed clean by this process
        self._last_hash: dict[str, str] = {}  # topic -> content hash of the last published document (minus timestamps)
        self._last_full = 0.0
        self._moving = False  # identity move in progress: nothing may be published under the old names
        self.rules = MqttRules(hass.config.path("integration_manager", "mqtt_rules.json"))
        self._republish_unsub = None
        self._health_last: dict[str, Any] = {}
        self._started_at = time.time()
        self.hass = hass
        self.path = hass.config.path(CONFIG_FILE)
        self.config = self._load()
        # instance identity (hass_<active domain>) comes from the installer
        self._key_provider = key_provider or (lambda: "hass_remote")
        self._live_base: str | None = None      # base topic the current connection uses
        self._live_prefix: str | None = None
        self._client: mqtt.Client | None = None
        self._connected = False
        self._lock = threading.Lock()
        self._conn_lock = asyncio.Lock()  # reconnects never overlap: two paho clients with one client id kick each other off forever
        self._unsub: list[Any] = []
        self.stats: dict[str, Any] = {
            "connected": False,
            "connect_error": "",
            "published": 0,
            "cleared": 0,
            "last_publish": None,
            "last_full_republish": None,
            "health_state": None,
            "unchanged_skipped": 0,
            "last_incremental_republish": None,
            "entities_last_incremental": 0,
            "health_published": None,
            "entities_last_run": 0,
            "discovery_devices": 0,
            "discovery_components": 0,
            "discovery_mirrored": 0,
            "discovery_disabled": 0,
            "services_published": 0,
            "commands": 0,
            "last_command": None,
            "calls": 0,
            "last_call": None,
        }
        # discovery_id -> {entity_id: component}; what we last published per device
        self._discovery_map: dict[str, dict[str, dict[str, Any]]] = {}
        self._services_published: set[str] = set()
        self._services_timer: asyncio.TimerHandle | None = None
        # newest last: {id, kind, what, data, received, finished, duration_ms, state, error, result}
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=HISTORY_MAX)
        # idempotency: _id -> the canonical call record (state, result) for DEDUP_WINDOW_S,
        # independent of the visual history (which commands can push out)
        self._calls: dict[str, dict[str, Any]] = {}
        self._registry_timer: asyncio.TimerHandle | None = None
        # entity_id -> document topic last published (the registry entry is
        # already gone when the remove event fires, so recompute is wrong)
        self._topics: dict[str, str] = {}
        self.manager = None  # ManagerDevice (manager_device.py), set by __init__
        self._manager_absent_sent = False  # this connection already told the consumer there is no manager device
        self._resync_excluded = False  # integrations were excluded while disconnected: sweep the broker at the next connect
        self._health_soon_handle: asyncio.TimerHandle | None = None
        self._health_announced: str | None = None  # the verdict last published (and put in the timeline)

    # ----- identity --------------------------------------------------------

    @property
    def base_topic(self) -> str:
        return self._live_base or self._key_provider() or "hass_none"

    @property
    def wanted_base_topic(self) -> str | None:
        return self._key_provider()

    @property
    def prefix(self) -> str:
        return self._live_prefix or ((self._key_provider() or "hass_none") + "_")

    @property
    def client_id(self) -> str:
        return self.base_topic

    # ----- config ----------------------------------------------------------

    def _load(self) -> MqttConfig:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            known = {k: v for k, v in data.items() if k in MqttConfig.__dataclass_fields__}
            return MqttConfig(**known)
        except (OSError, ValueError, TypeError):
            return MqttConfig()

    def save(self, updates: dict[str, Any]) -> MqttConfig:
        """Validate types strictly: a null/NaN from the form would be stored
        and crash async_start() on the next boot, before the UI exists."""
        current = asdict(self.config)
        for k, v in updates.items():
            if k not in current or k in ("base_topic", "client_id"):
                continue  # derived from the running integration, never stored from the UI
            if k == "password" and v == "":
                continue  # blank in the UI means "keep"
            if k in ("enabled", "discovery_enabled", "force_base_topic", "manager_discovery", "manager_commands"):
                if not isinstance(v, bool):
                    raise ValueError(f"{k} must be true or false")
            elif k in ("port", "republish_interval_s", "qos", "full_republish_interval_min"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{k} must be an integer") from None
                if k == "port" and not 1 <= v <= 65535:
                    raise ValueError("port out of range")
                if k == "qos" and v not in (0, 1, 2):
                    raise ValueError("qos must be 0, 1 or 2")
                if k == "republish_interval_s":
                    v = max(30, v)
                if k == "full_republish_interval_min":
                    v = max(5, v)
            elif k == "exclude_integrations":
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise ValueError("exclude_integrations must be a list of domains")
            elif not isinstance(v, str):
                raise ValueError(f"{k} must be a string")
            elif k in ("base_topic", "discovery_prefix", "client_id", "host") and not v.strip():
                raise ValueError(f"{k} must not be empty")
            elif k in ("base_topic", "discovery_prefix") and any(ch in v for ch in "+#"):
                raise ValueError(f"{k} must not contain MQTT wildcards")
            current[k] = v
        # Written to disk only; async_reconnect() adopts it, so it can still
        # compare the old topics against the new ones and clear them.
        new = MqttConfig(**current)
        write_json(self.path, asdict(new), mode=0o600, fsync=False)
        return new

    def public_config(self) -> dict[str, Any]:
        d = asdict(self.config)
        d["password"] = "***" if d["password"] else ""
        d["base_topic"] = self.wanted_base_topic
        d["client_id"] = self.wanted_base_topic
        d["derived"] = ["base_topic", "client_id"]
        return d

    # ----- lifecycle -------------------------------------------------------

    async def async_start(self) -> None:
        self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state))
        self._unsub.append(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry)
        )
        self._arm_republish_timer()
        self._unsub.append(async_track_time_interval(self.hass, self._on_health_timer, timedelta(seconds=HEALTH_INTERVAL_S)))
        # the verdict follows the integration at once (its entry loading at boot, a
        # failed setup, a reload), not only at the next timer tick a minute later
        self._unsub.append(async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._on_entry_changed))
        self._unsub.append(self.hass.bus.async_listen(EVENT_COMPONENT_LOADED, self._on_component_loaded))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started)
        # Integrations register their services after we connect (the
        # integration loads later in the boot); refresh the catalog, debounced.
        for ev in (EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED):
            self._unsub.append(self.hass.bus.async_listen(ev, self._on_service_event))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)
        if self.config.enabled:
            # in the background: a broker that hangs, or the sweep of an identity that changed while
            # disconnected, must not hold up the setup of this component (and with it the boot)
            self.hass.async_create_background_task(self._async_first_connect(), "integration_manager MQTT connect")

    async def _async_first_connect(self) -> None:
        async with self._conn_lock:
            try:
                await self.hass.async_add_executor_job(self._connect)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("MQTT connect failed: %s", err)
                self.stats["connect_error"] = f"{type(err).__name__}: {err}"

    async def async_reload_config(self) -> None:
        """Adopt settings that do not need a reconnect (discovery on/off)."""
        self.config = await self.hass.async_add_executor_job(self._load)

    def _arm_republish_timer(self) -> None:
        if self._republish_unsub is not None:
            self._republish_unsub()
        self._republish_unsub = async_track_time_interval(
            self.hass, self._on_timer, timedelta(seconds=max(30, self.config.republish_interval_s)))
        self._republish_interval = self.config.republish_interval_s

    async def async_reconnect(self) -> None:
        async with self._conn_lock:
            await self._async_reconnect_locked()

    async def _async_reconnect_locked(self) -> None:
        """Reload the config file and reconnect.  If the instance identity
        (running integration) or the discovery prefix changed since we
        connected, everything we own under the old names is cleared first
        (a stop is not a move: the consumer keeps its entities)."""
        new = await self.hass.async_add_executor_job(self._load)
        # A stop (wanted identity None) is NOT a move: the consumer keeps its
        # entities, marked unavailable by the retained "offline"; clearing
        # would delete them there with every customisation.  Uninstall clears.
        moved = self._connected and ((self.wanted_base_topic is not None and self.wanted_base_topic != self._live_base)
                                     or new.discovery_prefix != self.config.discovery_prefix)
        if self.wanted_base_topic != getattr(self, "_last_wanted", self.wanted_base_topic):
            self._started_at = time.time()  # health grace restarts with a new identity
            self._pending_clears.clear()
        self._last_wanted = self.wanted_base_topic
        # integrations newly excluded: their retained documents must go too
        newly_excluded = set(new.exclude_integrations) - set(self.config.exclude_integrations)
        if newly_excluded and not moved:
            for eid, topic in list(self._topics.items()):
                if (self._integration_of(eid) or "unregistered") not in newly_excluded:
                    continue
                if self._connected:
                    self._clear(eid)
                else:
                    # cleared at the next connect: _publish_state never touches an excluded entity again
                    self._topics.pop(eid, None)
                    self._last_hash.pop(topic, None)
                    self._pending_clears.add(topic)
            if not self._connected:
                self._resync_excluded = True  # after a restart this process does not know every topic of theirs
        for t in ("_registry_timer", "_services_timer"):
            h = getattr(self, t, None)
            if h is not None:
                h.cancel()
                setattr(self, t, None)
        swept = True  # a failed sweep is retried at the next connect
        if moved:
            self._moving = True  # state events keep arriving: nothing goes out under the old names
            # The consumer would keep every retained doc/config under the old
            # names (and reject the new ones as duplicate unique_ids).
            # the throwaway sweep finds everything retained under the old names,
            # what this process published included; forget the bookkeeping
            for m in (self._topics, self._last_hash, self._discovery_map, self._blocks):
                m.clear()
            self._services_published.clear()
            cleared = await self.hass.async_add_executor_job(self._clear_retained_under, self.base_topic, self.config.discovery_prefix)
            swept = cleared is not None
            _LOGGER.info("MQTT: cleared %s retained topics left under the old names by earlier runs", cleared)
        if (moved or new.discovery_prefix != self.config.discovery_prefix) and swept:
            # the live move handled it: the next connect must not sweep the old names a second time
            await self.hass.async_add_executor_job(self._remember_identity, self.wanted_base_topic, new.discovery_prefix)
        # no retained "offline" on a status topic we just cleared
        await self.hass.async_add_executor_job(self._disconnect, not moved)
        self._moving = False
        self.config = new
        if getattr(self, "_republish_interval", None) != new.republish_interval_s:
            self._arm_republish_timer()
        if self.config.enabled:
            await self.hass.async_add_executor_job(self._connect)
        self.publish_health()  # status() shows the new identity's verdict right away

    def _is_ours(self, topic: str, payload: bytes, base_topic: str) -> bool:
        """Only what this tool publishes may be cleared: never someone else's
        retained data that happens to share a prefix."""
        if not payload:
            return False
        if topic == f"{base_topic}/status":
            return payload in (b"online", b"offline")
        if topic.startswith((f"{base_topic}/cmd/", f"{base_topic}/call/", f"{base_topic}/result/", f"{base_topic}/manager/cmd/")):
            return True  # a consumer that retained a command must not block our connect
        try:
            doc = json.loads(payload)
        except ValueError:
            return False
        if not isinstance(doc, dict):
            return False
        if topic == f"{base_topic}/health":
            return "updated_at" in doc and "base_topic" in doc
        if topic == f"{base_topic}/manager":
            return "updated_at" in doc and "manager_version" in doc
        if topic.startswith(base_topic + "/"):
            return ("published_at" in doc and "integration" in doc) or "call_topic" in doc
        # exact origin of THIS identity: instance hass_a must not clear hass_a_b's configs
        origin_name = str((doc.get("origin") or {}).get("name", ""))
        return origin_name == disc.origin(base_topic + "_")["name"]

    def probe_foreign(self, base_topic: str) -> dict[str, Any]:
        """Blocking: what sits retained under <base>/# that is NOT ours."""
        try:
            found = self._retained_scan("probe", [(f"{base_topic}/#", 1)], min_s=2.0)
        except Exception as err:  # noqa: BLE001
            return {"error": f"{type(err).__name__}: {err}", "foreign": [], "ours": 0}
        foreign = [t for t, p in found.items() if not self._is_ours(t, p, base_topic)]
        return {"foreign": sorted(foreign)[:20], "foreign_count": len(foreign), "ours": len(found) - len(foreign)}

    def _retained_scan(self, suffix: str, topics: list[tuple[str, int]], min_s: float = 2.0) -> dict[str, bytes]:
        """Blocking: a throwaway client that collects every retained message
        under `topics` until the burst goes quiet; returns {topic: payload}.
        The network thread is always stopped, whatever happens."""
        found: dict[str, bytes] = {}
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.client_id}-{suffix}", clean_session=True)
        if self.config.username:
            c.username_pw_set(self.config.username, self.config.password or None)
        c.on_message = lambda cl, u, m: found.__setitem__(m.topic, m.payload) if m.retain and m.payload else None
        ack: dict[str, Any] = {"rc": None, "granted": None}
        c.on_connect = lambda cl, u, flags, rc, props=None: ack.__setitem__("rc", rc)
        c.on_subscribe = lambda cl, u, mid, granted, props=None: ack.__setitem__("granted", granted)
        c.connect(self.config.host, self.config.port, keepalive=30)
        try:
            c.subscribe(topics)
            c.loop_start()
            t0 = time.time()
            while (ack["rc"] is None or ack["granted"] is None) and time.time() - t0 < 5:
                time.sleep(0.05)
            if ack["rc"] is None or ack["rc"] != 0:
                raise RuntimeError(f"the broker did not accept the scan connection ({ack['rc']})")
            if ack["granted"] is None or any(getattr(g, "is_failure", False) for g in ack["granted"]):
                raise RuntimeError("the broker refused the subscription (ACL?)")
            self._collect_quiet(c, found, min_s=min_s)
        finally:
            c.loop_stop()
            c.disconnect()
        return found

    def _clear_topics(self, suffix: str, topics: list[str]) -> None:
        """Blocking: an empty retained payload to each topic from a throwaway
        client (QoS 1, awaited)."""
        if not topics:
            return
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.client_id}-{suffix}-clear", clean_session=True)
        if self.config.username:
            c.username_pw_set(self.config.username, self.config.password or None)
        c.connect(self.config.host, self.config.port, keepalive=30)
        try:
            c.loop_start()
            infos = [c.publish(t, "", qos=1, retain=True) for t in topics]
            # one budget for the whole sweep, not 5 s per topic: a broker that stops acknowledging
            # would otherwise hold the reconnect lock (or an uninstall) for hours
            deadline = time.monotonic() + min(120.0, 15.0 + 0.02 * len(infos))
            while time.monotonic() < deadline and c.is_connected() and not all(i.is_published() for i in infos):
                time.sleep(0.1)
            unconfirmed = sum(1 for i in infos if not i.is_published())
            if unconfirmed:
                raise RuntimeError(f"the broker did not confirm {unconfirmed} of {len(infos)} cleared topics")
        finally:
            c.loop_stop()
            c.disconnect()

    @staticmethod
    def _collect_quiet(c: mqtt.Client, found: dict[str, bytes], min_s: float = 2.0, quiet_s: float = 1.0, max_s: float = 15.0) -> None:
        """Wait for the retained burst: at least min_s, then until nothing new
        arrived for quiet_s, capped at max_s (busy brokers with thousands of
        retained configs need more than a fixed 3 s)."""
        t0 = time.time()
        last_n, last_change = -1, t0
        while True:
            time.sleep(0.25)
            now = time.time()
            if len(found) != last_n:
                last_n, last_change = len(found), now
            if now - t0 >= min_s and now - last_change >= quiet_s:
                return
            if now - t0 >= max_s:
                return

    def _identity_file(self) -> str:
        return self.hass.config.path("integration_manager", "mqtt_identity.json")

    def _remember_identity(self, base: str | None, prefix: str) -> None:
        """Blocking: the names retained data was last published under."""
        if not base:
            return
        try:
            write_json(self._identity_file(), {"base": base, "prefix": prefix}, fsync=False)
        except OSError:
            pass

    def _clear_retained_under(self, base_topic: str, discovery_prefix: str, docs: bool = True) -> int | None:
        """Blocking: every retained topic of ours under <base>/# (unless
        ``docs`` is False) plus the discovery configs carrying our origin
        under <prefix>/device/+/config get an empty retained payload."""
        try:
            topics = [(f"{discovery_prefix}/device/+/config", 1)] + ([(f"{base_topic}/#", 1)] if docs else [])
            found = self._retained_scan("cleanup", topics)
            ours = [t for t, p in found.items() if self._is_ours(t, p, base_topic)]
            self._clear_topics("cleanup", ours)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("retained cleanup under %s failed: %s", base_topic, err)
            return None  # not done: the identity must not be recorded as moved
        skipped = len(found) - len(ours)
        if skipped:
            _LOGGER.info("MQTT: left %s retained topics under %s alone (not ours)", skipped, base_topic)
        return len(ours)

    async def _on_stop(self, _: Event) -> None:
        await self.hass.async_add_executor_job(self._disconnect)

    # ----- paho ------------------------------------------------------------

    def _status_topic(self) -> str:
        return f"{self.base_topic}/status"

    def _connect(self) -> None:
        base = self.wanted_base_topic
        if not base:
            # nothing running -> no identity -> nothing to publish under
            self.stats["connect_error"] = "no integration is running: MQTT has no identity (hass_<domain>) yet"
            _LOGGER.info("MQTT: %s", self.stats["connect_error"])
            return
        probe_key = f"{self.config.host}:{self.config.port}/{base}"  # a different broker is a different namespace
        if not self.config.force_base_topic and probe_key not in self._probed_ok:
            probe = self.probe_foreign(base)
            self.stats["foreign_topics"] = probe.get("foreign", [])
            self.stats["foreign_count"] = probe.get("foreign_count", 0)
            if probe.get("foreign_count"):
                self.stats["connect_error"] = (f"base topic {base} already carries {probe['foreign_count']} retained topics that are not ours "
                                               f"(e.g. {probe['foreign'][0]}); not connecting. Tick force_base_topic to use it anyway")
                _LOGGER.error("MQTT: %s", self.stats["connect_error"])
                return
            if probe.get("error"):
                _LOGGER.warning("MQTT: could not verify that %s is free (%s); connecting, verifying again at the next connect", base, probe["error"])
            else:
                self._probed_ok.add(probe_key)  # this process owns the namespace now: no re-probe on reconnects
        elif self.config.force_base_topic:
            self.stats["foreign_topics"], self.stats["foreign_count"] = [], 0
        last = read_json(self._identity_file(), {}) or {}
        swept = True
        if isinstance(last, dict) and last.get("base") and (last["base"], last.get("prefix")) != (base, self.config.discovery_prefix):
            # changed while we were not connected (async_reconnect only moves a live
            # connection): what the previous names left retained goes now; with the
            # same identity only the discovery configs under the old prefix moved
            same_base = last["base"] == base
            n = self._clear_retained_under(last["base"], last.get("prefix") or self.config.discovery_prefix, docs=not same_base)
            swept = n is not None
            _LOGGER.info("MQTT: identity/prefix changed while disconnected (%s/%s -> %s/%s): cleared %s retained topics",
                         last.get("base"), last.get("prefix"), base, self.config.discovery_prefix, n)
        if swept:
            self._remember_identity(base, self.config.discovery_prefix)
        self._live_base = base
        self._live_prefix = base + "_"
        old = self._client
        if old is not None:  # belt and braces next to the lock: never leave a second client running
            self._client = None
            try:
                old.loop_stop()
                old.disconnect()
            except Exception:  # noqa: BLE001
                pass
        c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            clean_session=True,
        )
        if self.config.username:
            c.username_pw_set(self.config.username, self.config.password or None)
        c.will_set(self._status_topic(), "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.suppress_exceptions = True  # a callback bug must not kill the network thread
        c.reconnect_delay_set(min_delay=2, max_delay=60)
        try:
            c.connect_async(self.config.host, self.config.port, keepalive=60)
            c.loop_start()
            self._client = c
            self.stats["connect_error"] = ""
        except Exception as err:  # noqa: BLE001
            self.stats["connect_error"] = f"{type(err).__name__}: {err}"
            _LOGGER.error("MQTT connect failed: %s", err)

    def _disconnect(self, publish_offline: bool = True) -> None:
        c, self._client = self._client, None
        if c is None:
            self._live_base = self._live_prefix = None
            return
        try:
            if publish_offline and self._connected:
                c.publish(self._status_topic(), "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:  # noqa: BLE001
            pass
        finally:
            # always: an orphaned paho thread would keep reconnecting with
            # our callbacks bound and steal the client id from the new client
            try:
                c.loop_stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._connected = False
        self.stats["connected"] = False
        self._live_base = self._live_prefix = None
        self.hass.loop.call_soon_threadsafe(self._last_hash.clear)  # a new connection re-asserts every retained document

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            self._connected = False
            self.stats["connected"] = False
            self.stats["connect_error"] = f"reason_code={reason_code}"
            _LOGGER.error("MQTT connect refused: %s", reason_code)
            return
        self._connected = True
        self.stats["connected"] = True
        self.stats["connect_error"] = ""
        client.publish(self._status_topic(), "online", qos=1, retain=True)
        # Commands from the consuming HA: <base>/cmd/<domain>/<object_id>/<field>
        # and generic service calls: <base>/call/<domain>/<service>
        client.subscribe([(f"{self._cmd_base()}/#", 1), (f"{self._call_base()}/#", 1), (f"{self._manager_cmd_base()}/+", 1)])
        _LOGGER.info("MQTT connected to %s:%s", self.config.host, self.config.port)
        events.emit("mqtt", f"connected to {self.config.host}:{self.config.port} as {self.base_topic}")
        # Runs in paho's thread: hop onto the HA loop for the full publish.  A
        # broker that came back without its retained store must get every
        # discovery config and the service catalog again (paho's automatic
        # reconnect lands here too): the "already published" memory is dropped
        # on the loop, which is the only place that reads it, right before.
        def _resume() -> None:
            self._last_hash.clear()
            self._manager_absent_sent = False
            self.hass.async_create_task(self.async_republish_all())

        self.hass.loop.call_soon_threadsafe(_resume)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected = False
        self.stats["connected"] = False
        if reason_code != 0:
            _LOGGER.warning("MQTT disconnected (%s); paho will retry", reason_code)
            events.emit("mqtt", f"disconnected ({reason_code}); reconnecting")

    def _cmd_base(self) -> str:
        return f"{self.base_topic}/cmd"

    def _call_base(self) -> str:
        return f"{self.base_topic}/call"

    def _manager_cmd_base(self) -> str:
        return f"{self.base_topic}/manager/cmd"

    def _on_message(self, client, userdata, msg) -> None:
        """Command or service call from the consuming HA (paho thread).
        Anything raised here would end paho's network loop, so nothing may."""
        try:
            self._handle_message(msg)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("MQTT message %s could not be handled: %s", msg.topic, err)

    def _handle_message(self, msg) -> None:
        if getattr(msg, "retain", False):
            # a command published with retain would run again at every
            # (re)subscription: physical effects must never replay
            _LOGGER.warning("MQTT: ignoring retained command on %s (commands must not be retained)", msg.topic)
            return
        manager_prefix = self._manager_cmd_base() + "/"
        if msg.topic.startswith(manager_prefix):
            self._on_manager_command(msg.topic[len(manager_prefix):], msg.payload.decode(errors="replace"))
            return
        call_prefix = self._call_base() + "/"
        if msg.topic.startswith(call_prefix):
            if not msg.payload:
                self._reject_empty_call(msg.topic[len(call_prefix):])
                return
            self._on_call(msg.topic[len(call_prefix):], msg.payload.decode(errors="replace"))
            return
        prefix = self._cmd_base() + "/"
        if not msg.topic.startswith(prefix):
            return
        parts = msg.topic[len(prefix):].split("/")
        if len(parts) != 3:
            return
        domain, object_id, field = parts
        payload = msg.payload.decode(errors="replace")
        if not payload and ((domain, field) not in (("text", "value"), ("notify", "message")) or self._moving):
            # clearing a retained command reaches live subscribers as an empty payload (this process
            # clears retained cmd topics itself when its identity moves): only text/notify take "" as a value
            self._finish(self._remember("cmd", f"{domain}.{object_id}/{field}", ""), "ignored", "empty payload")
            return
        rec = self._remember("cmd", f"{domain}.{object_id}/{field}", payload)
        if f"{domain}.{object_id}" not in self._topics:
            self._finish(rec, "rejected", "not an entity this container publishes")
            return
        try:
            mapped = disc.command_to_service(domain, object_id, field, payload)
        except (ValueError, KeyError, OverflowError) as err:
            _LOGGER.warning("MQTT command %s=%r rejected: %s", msg.topic, payload, err)
            self._finish(rec, "rejected", str(err))
            return
        if mapped is None:
            _LOGGER.warning("MQTT command not supported: %s", msg.topic)
            self._finish(rec, "rejected", "not supported")
            return
        svc_domain, service, data = mapped
        rec["what"] = f"{domain}.{object_id}/{field} → {svc_domain}.{service}"
        self.stats["commands"] += 1
        self.stats["last_command"] = f"{msg.topic} = {payload}"

        async def _call() -> None:
            if svc_domain == "climate" and service == "set_temperature" and ("target_temp_high" in data) != ("target_temp_low" in data):
                # MQTT climate sends each bound on its own topic; HA's schema wants both
                other = "target_temp_low" if "target_temp_high" in data else "target_temp_high"
                st = self.hass.states.get(data["entity_id"])
                val = st.attributes.get(other) if st else None
                if val is None:
                    self._finish(rec, "rejected", f"{other} is unknown: a range setpoint needs both bounds")
                    return
                data[other] = val
            # like a service call: the handler keeps running past the timeout, the
            # history shows "timeout" now and "late-ok"/"late-error" when it ends
            task = self.hass.async_create_task(self.hass.services.async_call(svc_domain, service, data, blocking=True))
            done, _ = await asyncio.wait({task}, timeout=CALL_TIMEOUT_S)
            late = not done
            if late:
                self._finish(rec, "timeout", f"no answer after {CALL_TIMEOUT_S}s (service still running)")
                _LOGGER.warning("MQTT command %s -> %s.%s: no answer after %ss", msg.topic, svc_domain, service, CALL_TIMEOUT_S)
            try:
                await task
                self._finish(rec, "late-ok" if late else "ok")
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                self._finish(rec, "late-error" if late else "error", "cancelled by the service handler")
            except Exception as err:  # noqa: BLE001 - logged, never crashes the loop
                _LOGGER.error("MQTT command %s -> %s.%s failed: %s", msg.topic, svc_domain, service, err)
                self._finish(rec, "late-error" if late else "error", f"{type(err).__name__}: {err}")

        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(_call()))

    def _on_manager_command(self, action: str, payload: str) -> None:
        """<base>/manager/cmd/<action> (paho thread): see manager_device.py."""
        rec = self._remember("manager", action[:40], payload)
        expected = disc.MANAGER_ACTIONS.get(action)
        if expected is None or payload.strip() != expected:
            # an empty payload clearing a retained command reaches live subscribers too: never an action
            self._finish(rec, "rejected", f"unknown action {action!r}" if expected is None else f"payload must be {expected!r}")
            return
        if not self.config.manager_commands:
            self._finish(rec, "rejected", "manager_commands is off")
            return
        if self.manager is None:
            self._finish(rec, "rejected", "the manager device is not set up")
            return
        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(self.manager.async_action(action, rec)))

    def _remember(self, kind: str, what: str, data: Any, call_id: Any = None) -> dict[str, Any]:
        rec = {"id": call_id, "kind": kind, "what": what, "data": (json.dumps(data, default=str) if not isinstance(data, str) else data)[:200],
               "received": time.time(), "finished": None, "duration_ms": None, "state": "running", "error": None, "result": None}
        self.history.append(rec)
        return rec

    @staticmethod
    def _finish(rec: dict[str, Any], state: str, error: str | None = None, result: dict[str, Any] | None = None) -> None:
        now = time.time()
        rec["finished"] = now
        rec["duration_ms"] = int((now - rec["received"]) * 1000)
        rec["state"], rec["error"] = state, error
        if result is not None:
            rec["result"] = result

    def _seen_call(self, key: str | None) -> dict[str, Any] | None:
        """The canonical record of a call with this id inside the dedup window."""
        if key is None:
            return None
        now = time.time()
        for k in [k for k, r in self._calls.items() if now - r["received"] >= DEDUP_WINDOW_S]:
            del self._calls[k]  # expired
        return self._calls.get(key)

    def recent_commands(self, limit: int = 30) -> list[dict[str, Any]]:
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if t else None
        rows = list(self.history)[-limit:]
        return [{**r, "received": iso(r["received"]), "finished": iso(r["finished"]), "result": None} for r in reversed(rows)]

    def _reject_empty_call(self, rest: str) -> None:
        """A call needs a JSON object ({} without data); an empty payload is what clearing a retained call looks like."""
        parts = rest.split("/")
        rec = self._remember("call", rest[:80], "")
        self._finish(rec, "rejected", "empty payload: send {} to call a service without data")
        if len(parts) == 2 and _SERVICE_NAME.fullmatch(parts[0].lower()) and _SERVICE_NAME.fullmatch(parts[1].lower()) and not self._moving:
            self._publish_result(parts[0].lower(), parts[1].lower(), {"ok": False, "error": "empty payload: send {} to call a service without data"})

    def _on_call(self, rest: str, payload: str) -> None:
        """Generic service call: <base>/call/<domain>/<service> with a JSON
        object as payload (service data incl. entity_id/device_id/area_id;
        an optional "_id" is echoed back).  Outcome goes to
        <base>/result/<domain>/<service>, not retained."""
        parts = rest.split("/")
        if len(parts) != 2:
            return
        domain, service = parts[0].lower(), parts[1].lower()  # HA looks services up in lower case, so the deny list must too
        if not _SERVICE_NAME.fullmatch(domain) or not _SERVICE_NAME.fullmatch(service):
            self._finish(self._remember("call", rest[:80], payload), "rejected", "domain and service must be names made of a-z, 0-9 and _")
            return
        if domain in CALL_DENY_DOMAINS or domain in self.config.exclude_integrations:
            self._publish_result(domain, service, {"ok": False, "error": f"domain {domain} is not callable over MQTT"})
            self._finish(self._remember("call", f"{domain}.{service}", payload), "rejected", f"domain {domain} is not callable over MQTT")
            return
        try:
            data = json.loads(payload) if payload.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("payload must be a JSON object")
        except ValueError as err:
            self._publish_result(domain, service, {"ok": False, "error": f"bad payload: {err}"})
            self._finish(self._remember("call", f"{domain}.{service}", payload), "rejected", f"bad payload: {err}")
            return
        call_id = data.pop("_id", None)
        # an _id is unique per service for the consumer (a counter that restarts, one per automation)
        call_key = f"{domain}.{service}:{call_id}" if call_id not in (None, "") else None
        prior = self._seen_call(call_key)
        if prior is not None:
            # A retry of the same _id (the consumer did not see the result in
            # time): answer from history, never run the service twice.
            dup = self._remember("call", f"{domain}.{service}", data, call_id)
            if prior["state"] == "running":
                self._publish_result(domain, service, {"id": call_id, "service": f"{domain}.{service}", "ok": None, "state": "running", "duplicate": True})
                self._finish(dup, "duplicate", "still running")
            else:
                self._publish_result(domain, service, {**(prior.get("result") or {"id": call_id, "service": f"{domain}.{service}", "ok": None}), "duplicate": True})
                self._finish(dup, "duplicate", f"answered from history ({prior['state']})")
            _LOGGER.info("MQTT call %s.%s id=%s repeated: answered from history (%s)", domain, service, call_id, prior["state"])
            return
        rec = self._remember("call", f"{domain}.{service}", data, call_id)
        if call_id not in (None, ""):
            self._calls[call_key] = rec  # the canonical record: duplicates never replace it
        self.stats["calls"] += 1
        self.stats["last_call"] = f"{domain}.{service} {json.dumps(data)[:120]}"

        async def _call() -> None:
            base: dict[str, Any] = {"id": call_id, "service": f"{domain}.{service}"}
            if not self.hass.services.has_service(domain, service):
                res = {**base, "ok": False, "error": f"unknown service {domain}.{service}"}
                self._publish_result(domain, service, res)
                self._finish(rec, "error", res["error"], res)
                _LOGGER.warning("MQTT call %s.%s failed: unknown service", domain, service)
                return
            wants = self.hass.services.supports_response(domain, service) != SupportsResponse.NONE
            _LOGGER.debug("MQTT call %s.%s start (response=%s) data=%s", domain, service, wants, data)
            # Not wait_for(): cancelling a service handler that shields or
            # swallows CancelledError would hang the timeout itself.  The
            # call keeps running; the caller gets a timeout now and the real
            # outcome later, flagged "late".
            task = self.hass.async_create_task(
                self.hass.services.async_call(domain, service, data, blocking=True, return_response=wants)
            )
            done, _ = await asyncio.wait({task}, timeout=CALL_TIMEOUT_S)
            late = not done
            _LOGGER.debug("MQTT call %s.%s wait returned, done=%s", domain, service, bool(done))
            if late:
                res = {**base, "ok": False, "error": f"timeout after {CALL_TIMEOUT_S}s (service still running)"}
                self._publish_result(domain, service, res)
                self._finish(rec, "timeout", res["error"], res)
                _LOGGER.warning("MQTT call %s.%s timed out after %ss", domain, service, CALL_TIMEOUT_S)
            try:
                resp = await task
                result = {**base, "ok": True}
                if wants:
                    result["response"] = resp
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # The handler's own task was cancelled (some integrations
                # cancel long RF requests they cannot send); report, don't
                # propagate into our task.
                result = {**base, "ok": False, "error": "cancelled by the service handler"}
                _LOGGER.warning("MQTT call %s.%s was cancelled by the handler", domain, service)
            except Exception as err:  # noqa: BLE001 - reported to the caller
                result = {**base, "ok": False, "error": f"{type(err).__name__}: {err}"}
                _LOGGER.warning("MQTT call %s.%s failed: %s", domain, service, err)
            if late:
                result["late"] = True
            self._publish_result(domain, service, result)
            self._finish(rec, ("late-ok" if result.get("ok") else "late-error") if late else ("ok" if result.get("ok") else "error"),
                         result.get("error"), result)

        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(_call()))

    def _publish_result(self, domain: str, service: str, result: dict[str, Any]) -> None:
        c = self._client
        if c is None or not self._connected:
            return
        info = c.publish(f"{self.base_topic}/result/{domain}/{service}",
                         _dumps(result), qos=1, retain=False)
        _LOGGER.debug("MQTT result %s.%s published rc=%s ok=%s", domain, service, info.rc, result.get("ok"))

    def _publish_if_changed(self, topic: str, payload: str, qos: int | None = None) -> bool:
        """Retained payloads that carry no timestamp (discovery configs, the
        service catalog): skip when identical to what was last published on
        this connection (the hash map is cleared on every disconnect, so a
        reconnect still re-asserts everything).  Returns True when the
        broker holds the current payload."""
        digest = hashlib.sha1(payload.encode()).hexdigest()
        if self._last_hash.get(topic) == digest:
            self.stats["unchanged_skipped"] += 1
            return True
        if self._publish(topic, payload, qos=qos):
            self._last_hash[topic] = digest
            return True
        return False

    def _publish(self, topic: str, payload: str | None, retain: bool = True, qos: int | None = None) -> bool:
        c = self._client
        if c is None or not self._connected or self._moving:
            return False
        if payload is None:
            # a cleared retained topic must be published again next time, even
            # with content identical to what was there (device gone and back,
            # service catalog of a domain that returns)
            self._last_hash.pop(topic, None)
        info = c.publish(topic, payload if payload is not None else "", qos=self.config.qos if qos is None else qos, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # ----- entity documents -----------------------------------------------

    def _topic_for(self, entity_id: str, integration: str) -> str:
        domain, object_id = entity_id.split(".", 1)
        return f"{self.base_topic}/{integration}/{domain}/{object_id}"

    def _integration_of(self, entity_id: str) -> str | None:
        return platform_of(self.hass, entity_id)

    def build_document(self, state: State) -> tuple[str, dict[str, Any]] | None:
        """Return (topic, document) or None if the entity is excluded."""
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(state.entity_id)
        integration = platform_of(self.hass, state.entity_id) or "unregistered"
        if integration in self.config.exclude_integrations:
            return None
        rule = self.rules.for_entity(state.entity_id)
        if rule.get("exclude"):
            return None

        domain, object_id = state.entity_id.split(".", 1)
        doc: dict[str, Any] = {
            "entity_id": state.entity_id,
            "domain": domain,
            "object_id": object_id,
            "integration": integration,
            "state": state.state,
            "attributes": dict(state.attributes),
            "last_changed": state.last_changed.isoformat(),
            "last_updated": state.last_updated.isoformat(),
            # moves on every write by the integration, value changed or not; picked
            # up by the incremental pass (reports fire no state_changed event)
            "last_reported": state.last_reported.isoformat(),
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if entry:
            doc.update(
                {
                    "unique_id": entry.unique_id,
                    "name": entry.name or entry.original_name,
                    "original_name": entry.original_name,
                    "device_class": entry.device_class or entry.original_device_class,
                    "unit_of_measurement": entry.unit_of_measurement,
                    "icon": entry.icon or entry.original_icon,
                    "entity_category": entry.entity_category.value if entry.entity_category else None,
                    "disabled": entry.disabled,
                    "hidden": entry.hidden,
                    "area_id": entry.area_id,
                    "labels": sorted(entry.labels),
                    "config_entry_id": entry.config_entry_id,
                    "translation_key": entry.translation_key,
                }
            )
            if entry.device_id:
                dev = dr.async_get(self.hass).async_get(entry.device_id)
                if dev and disc.is_child_device(dev):
                    parent = dr.async_get(self.hass).async_get(dev.parent_device_id)
                    doc["device"] = {"id": dev.id, "name": dev.name_by_user or dev.name, "child_of": dev.parent_device_id,
                                     "parent_name": (parent.name_by_user or parent.name) if parent else None}
                elif dev:
                    doc["device"] = {
                        "id": dev.id,
                        "name": dev.name_by_user or dev.name,
                        "manufacturer": dev.manufacturer,
                        "model": dev.model,
                        "sw_version": dev.sw_version,
                        "identifiers": [list(i) for i in dev.identifiers],
                        "via_device_id": dev.via_device_id,
                        "area_id": dev.area_id,
                    }
        if rule:
            doc["mqtt_rule"] = rule
            if rule.get("name"):
                doc["name"] = rule["name"]
        return self._topic_for(state.entity_id, integration), doc

    def _publish_state(self, state: State, is_event: bool = False, force: bool = True) -> bool:
        built = self.build_document(state)
        if built is None:
            return False
        topic, doc = built
        self._topics[state.entity_id] = topic
        digest = hashlib.sha1(_dumps({k: v for k, v in doc.items() if k != "published_at"},
                                         sort_keys=True).encode()).hexdigest()
        if not force and self._last_hash.get(topic) == digest:
            self.stats["unchanged_skipped"] += 1
            return False
        payload = _dumps(doc)
        if not self._publish(topic, payload):
            return False
        self._last_hash[topic] = digest
        if is_event and doc["domain"] == "event":
            # Real occurrence: also emit on the non-retained stream the
            # discovered event entity listens to (a retained doc would
            # replay the last event on every reconnect).
            self._publish(disc.event_stream_topic(topic), payload, retain=False)
        self.stats["published"] += 1
        self.stats["last_publish"] = doc["published_at"]
        return True

    # ----- HA MQTT discovery (device-based) ---------------------------------

    def _discovery_topic(self, discovery_id: str) -> str:
        return f"{self.config.discovery_prefix}/device/{discovery_id}/config"

    def _group_by_device(self) -> tuple[dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]], dict[str, int]]:
        """Return {discovery_id: (device_block, {entity_id: component})} and
        counters.  Every entity gets a component: native MQTT platform where
        one exists, a read-only sensor mirror otherwise; registry entries
        without a state (disabled) are included as enabled_by_default=false."""
        ent_reg = er.async_get(self.hass)
        groups: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] = {}
        counts = {"mirrored": 0, "disabled": 0}
        seen: set[str] = set()
        for state in self.hass.states.async_all():
            seen.add(state.entity_id)
            entry = ent_reg.async_get(state.entity_id)
            integration = platform_of(self.hass, state.entity_id) or "unregistered"
            if integration in self.config.exclude_integrations:
                continue
            rule = self.rules.for_entity(state.entity_id)
            if rule.get("exclude"):
                continue
            doc_topic = self._topic_for(state.entity_id, integration)
            try:
                comp = self.rules.apply_component(disc.build_component(self.hass, state, doc_topic, self._cmd_base(), self.prefix), rule)
            except Exception as err:  # noqa: BLE001 - one bad attribute must not drop discovery of every device
                _LOGGER.warning("MQTT discovery: %s skipped: %s", state.entity_id, err)
                continue
            if comp["platform"] != state.domain:
                counts["mirrored"] += 1
            disc_id, block = disc.device_block(self.hass, entry.device_id if entry else None, integration, self.prefix)
            groups.setdefault(disc_id, (block, {}))[1][state.entity_id] = comp
        loaded = set(self.hass.config.components)
        for entry in list(ent_reg.entities.values()):
            if entry.entity_id in seen or entry.platform in self.config.exclude_integrations:
                continue
            if entry.platform not in loaded:
                continue  # an installed-but-stopped integration's entries are not announced under this identity
            rule = self.rules.for_entity(entry.entity_id)
            if rule.get("exclude"):
                continue
            doc_topic = self._topic_for(entry.entity_id, entry.platform)
            try:
                comp = self.rules.apply_component(disc.build_component_from_entry(self.hass, entry, doc_topic, self._cmd_base(), self.prefix), rule)
            except Exception as err:  # noqa: BLE001 - one bad attribute must not drop discovery of every device
                _LOGGER.warning("MQTT discovery: %s skipped: %s", entry.entity_id, err)
                continue
            if entry.disabled:
                counts["disabled"] += 1
            if comp["platform"] != entry.domain:
                counts["mirrored"] += 1
            disc_id, block = disc.device_block(self.hass, entry.device_id, entry.platform, self.prefix)
            groups.setdefault(disc_id, (block, {}))[1][entry.entity_id] = comp
        return groups, counts

    def discovery_preview(self) -> list[dict[str, Any]]:
        """What would be (or is) published as discovery, for the UI/API."""
        groups, counts = self._group_by_device()
        hid, hblock, hcomps = self._manager_discovery()
        groups[hid] = (hblock, hcomps)
        return [
            {"discovery_id": disc_id, "topic": self._discovery_topic(disc_id), "device": block,
             "components": {eid: comp for eid, comp in comps.items()}}
            for disc_id, (block, comps) in groups.items()
        ]

    def _publish_device_discovery(self, discovery_id: str, block: dict[str, Any], comps: dict[str, dict[str, Any]]) -> None:
        payload = {
            "device": block,
            "origin": disc.origin(self.prefix),
            "payload_available": "online",
            "payload_not_available": "offline",
            # keyed by "<domain>_<object_id>": object ids alone collide across
            # domains (switch.x + light.x on one device)
            "components": {_comp_key(eid): comp for eid, comp in comps.items()},
        }
        # Entities that were in this device last time and are gone now must
        # be sent once in HA's removal form, otherwise the consumer keeps them.
        for gone in set(self._discovery_map.get(discovery_id, {})) - set(comps):
            # the platform we PUBLISHED (a mirrored media_player is a "sensor"), or HA rejects the whole device
            payload["components"][_comp_key(gone)] = {"platform": self._discovery_map[discovery_id][gone].get("platform", gone.split(".", 1)[0])}
        topic = self._discovery_topic(discovery_id)
        if self._publish_if_changed(topic, _dumps(payload), qos=1):
            self._discovery_map[discovery_id] = comps
            self._blocks[discovery_id] = block
        # not published (disconnected/moving): the map keeps the old components,
        # so the next full republish computes the removal forms again

    def _publish_discovery_all(self) -> None:
        if not self.config.discovery_enabled:
            return  # e.g. a delayed republish that lands after an undo
        groups, counts = self._group_by_device()
        hid, hblock, hcomps = self._manager_discovery()
        groups[hid] = (hblock, hcomps)
        # via_device only towards devices that are announced too (the consumer
        # would create a nameless stub otherwise); parents before children
        for disc_id, (block, comps) in groups.items():
            if block.get("via_device") and block["via_device"] not in groups:
                block.pop("via_device", None)

        def depth(disc_id: str) -> int:
            d, cur = 0, disc_id
            while d < 10 and (nxt := groups[cur][0].get("via_device")) in groups and nxt != cur:
                d, cur = d + 1, nxt
            return d

        # An entity that moved to another device: the parent ignores its unique_id
        # in the new device's config while the old one still owns it, then the
        # old config's removal form deletes it.  So: vanished devices and configs
        # carrying removal forms first, and the devices that took entities over
        # are sent again a moment later.
        prev_owner = {eid: did for did, comps in self._discovery_map.items() for eid in comps}
        moved_in = {did for did, (_b, comps) in groups.items() if any(prev_owner.get(eid) not in (None, did) for eid in comps)}
        with_removals = {did for did in groups if set(self._discovery_map.get(did, {})) - set(groups[did][1])}
        for gone in set(self._discovery_map) - set(groups):
            if self._publish(self._discovery_topic(gone), None, qos=1):
                del self._discovery_map[gone]
        for disc_id in sorted(groups, key=lambda d: (d not in with_removals, depth(d))):
            block, comps = groups[disc_id]
            self._publish_device_discovery(disc_id, block, comps)
        if moved_in and self._connected:
            for did in moved_in:
                self._last_hash.pop(self._discovery_topic(did), None)
            self.hass.loop.call_soon_threadsafe(lambda: self.hass.loop.call_later(5, self._publish_discovery_all))
        self.stats["discovery_devices"] = len(groups)
        self.stats["discovery_components"] = sum(len(c) for _, c in groups.values())
        self.stats["discovery_mirrored"] = counts["mirrored"]
        self.stats["discovery_disabled"] = counts["disabled"]

    def _clear_stale_docs(self) -> int:
        """Blocking: retained entity documents of ours under <base>/ that this
        process no longer publishes (entities a new version dropped)."""
        base = self.base_topic
        try:
            found = self._retained_scan("stale", [(f"{base}/#", 1)])
            live = set(self._topics.values())
            keep_prefixes = (f"{base}/services/", f"{base}/cmd/", f"{base}/call/")
            stale = [t for t, p in found.items()
                     if t not in live and t not in (self._status_topic(), self._health_topic()) and not t.startswith(keep_prefixes)
                     and self._is_ours(t, p, base) and b"published_at" in p]
            self._clear_topics("stale", stale)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("stale document cleanup failed: %s", err)
            return 0
        if stale:
            _LOGGER.info("MQTT: cleared %s retained documents of entities that no longer exist", len(stale))
        return len(stale)

    def remove_discovered_component(self, discovery_id: str, entity_id: str, platform: str) -> bool:
        """Tell the consumer to drop ONE component of a device we announce
        (its removal form inside the device config), without touching its
        registry: an entity that vanished here while we were not looking."""
        if not self._connected:
            return False
        if not self.config.discovery_enabled:
            # discovery is off: never announce the device again, only remove it there
            self._last_hash.pop(self._discovery_topic(discovery_id), None)
            return self._publish(self._discovery_topic(discovery_id), None, qos=1)
        groups, _ = self._group_by_device()
        if discovery_id not in groups:
            # the whole device is gone here: an empty retained config removes it there
            self._last_hash.pop(self._discovery_topic(discovery_id), None)
            return self._publish(self._discovery_topic(discovery_id), None, qos=1)
        block, comps = groups[discovery_id]
        payload = {"device": block, "origin": disc.origin(self.prefix), "payload_available": "online", "payload_not_available": "offline",
                   "components": {_comp_key(eid): comp for eid, comp in comps.items()}}
        payload["components"][_comp_key(entity_id)] = {"platform": platform}
        return self._publish(self._discovery_topic(discovery_id), _dumps(payload), qos=1)

    def _clear_discovery_retained(self) -> int:
        """Blocking: every retained discovery config of THIS identity under
        <prefix>/device/+/config (the consumer removes the entities)."""
        base, prefix = self.base_topic, self.config.discovery_prefix
        try:
            found = self._retained_scan("undisc", [(f"{prefix}/device/+/config", 1)])
            # the manager device stays while manager_discovery wants it: removing it would drop the
            # consumer's customisations of those entities only to announce them again a minute later
            keep = {self._discovery_topic(f"{base}_manager")} if self.config.manager_discovery else set()
            ours = [t for t, p in found.items() if self._is_ours(t, p, base) and t not in keep]
            self._clear_topics("undisc", ours)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("discovery cleanup failed: %s", err)
            raise RuntimeError(f"discovery cleanup failed: {err}") from err  # never report "cleared 0" for a cleanup that did not run
        return len(ours)

    async def async_clear_identity(self, base_topic: str) -> int:
        """Everything retained of ours under one identity (uninstall of that
        integration): documents, services, health, status and its discovery
        configs, so the consumer removes the entities."""
        if not self.config.enabled:
            return 0  # MQTT is off: this process published nothing, and the broker may not even take our credentials
        n = await self.hass.async_add_executor_job(self._clear_retained_under, base_topic, self.config.discovery_prefix)
        if base_topic == self.base_topic:
            self._topics.clear()
            self._last_hash.clear()
            self._discovery_map.clear()
            self._blocks.clear()
        return n or 0

    async def async_clear_discovery(self) -> int:
        n = await self.hass.async_add_executor_job(self._clear_discovery_retained)
        self._discovery_map.clear()
        self._blocks.clear()
        # the configs are gone from the broker: an identical config published
        # later (Enable after Undo) must not be skipped as "unchanged"
        self._forget_hashes(f"{self.config.discovery_prefix}/device/")
        return n

    def _forget_hashes(self, topic_prefix: str) -> None:
        for t in [t for t in list(self._last_hash) if t.startswith(topic_prefix)]:
            del self._last_hash[t]

    async def _async_resync_excluded(self) -> None:
        """Retained documents of excluded integrations, and discovery configs of
        devices no longer announced, swept from the broker (what an exclusion
        while disconnected could not clear)."""
        base, prefix = self.base_topic, self.config.discovery_prefix
        excluded = set(self.config.exclude_integrations)
        try:
            found = await self.hass.async_add_executor_job(self._retained_scan, "resync", [(f"{base}/#", 1), (f"{prefix}/device/+/config", 1)])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MQTT: sweep of excluded integrations failed: %s", err)
            self._resync_excluded = True
            return
        docs, configs = [], []
        for topic, payload in found.items():
            if not self._is_ours(topic, payload, base):
                continue
            if topic.startswith(f"{prefix}/device/"):
                configs.append(topic)
                continue
            try:
                doc = json.loads(payload)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("integration") in excluded:
                docs.append(topic)
        if docs:
            await self.hass.async_add_executor_job(self._clear_topics, "resync", docs)
        if self.config.discovery_enabled:
            groups, _ = self._group_by_device()
            for topic in configs:
                if topic.split("/")[-2] not in groups and topic != self._discovery_topic(f"{base}_manager"):
                    self._publish(topic, None, qos=1)
        _LOGGER.info("MQTT: swept %s retained documents of excluded integrations", len(docs))

    async def async_after_start(self, res: dict[str, Any]) -> None:
        """After a successful start (UI or MQTT action): the identity follows
        the running integration; a version switch clears the retained
        documents of entities the new version no longer has (discovery is NOT
        reset: the consumer would delete and recreate every entity)."""
        await self.async_reconnect()
        if res.get("pre_update_backup"):
            res["stale_docs_cleared"] = await self.async_clear_stale_docs()

    async def async_clear_stale_docs(self) -> int:
        if not self._connected:
            return 0
        await self.async_republish_all()  # so _topics reflects the new version first
        return await self.hass.async_add_executor_job(self._clear_stale_docs)

    # ----- rules ---------------------------------------------------------------

    async def async_apply_rules(self) -> dict[str, int]:
        """After the rules changed: clear the retained documents (and the
        discovery components) of entities that are now excluded, then a full
        republish picks up names/flags."""
        cleared = 0
        for entity_id in list(self._topics):
            if self.rules.for_entity(entity_id).get("exclude"):
                self._clear(entity_id)  # discovery removal forms come from the full republish's diff
                cleared += 1
        n = await self.async_republish_all()
        return {"cleared": cleared, "republished": n}

    # ----- health ------------------------------------------------------------

    def _health_topic(self) -> str:
        return f"{self.base_topic}/health"

    def _manager_topic(self) -> str:
        return f"{self.base_topic}/manager"

    def build_health(self, grace: bool = True) -> dict[str, Any]:
        """The retained health document: what the installer knows about the
        running integration plus what this process sees of its entities.
        state: ok | degraded | error | stopped."""
        base = dict(self._health_provider() if self._health_provider else {"integration": None, "state": "stopped"})
        domain = base.get("integration")
        now = time.time()
        if domain:
            reg = er.async_get(self.hass)
            ids = {e.entity_id for e in reg.entities.values() if e.platform == domain and not e.disabled}
            # entities without a unique_id (YAML platforms) never reach the
            # registry but do live on the integration's entity platforms
            platforms = async_get_platforms(self.hass, domain)
            for platform in platforms:
                ids.update(platform.entities)
            has_entities = bool(ids) or any(p.entities for p in platforms)
            ids = sorted(ids)
            states = [st for st in (self.hass.states.get(i) for i in ids) if st is not None]
            unavailable = sum(1 for st in states if st.state == "unavailable")
            unknown = sum(1 for st in states if st.state == "unknown")
            last = max((st.last_updated.timestamp() for st in states), default=None)
            # last_reported moves whenever the integration writes a state, even an
            # unchanged value (a message received): a quiet bus with steady values
            # is alive, a silent integration is not
            reported = max((st.last_reported.timestamp() for st in states), default=None)
            base.update({
                "entities": len(ids), "entities_with_state": len(states), "entities_unavailable": unavailable, "entities_unknown": unknown,
                "last_state_update": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(last)) if last else None,
                "last_state_update_age_s": int(now - last) if last else None,
                "last_report": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(reported)) if reported else None,
                "last_report_age_s": int(now - reported) if reported else None,
            })
            rules = self._rules_provider(domain)
            base["rules"] = rules
            booting = grace and now - self._started_at < min(rules["stale_s"], HEALTH_GRACE_S)  # grace: entities fill in after the first traffic
            if base.get("state") == "ok" and not booting:
                if states and unavailable * 100 >= len(states) * rules["unavailable_pct"]:
                    base["state"], base["reason"] = "degraded", f"{unavailable} of {len(states)} entities unavailable"
                elif rules["mode"] == "periodic" and reported and now - reported > rules["stale_s"]:
                    base["state"], base["reason"] = "degraded", f"no entity report for {int(now - reported)} s"
                elif not states and has_entities:
                    base["state"], base["reason"] = "degraded", "no entities with a state yet"
                # an integration that exposes no entities at all (services only,
                # a hub without platforms) is judged by its config entries only
        base.update({
            "ha_version": ha_version_str,
            "manager_uptime_s": int(now - self._started_at),
            "mqtt_published": self.stats.get("published", 0),
            "notifications": _notification_count(self.hass),
            "base_topic": self.base_topic,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        return base

    def publish_health(self) -> dict[str, Any]:
        doc = self.build_health()
        self._health_last = doc
        self.stats["health_state"] = doc.get("state")
        if self.hass.state is not CoreState.running:
            # booting: the integration is still being set up, and its "error" would flip the consumer's
            # connectivity and the timeline for a moment; the retained verdict of the last run stays
            # until Home Assistant has started (then _on_started publishes at once)
            return doc
        prev = self._health_announced
        if prev is not None and doc.get("state") != prev:
            events.emit("health", f"{prev} → {doc.get('state')}" + (f": {doc.get('reason')}" if doc.get("reason") else ""),
                        integration=doc.get("integration"))
        self._health_announced = doc.get("state")
        if self._connected:
            self._publish(self._health_topic(), _dumps(doc))
            self.stats["health_published"] = doc["updated_at"]
        return doc

    @callback
    def _on_started(self, _event: Event) -> None:
        self._health_soon()

    @callback
    def _on_entry_changed(self, _change: Any, entry: Any) -> None:
        if entry.domain != "integration_manager":
            self._health_soon()

    @callback
    def _on_component_loaded(self, event: Event) -> None:
        if event.data.get("component") not in ("integration_manager", "persistent_notification"):
            self._health_soon()

    @callback
    def _health_soon(self) -> None:
        """A burst of entry state changes (not_loaded -> setup_in_progress -> loaded) gives one publication."""
        if self._health_soon_handle is not None:
            self._health_soon_handle.cancel()
        self._health_soon_handle = self.hass.loop.call_later(2, self.publish_health)

    async def _on_health_timer(self, _now) -> None:
        if self.manager is not None:
            await self.manager.async_sample()
        self.publish_health()
        self.publish_manager()
        self._publish_manager_discovery()

    def publish_manager(self) -> None:
        if self.manager is not None and self._connected and not self._moving:
            self._publish(self._manager_topic(), _dumps(self.manager.document()))

    async def async_publish_manager_result(self, result: dict[str, Any]) -> None:
        """The outcome of a manager action, delivered to the broker before
        whatever comes next (a reconnect, a restart) can drop it."""
        c = self._client
        if c is None or not self._connected:
            return
        info = c.publish(f"{self.base_topic}/manager/result", _dumps(result), qos=1, retain=False)
        doc = c.publish(self._manager_topic(), _dumps(self.manager.document()), qos=1, retain=True) if self.manager else None
        try:
            await self.hass.async_add_executor_job(lambda: [i.wait_for_publish(3) for i in (info, doc) if i is not None])
        except (RuntimeError, ValueError) as err:  # the connection dropped in between: the action itself still completes
            _LOGGER.warning("MQTT: the result of a manager action may not have reached the broker: %s", err)

    def _publish_manager_discovery(self) -> None:
        """The manager device on its own while entity discovery is off (with
        discovery on, _publish_discovery_all carries it)."""
        if self.config.discovery_enabled or not self._connected or self._moving:
            return
        mid, block, comps = self._manager_discovery()
        if self.config.manager_discovery:
            self._publish_device_discovery(mid, block, comps)
        elif not self._manager_absent_sent and self._publish(self._discovery_topic(mid), None, qos=1):
            # once per connection, no memory needed: turned off while disconnected
            # or across a restart still removes a device announced earlier
            self._discovery_map.pop(mid, None)
            self._blocks.pop(mid, None)
            self._manager_absent_sent = True

    def _manager_discovery(self) -> tuple[str, dict[str, Any], dict[str, dict[str, Any]]]:
        return disc.manager_device(
            self.base_topic, self.prefix,
            {"status": self._status_topic(), "health": self._health_topic(), "manager": self._manager_topic(), "cmd": self._manager_cmd_base()},
            (self._health_last or {}).get("integration"), self.manager.version if self.manager else "", self.config.manager_commands)

    # ----- service catalog ------------------------------------------------

    async def _publish_services(self) -> None:
        """One retained document per domain with every registered service,
        its fields/target/description; the consuming HA calls them through
        <base>/call/<domain>/<service>."""
        if not self._connected:
            return
        rows = await service_rows(self.hass)
        rows = [r for r in rows if r["domain"] not in self.config.exclude_integrations and r["domain"] not in CALL_DENY_DOMAINS]
        base = f"{self.base_topic}/services"
        current = {r["domain"] for r in rows}
        for r in rows:
            self._publish_if_changed(f"{base}/{r['domain']}", _dumps(
                {"domain": r["domain"], "custom": r["custom"], "call_topic": f"{self._call_base()}/{r['domain']}/<service>",
                 "result_topic": f"{self.base_topic}/result/{r['domain']}/<service>", "services": r["services"]}))
        for gone in self._services_published - current:
            self._publish(f"{base}/{gone}", None)
        self._services_published = current
        self.stats["services_published"] = sum(len(r["services"]) for r in rows)

    def _remove_component(self, entity_id: str) -> None:
        """HA's documented removal form: republish the device with the
        component reduced to {"platform": <domain>}; the next full republish
        drops the key entirely."""
        if not self._connected or self._moving:
            return  # the map keeps it; the next full republish sends the removal form
        for disc_id, comps in self._discovery_map.items():
            if entity_id not in comps:
                continue
            block = self._blocks.get(disc_id)
            if block is None:
                groups, _ = self._group_by_device()
                block = groups[disc_id][0] if disc_id in groups else None
            if block is None:
                return  # device vanished entirely: the full republish clears its config
            remaining = {eid: c for eid, c in comps.items() if eid != entity_id}
            # _publish_device_discovery adds the removal form for entity_id itself
            self._publish_device_discovery(disc_id, block, remaining)
            return

    # ----- events ----------------------------------------------------------

    @callback
    def _on_state(self, event: Event) -> None:
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if new is not None:
            # An event entity's state is the time of its last event: only a
            # changed state is a new event (restore at startup, availability
            # flaps and attribute-only writes must not replay it).
            is_event = new.domain == "event" and old is not None and old.state != new.state
            self._publish_state(new, is_event=is_event)
        elif old is not None:
            self._clear(old.entity_id)

    @callback
    def _on_registry(self, event: Event) -> None:
        action = event.data.get("action")
        entity_id = event.data.get("entity_id", "")
        if action == "remove":
            self._clear(entity_id)
            if self.config.discovery_enabled:
                self._remove_component(entity_id)
            return
        if action == "update" and "disabled_by" in (event.data.get("changes") or {}) and self._connected:
            entry = er.async_get(self.hass).async_get(entity_id)
            if entry is not None and entry.disabled:
                # the consumer would keep the last value forever (empty payloads are
                # ignored by its templates): remove the entity there instead
                self._clear(entity_id)
                if self.config.discovery_enabled:
                    self._remove_component(entity_id)
                return
        if action in ("create", "update") and self._connected:
            old_id = (event.data.get("changes") or {}).get("entity_id") or event.data.get("old_entity_id")
            if old_id and old_id != entity_id:
                self._clear(old_id)
                if self.config.discovery_enabled:
                    self._remove_component(old_id)
            state = self.hass.states.get(entity_id)
            if state is not None:
                self._publish_state(state)
            # Registry metadata changed (name, device, class...): refresh
            # discovery once the burst is over (an integration adding 100
            # entities fires 100 events), not per event.
            if self.config.discovery_enabled:
                if self._registry_timer is not None:
                    self._registry_timer.cancel()
                self._registry_timer = self.hass.loop.call_later(
                    3, lambda: self.hass.async_create_task(self._async_discovery_refresh())
                )

    def _clear(self, entity_id: str) -> None:
        if not entity_id:
            return
        topic = self._topics.pop(entity_id, None)
        if topic is None:
            integ = self._integration_of(entity_id)
            if integ is None:
                return  # never published by us (or already cleared with the registry entry): nothing to clear
            topic = self._topic_for(entity_id, integ)
        self._last_hash.pop(topic, None)  # a re-included entity must be published again, changed or not
        if self._publish(topic, None, qos=1):
            self.stats["cleared"] += 1
        else:
            self._pending_clears.add(topic)  # sent at the next republish

    async def _on_timer(self, _now) -> None:
        full = time.time() - self._last_full >= max(5, self.config.full_republish_interval_min) * 60
        await self.async_republish_all(full=full)

    @callback
    def _on_service_event(self, _event: Event) -> None:
        if self._services_timer is not None:
            self._services_timer.cancel()
        # A burst of registrations means an integration just finished
        # loading: republish everything (its entities appeared after our
        # connect-time run), not only the catalog.
        self._services_timer = self.hass.loop.call_later(
            5, lambda: self.hass.async_create_task(self._async_services_refresh())
        )

    async def _async_discovery_refresh(self) -> None:
        """A registry burst (rename, device change, an integration adding
        entities): discovery configs (unchanged ones are skipped by the
        content gate) and the documents of entities without one yet; not a
        full republish."""
        if not self._connected or self._moving:
            return
        for state in self.hass.states.async_all():
            if state.entity_id not in self._topics:
                self._publish_state(state)
        if self.config.discovery_enabled:
            self._publish_discovery_all()
        self.publish_health()
        self._publish_manager_discovery()

    async def _async_services_refresh(self) -> None:
        if self._connected and not self._moving:
            await self._publish_services()

    async def async_republish_all(self, full: bool = True) -> int:
        """full=True: every document, discovery and services (connect, hourly,
        explicit).  full=False: only documents whose content changed since
        they were last published (the periodic pass).  Long runs yield to
        the loop every batch so incoming data keeps flowing."""
        if not self._connected or self._moving:
            return 0
        for topic in list(self._pending_clears):
            if self._publish(topic, None, qos=1):
                self._pending_clears.discard(topic)
                self.stats["cleared"] += 1
        n = 0
        for i, state in enumerate(self.hass.states.async_all()):
            try:
                if self._publish_state(state, force=full):
                    n += 1
            except Exception as err:  # noqa: BLE001 - one odd entity must not stop health, discovery and services
                _LOGGER.warning("MQTT: document of %s not published: %s", state.entity_id, err)
            if i % REPUBLISH_BATCH == REPUBLISH_BATCH - 1:
                await asyncio.sleep(REPUBLISH_BATCH_PAUSE_S)
                if not self._connected or self._moving:
                    return n
        self.publish_health()
        if not full:
            self.stats["last_incremental_republish"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.stats["entities_last_incremental"] = n
            _LOGGER.debug("MQTT incremental republish: %s changed documents", n)
            return n
        self._last_full = time.time()
        self.stats["entities_last_run"] = n
        self.stats["last_full_republish"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if self.config.discovery_enabled:
            self._publish_discovery_all()
        self._publish_manager_discovery()
        self.publish_manager()
        if self._resync_excluded:
            self._resync_excluded = False
            await self._async_resync_excluded()
        await self._publish_services()
        _LOGGER.info(
            "MQTT full republish: %s entities, %s services, discovery: %s devices / %s components",
            n, self.stats["services_published"], self.stats["discovery_devices"], self.stats["discovery_components"],
        )
        return n

    def status(self) -> dict[str, Any]:
        return {
            **self.stats,
            "enabled": self.config.enabled,
            "host": self.config.host,
            "port": self.config.port,
            "base_topic": self.base_topic,
            "wanted_base_topic": self.wanted_base_topic,
            "identity_moved": self._connected and self.wanted_base_topic != self._live_base,
            "has_identity": bool(self.wanted_base_topic),
            "prefix": self.prefix,
            "force_base_topic": self.config.force_base_topic,
            "entities_total": len(self.hass.states.async_all()),
            # registry entries with no state (disabled or not yet added): no
            # document to publish, only announced through discovery
            "entities_registry_only": sum(
                1 for e in er.async_get(self.hass).entities.values()
                if self.hass.states.get(e.entity_id) is None and e.platform not in self.config.exclude_integrations
            ),
            "discovery_enabled": self.config.discovery_enabled,
            "discovery_prefix": self.config.discovery_prefix,
            "manager_discovery": self.config.manager_discovery,
            "manager_commands": self.config.manager_commands,
            "manager_topic": self._manager_topic(),
            "cmd_base": self._cmd_base(),
            "call_base": self._call_base(),
            "health_topic": self._health_topic(),
            "rules": len(self.rules.rules),
            "health": self._health_last or self.build_health(),
            "recent_commands": self.recent_commands(30),
            "history_size": len(self.history),
        }
