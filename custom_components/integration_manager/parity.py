"""Parity with the consuming ("parent") Home Assistant and the assisted
cutover.

The parent is reached over its websocket API with a long-lived token
(settings: parent_ha_url / parent_ha_token, token write-only).  Entities
created there by our discovery carry unique ids ``<prefix><entity_id>``,
which never change when the user renames them on the parent, so the
comparison is by unique id:

* missing  - announced by us, not present on the parent
* orphans  - present on the parent under our prefix, no longer announced
* matched  - both sides, with renames, disabled flags and state mismatches

Cutover = enable discovery, wait for the parent to create the entities,
compare; undo = discovery off + every retained discovery config of this
identity cleared (the parent removes the entities, the retained state
documents stay), except the manager device's while manager_discovery is on."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from aiohttp import ClientError, ClientWSTimeout, WSMsgType, web

from .ui import load_template, render
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import events
from .installer import Installer
from .mqtt_publisher import MqttPublisher
from .http_util import ManagerView, with_body

_LOGGER = logging.getLogger(__name__)
WS_TIMEOUT = 20
WS_MAX_MSG = 256 * 1024 * 1024  # a large parent's entity registry list is several MB (aiohttp's default cap is 4 MB)


class ParentCommandFailed(ValueError):
    """The parent answered the command with an error (an older parent without it, say): it was reached."""


class ParentHA:
    """Minimal websocket client for the parent HA (auth + a few commands)."""

    def __init__(self, hass: HomeAssistant, url: str, token: str) -> None:
        self.hass = hass
        self.url = url.rstrip("/")
        self.token = token

    async def commands(self, cmds: list[dict[str, Any]]) -> list[Any]:
        if not self.url or not self.token:
            raise ValueError("parent Home Assistant not configured (URL + long-lived token)")
        ws_url = self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
        session = async_get_clientsession(self.hass)
        out: list[Any] = []
        try:
            async with asyncio.timeout(WS_TIMEOUT * 4):
              async with session.ws_connect(ws_url, timeout=ClientWSTimeout(ws_receive=WS_TIMEOUT, ws_close=5), heartbeat=30,
                                      max_msg_size=WS_MAX_MSG) as ws:
                  first = await asyncio.wait_for(ws.receive_json(), WS_TIMEOUT)
                  if first.get("type") != "auth_required":
                      raise ValueError(f"unexpected handshake: {first.get('type')}")
                  await ws.send_json({"type": "auth", "access_token": self.token})
                  auth = await asyncio.wait_for(ws.receive_json(), WS_TIMEOUT)
                  if auth.get("type") != "auth_ok":
                      raise ValueError("the parent rejected the token (auth_invalid)")
                  for i, cmd in enumerate(cmds, start=1):
                      await ws.send_json({"id": i, **cmd})
                      while True:
                          msg = await asyncio.wait_for(ws.receive(), WS_TIMEOUT)
                          if msg.type != WSMsgType.TEXT:
                              detail = f": {ws.exception()}" if msg.type == WSMsgType.ERROR and ws.exception() else ""
                              raise ValueError(f"websocket closed ({WSMsgType(msg.type).name}{detail})")
                          data = json.loads(msg.data)
                          if data.get("id") != i or data.get("type") != "result":
                              continue
                          if not data.get("success"):
                              raise ParentCommandFailed(f"{cmd.get('type')}: {(data.get('error') or {}).get('message', 'failed')}")
                          out.append(data.get("result"))
                          break
        except (ClientError, OSError, asyncio.TimeoutError, TypeError, ValueError) as err:
            if isinstance(err, ParentCommandFailed) or (isinstance(err, ValueError) and "parent" in str(err)):
                raise
            raise ValueError(f"cannot reach the parent at {self.url}: {type(err).__name__}: {err}") from None
        return out


def _parent_client(hass: HomeAssistant, installer: Installer) -> ParentHA:
    st = installer.settings
    return ParentHA(hass, str(st.data.get("parent_ha_url") or ""), str(st.data.get("parent_ha_token") or ""))


async def compute_parity(hass: HomeAssistant, installer: Installer, publisher: MqttPublisher, light: bool = False) -> dict[str, Any]:
    """light=True (the cutover wait, polled every 5 s): only the parent's
    entity registry, no states/devices (on a big parent get_states is MB)."""
    prefix = publisher.prefix
    client = _parent_client(hass, installer)
    t0 = time.monotonic()
    if light:
        p_entities, p_config = await client.commands([{"type": "config/entity_registry/list"}, {"type": "get_config"}])
        p_devices, p_states = [], []
    else:
        p_entities, p_devices, p_states, p_config = await client.commands([
            {"type": "config/entity_registry/list"},
            {"type": "config/device_registry/list"},
            {"type": "get_states"},
            {"type": "get_config"},
        ])
    ms = int((time.monotonic() - t0) * 1000)
    mqtt_loaded = "mqtt" in (p_config.get("components") or [])
    from homeassistant.const import Platform

    from .discovery import manager_device

    # the publisher announces every state, non-platform domains (zone, person, ...) mirrored as sensors
    platforms = {p.value for p in Platform} | {eid.split(".", 1)[0] for eid in hass.states.async_entity_ids()}
    manager_uids = {c["unique_id"][len("k_"):] for c in manager_device("k", "k_", dict.fromkeys(("status", "health", "manager", "cmd"), "t"), "x", "", True)[2].values()}

    def _ours_uid(uid: Any) -> bool:
        # exactly this identity: hass_a must not claim hass_a_b's entities, whose ids start with hass_a_ too
        if not isinstance(uid, str) or not uid.startswith(prefix):
            return False
        rest = uid[len(prefix):]
        m = re.fullmatch(r"([a-z_]+)\.[a-z0-9_]+", rest)
        return (bool(m) and m.group(1) in platforms) or rest in manager_uids

    def _ours_device(ident: str) -> bool:
        rest = ident[len(prefix):] if ident.startswith(prefix) else None
        return rest is not None and (bool(re.fullmatch(r"[0-9a-f]{32}", rest)) or rest == "manager"
                                     or bool(re.fullmatch(r"[a-z0-9_]+_nodevice", rest)))  # <integration>_nodevice; the exact prefix scopes it

    parent_by_uid = {e["unique_id"]: e for e in p_entities if e.get("platform") == "mqtt" and _ours_uid(e.get("unique_id"))}
    p_state = {s["entity_id"]: s for s in p_states}
    p_dev = {d["id"]: d for d in p_devices}

    ours: dict[str, dict[str, Any]] = {}
    announces_manager = publisher.config.discovery_enabled or publisher.config.manager_discovery
    for dev in publisher.discovery_preview():
        if dev["discovery_id"] == f"{publisher.base_topic}_manager" and not announces_manager:
            continue  # the manager device is not announced: it cannot be missing on the consumer
        for eid, comp in dev["components"].items():
            ours[comp["unique_id"]] = {"entity_id": eid, "name": comp.get("name"), "platform": comp.get("platform"),
                                       "announced_entity_id": comp.get("default_entity_id") or eid,
                                       "enabled_by_default": comp.get("enabled_by_default", True), "device": dev["device"].get("name"),
                                       "discovery_id": dev["discovery_id"]}
    missing, matched = [], []
    for uid, o in ours.items():
        st = hass.states.get(o["entity_id"])
        ours_state = st.state if st else None
        pe = parent_by_uid.get(uid)
        if pe is None:
            missing.append({**o, "unique_id": uid, "state": ours_state})
            continue
        ps = p_state.get(pe["entity_id"])
        parent_state = ps["state"] if ps else None
        matched.append({
            "unique_id": uid, "entity_id": o["entity_id"], "parent_entity_id": pe["entity_id"],
            "renamed": pe["entity_id"] != o["announced_entity_id"], "parent_name": pe.get("name"), "parent_original_name": pe.get("original_name"),
            "parent_disabled_by": pe.get("disabled_by"), "parent_area": pe.get("area_id"),
            "parent_device": (p_dev.get(pe.get("device_id") or "") or {}).get("name_by_user") or (p_dev.get(pe.get("device_id") or "") or {}).get("name"),
            "state": ours_state, "parent_state": parent_state,
            "state_differs": ours_state is not None and parent_state is not None and parent_state != ours_state,
            "parent_unavailable": parent_state == "unavailable" and ours_state not in (None, "unavailable"),
        })
    orphans = []
    for uid, pe in parent_by_uid.items():
        if uid in ours:
            continue
        pdev = p_dev.get(pe.get("device_id") or "") or {}
        disc_id = next((i[1] for i in (pdev.get("identifiers") or []) if isinstance(i, list) and len(i) == 2 and _ours_device(str(i[1]))), None)
        orphans.append({"unique_id": uid, "parent_entity_id": pe["entity_id"], "parent_name": pe.get("name") or pe.get("original_name"),
                        "parent_disabled_by": pe.get("disabled_by"), "parent_domain": pe["entity_id"].split(".", 1)[0],
                        "our_entity_id": uid[len(prefix):], "discovery_id": disc_id})
    return {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "parent_url": client.url, "parent_version": p_config.get("version"), "light": light,
        "parent_mqtt_loaded": mqtt_loaded, "prefix": prefix, "round_trip_ms": ms, "discovery_enabled": publisher.config.discovery_enabled,
        "ours": len(ours), "parent": len(parent_by_uid),
        "summary": {"matched": len(matched), "missing": len(missing), "orphans": len(orphans),
                    "renamed": sum(1 for m in matched if m["renamed"]), "state_differs": sum(1 for m in matched if m["state_differs"]),
                    "parent_unavailable": sum(1 for m in matched if m["parent_unavailable"]),
                    "parent_disabled": sum(1 for m in matched if m["parent_disabled_by"])},
        "missing": sorted(missing, key=lambda x: x["entity_id"]), "orphans": sorted(orphans, key=lambda x: x["parent_entity_id"]),
        "matched": sorted(matched, key=lambda x: x["entity_id"]),
    }


class ParityView(ManagerView):
    """GET /api/parity: the comparison; POST /api/parity/remove_orphans."""

    url = "/api/parity"

    def __init__(self, hass: HomeAssistant, installer: Installer, publisher: MqttPublisher) -> None:
        self.hass = hass
        self.installer = installer
        self.publisher = publisher

    async def get(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            # it connects to the parent Home Assistant with the stored token: not something any web page may trigger
            return self.json_message("X-Requested-With: fetch required", status_code=400)
        try:
            parity = await compute_parity(self.hass, self.installer, self.publisher, light=request.query.get("light") == "1")
        except ValueError as err:
            return self.json({"ok": False, "error": str(err)})
        except Exception as err:  # noqa: BLE001
            return self.json({"ok": False, "error": f"{type(err).__name__}: {err}"})
        return self.json({"ok": True, **parity})


class ParityActionView(ManagerView):
    url = "/api/parity/{action}"

    def __init__(self, hass: HomeAssistant, installer: Installer, publisher: MqttPublisher) -> None:
        self.hass = hass
        self.installer = installer
        self.publisher = publisher

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], action: str) -> web.Response:
        if action == "test":
            try:
                (cfg,) = await _parent_client(self.hass, self.installer).commands([{"type": "get_config"}])
            except Exception as err:  # noqa: BLE001
                return self.json({"ok": False, "error": str(err)})
            return self.json({"ok": True, "version": cfg.get("version"), "location": cfg.get("location_name"),
                              "mqtt_loaded": "mqtt" in (cfg.get("components") or [])})
        if action == "remove_orphans":
            ids = body.get("entity_ids")
            if not isinstance(ids, list) or not all(isinstance(x, str) and "." in x for x in ids) or len(ids) > 500:
                return self.json({"ok": False, "error": "entity_ids (list) required"})
            # only entities the last comparison called orphans of OUR prefix: never anything else on the parent
            try:
                parity = await compute_parity(self.hass, self.installer, self.publisher)
            except Exception as err:  # noqa: BLE001
                return self.json({"ok": False, "error": str(err)})
            by_id = {o["parent_entity_id"]: o for o in parity["orphans"]}
            todo = [by_id[x] for x in ids if x in by_id]
            if not todo:
                return self.json({"ok": True, "removed": [], "note": "nothing to remove (not orphans of ours)"})
            if not self.publisher.config.discovery_enabled:
                # one component is removed by re-announcing its device without it, which discovery off forbids (the
                # manager device excepted while manager_discovery announces it); the empty config left instead
                # removes the whole device on the parent, siblings included
                manager_id = f"{self.publisher.base_topic}_manager"
                blocked = [o["parent_entity_id"] for o in todo
                           if not (self.publisher.config.manager_discovery and o.get("discovery_id") == manager_id)]
                if blocked:
                    return self.json({"ok": False, "error": f"discovery is off: {', '.join(blocked[:5])}{'…' if len(blocked) > 5 else ''} "
                                                            "can only be removed on its own while discovery is on (turning discovery "
                                                            "off removes every entity this container announced from the main Home Assistant)"})
            # Removing the entity from the parent's registry would make its MQTT
            # integration publish an empty DEVICE config (removing every sibling).
            # The documented way is the removal form for that one component.
            removed, skipped = [], []
            for o in todo:
                if o.get("discovery_id") and self.publisher.remove_discovered_component(o["discovery_id"], o["our_entity_id"], o["parent_domain"]):
                    removed.append(o["parent_entity_id"])
                else:
                    skipped.append(o["parent_entity_id"])
            return self.json({"ok": True, "removed": removed, "skipped": skipped + [x for x in ids if x not in by_id],
                              "note": "removal forms published; the parent drops them within seconds"})
        return self.json_message("unknown action", status_code=400)


class CutoverView(ManagerView):
    """POST /api/cutover/{enable|undo|status}: the assisted cutover."""

    url = "/api/cutover/{action}"

    def __init__(self, hass: HomeAssistant, installer: Installer, publisher: MqttPublisher) -> None:
        self.hass = hass
        self.installer = installer
        self.publisher = publisher

    def _status(self) -> dict[str, Any]:
        h = self.publisher.build_health()
        st = self.installer.settings
        return {
            "running": self.installer.running, "tag": self.installer.running_tag,
            "health": h.get("state"), "health_reason": h.get("reason", ""),
            "mqtt_connected": bool(self.publisher.stats.get("connected")),
            "discovery_enabled": self.publisher.config.discovery_enabled,
            "discovery_devices": self.publisher.stats.get("discovery_devices", 0),
            "parent_configured": bool(st.data.get("parent_ha_url")) and bool(st.data.get("parent_ha_token")),
            "smoke_pending": bool(self.installer.smoke.get("pending")),
        }

    async def _parent_blockers(self, domain: str, components: list[str]) -> list[str]:
        """The main Home Assistant must not hold the integration any more: a config entry (enabled or disabled)
        keeps its entities in the registry, and an entity id still registered there sends the MQTT entity to <id>_2."""
        out: list[str] = []
        client = _parent_client(self.hass, self.installer)
        try:
            (entries,) = await client.commands([{"type": "config_entries/get", "domain": domain}])
            if entries:
                out.append(f"the main Home Assistant still has {len(entries)} config entr{'y' if len(entries) == 1 else 'ies'} of {domain}: "
                           "remove the integration there first (disabling keeps its entity ids, and the MQTT entities would get _2 ids)")
        except ParentCommandFailed:  # an older parent without the command: the loaded components tell less, but something
            if domain in components:
                out.append(f"the main Home Assistant still runs {domain}: remove it there first, or every entity exists twice")
        except Exception as err:  # noqa: BLE001 - a check that did not run is not a check that passed
            out.append(f"the config entries of {domain} on the main Home Assistant could not be checked ({err})")
        try:
            (registry,) = await client.commands([{"type": "config/entity_registry/list"}])
        except Exception as err:  # noqa: BLE001
            out.append(f"the entity ids registered on the main Home Assistant could not be checked ({err})")
            return out
        held = {e.get("entity_id"): e.get("platform") for e in registry if e.get("platform") != "mqtt"}
        announced = sorted({c.get("default_entity_id") for dev in self.publisher.discovery_preview() for c in dev["components"].values()
                            if c.get("default_entity_id")})
        taken = [eid for eid in announced if eid in held]
        if taken:
            out.append(f"{len(taken)} entity id{'s' if len(taken) > 1 else ''} the container announces {'are' if len(taken) > 1 else 'is'} still registered "
                       f"on the main Home Assistant ({', '.join(taken[:3])}{'…' if len(taken) > 3 else ''}): the MQTT entities would get _2 ids; "
                       "remove the integration there first")
        return out

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any], action: str) -> web.Response:
        if action == "status":
            return self.json({"ok": True, **self._status()})
        s = self._status()
        if action == "enable":
            problems = []
            if not s["running"]:
                problems.append("no integration is running")
            if s["health"] not in ("ok", "degraded"):
                problems.append(f"health is {s['health']}: {s['health_reason']}")
            if not s["mqtt_connected"]:
                problems.append("MQTT is not connected")
            # force skips only the checks on the main Home Assistant, and says so in the answer and on the timeline
            forced = bool(body.get("force")) and s["parent_configured"]
            if s["parent_configured"] and not forced:
                try:
                    (cfg,) = await _parent_client(self.hass, self.installer).commands([{"type": "get_config"}])
                except Exception as err:  # noqa: BLE001
                    problems.append(f"the main Home Assistant could not be checked ({err})")
                else:
                    components = cfg.get("components") or []
                    if "mqtt" not in components:
                        problems.append("the main Home Assistant has no MQTT integration loaded")
                    if s["running"]:
                        problems += await self._parent_blockers(s["running"], components)
            if problems:
                return self.json({"ok": False, "error": "; ".join(problems)})
            if not s["discovery_enabled"]:
                await self.publisher.async_save({"discovery_enabled": True})
                await self.publisher.async_reload_config()  # no reconnect: the parent would see every entity flap to unavailable
            n = await self.publisher.async_republish_all(full=True)
            events.emit("cutover", f"discovery enabled on the parent: {n} documents republished"
                        + (" (forced: the checks on the main Home Assistant were skipped)" if forced else ""), forced=forced)
            return self.json({"ok": True, "republished": n, "forced": forced, **self._status()})
        if action == "undo":
            if s["discovery_enabled"]:
                await self.publisher.async_save({"discovery_enabled": False})
                await self.publisher.async_reload_config()
            try:
                cleared = await self.publisher.async_clear_discovery()
            except RuntimeError as err:
                return self.json({"ok": False, "error": f"discovery disabled, but the retained configs could not be cleared: {err}; "
                                                    "retried at the next MQTT connection", **self._status()})
            self.publisher.undiscover_done()
            # the manager device is not entity discovery: it follows manager_discovery, and removing it here would
            # only drop its customisations on the main HA before the next health tick announces it again
            kept = bool(self.publisher.config.manager_discovery)
            events.emit("cutover", f"undo: discovery disabled, {cleared} retained configs cleared"
                        + (" (the manager device stays: manager_discovery is on)" if kept else ""))
            return self.json({"ok": True, "cleared_discovery_configs": cleared, "manager_device_kept": kept, **self._status()})
        return self.json_message("unknown action", status_code=400)


PARITY_HTML = load_template("parity")


class ParityPageView(ManagerView):
    url = "/parity"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(PARITY_HTML, "/parity"), content_type="text/html")
