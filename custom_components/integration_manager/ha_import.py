"""Import an integration's configuration from a standard Home Assistant
backup (.tar as produced by Settings -> System -> Backups), so no token
or API access to the other instance is needed.

What the backup gives us (inner ``homeassistant.tar.gz``, optionally
encrypted with the instance's backup encryption key): ``.storage/``,
i.e. ``core.config_entries`` (the entry with its data AND options, which
HA's APIs never expose), ``core.entity_registry`` / ``core.device_registry``
(ids, custom names, disabled flags: the alignment we otherwise do by
hand), and the integration's own store files (e.g. ``.storage/<domain>``).

Flow: upload -> inspect (needs the password if the backup is protected)
-> pick the config entry, edit data/options as JSON -> apply: the entry
is created in-process (``hass.config_entries.async_add`` sets it up),
the registry alignment map is stored and applied live as the integration
creates its entities, optionally the store files are copied first.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tarfile
import tempfile
import time
import zipfile
from types import MappingProxyType
from typing import Any

import backupkit
import securetar
from jsonio import write_json

from homeassistant import loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.components import persistent_notification as pn
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import events

_LOGGER = logging.getLogger(__name__)

STATE_DIR = "integration_manager"
IMPORT_TAR = os.path.join(STATE_DIR, "import.tar")
EXTRACT_DIR = os.path.join(STATE_DIR, "import-extracted")
SUMMARY_FILE = os.path.join(EXTRACT_DIR, "summary.json")
MAP_FILE = os.path.join(STATE_DIR, "import-map.json")
REBUILD_FILE = os.path.join(STATE_DIR, "rebuild-pending.json")  # read by entrypoint.py too
REBUILD_TYPE = "ha-downgrade-rebuild"
CORE_STORES = (".storage/core.config_entries", ".storage/core.entity_registry", ".storage/core.device_registry")


def _strip(name: str) -> str:
    n = name
    while n.startswith("./"):
        n = n[2:]
    n = n.lstrip("/")
    if n.startswith("data/"):
        n = n[5:]
    return n


def _wanted(rel: str, domains: set[str]) -> bool:
    """Only the three registries and the store files of importable domains:
    the rest of the other instance's .storage (auth, cloud, every
    integration's tokens) must never land on this volume."""
    if rel in CORE_STORES:
        return True
    if not rel.startswith(".storage/") or rel.count("/") != 1:
        return False
    f = rel[len(".storage/"):]
    return any(f == d or f.startswith(d + ".") or f.startswith(d + "_") for d in domains) and ".bak" not in f and "_backup" not in f


def inspect_backup(config_dir: str, password: str | None, domains: set[str]) -> dict[str, Any]:
    """Extract what the import needs from the backup into EXTRACT_DIR and
    return a summary.  `domains` = integrations installable here (the
    registry); only their config data/options are kept.  Blocking."""
    tar_path = os.path.join(config_dir, IMPORT_TAR)
    out_dir = os.path.join(config_dir, EXTRACT_DIR)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    try:
        return _inspect(config_dir, tar_path, out_dir, password, domains)
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)  # never leave a partial extraction
        raise


MAX_EXTRACT_BYTES = 2 * 1024**3  # what an import may unpack onto the volume (registries and the integration's stores)


def _inspect(config_dir: str, tar_path: str, out_dir: str, password: str | None, domains: set[str]) -> dict[str, Any]:
    with tarfile.open(tar_path) as outer:
        names = outer.getnames()
        meta_name = next((n for n in names if _strip(n) == "backup.json"), None)
        if meta_name is None:
            raise ValueError("not a Home Assistant backup (no backup.json)")
        meta = json.load(outer.extractfile(meta_name))
        compressed = bool(meta.get("compressed", True))
        inner_name = next((n for n in names if _strip(n) == f"homeassistant.tar{'.gz' if compressed else ''}"), None)
        if inner_name is None:
            raise ValueError("backup has no homeassistant.tar.gz (add-on only / partial without HA config?)")
        if meta.get("protected") and not password:
            raise ValueError("this backup is encrypted: enter the backup encryption key (emergency kit)")
        with tempfile.NamedTemporaryFile(dir=out_dir, suffix=".inner", delete=False) as tmp:
            shutil.copyfileobj(outer.extractfile(inner_name), tmp)
            inner_path = tmp.name
    too_big = False
    try:
        try:
            with securetar.SecureTarFile(inner_path, gzip=compressed, password=(password or None) if meta.get("protected") else None) as tar:
                total = 0
                for member in tar:
                    rel = _strip(member.name)
                    if not member.isfile() or ".." in rel.split("/") or not _wanted(rel, domains):
                        continue
                    total += member.size
                    if total > MAX_EXTRACT_BYTES:
                        too_big = True
                        break
                    dest = os.path.join(out_dir, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    src = tar.extractfile(member)
                    with open(dest, "wb") as fh:
                        shutil.copyfileobj(src, fh)
        except Exception as err:  # noqa: BLE001 - wrong key / corrupt archive
            raise ValueError(f"cannot read the backup contents (wrong encryption key?): {type(err).__name__}: {err}") from None
    finally:
        os.remove(inner_path)
    if too_big:
        raise ValueError(f"the configuration in this backup unpacks to more than {MAX_EXTRACT_BYTES // 1024**3} GB: not imported")
    summary = _summarize(out_dir, meta, domains)
    # every integration's credentials of the other instance: the summary kept
    # what the installed domain needs, the rest must not stay on this volume
    try:
        os.remove(os.path.join(out_dir, ".storage", "core.config_entries"))
    except OSError:
        pass
    with open(os.path.join(config_dir, SUMMARY_FILE), "w", encoding="utf-8") as fh:
        json.dump(summary, fh)
    # The archive is not needed any more (everything useful is in EXTRACT_DIR);
    # it is the other instance's full backup, so do not keep it around.
    try:
        os.remove(tar_path)
    except OSError:
        pass
    return summary


def _device_entries(dv: dict[str, Any]) -> list[str]:
    """Device registry formats differ: a `config_entries` list (older) or a
    single `config_entry_id` / `primary_config_entry` (2026.8+)."""
    out = list(dv.get("config_entries") or [])
    for k in ("config_entry_id", "primary_config_entry"):
        if dv.get(k) and dv[k] not in out:
            out.append(dv[k])
    return out


def _load_store(out_dir: str, name: str) -> dict[str, Any]:
    try:
        with open(os.path.join(out_dir, ".storage", name), encoding="utf-8") as fh:
            return json.load(fh).get("data") or {}
    except (OSError, ValueError):
        return {}


def _summarize(out_dir: str, meta: dict[str, Any], domains: set[str]) -> dict[str, Any]:
    entries = _load_store(out_dir, "core.config_entries").get("entries") or []
    ents = _load_store(out_dir, "core.entity_registry").get("entities") or []
    _dstore = _load_store(out_dir, "core.device_registry")
    devs = list(_dstore.get("devices") or []) + list(_dstore.get("child_devices") or [])  # 2026.9+: zones as child devices
    storage_files = sorted(os.listdir(os.path.join(out_dir, ".storage"))) if os.path.isdir(os.path.join(out_dir, ".storage")) else []
    by_domain: dict[str, Any] = {}
    for e in entries:
        d = by_domain.setdefault(e["domain"], {"entries": [], "entities": 0, "devices": 0, "storage_files": [], "importable": e["domain"] in domains})
        rec = {k: e.get(k) for k in ("entry_id", "title", "unique_id", "source", "pref_disable_new_entities", "pref_disable_polling",
                                     "disabled_by")}
        rec["version"] = int(e.get("version") or 1) if str(e.get("version") or "1").isdigit() else 1
        rec["minor_version"] = int(e.get("minor_version") or 1) if str(e.get("minor_version") or "1").isdigit() else 1
        if e["domain"] in domains:  # secrets of every other integration stay out of the summary
            rec["data"], rec["options"], rec["subentries"] = e.get("data") or {}, e.get("options") or {}, e.get("subentries") or []
        d["entries"].append(rec)
    for x in ents:
        if x.get("platform") in by_domain:
            by_domain[x["platform"]]["entities"] += 1
    for dv in devs:
        for cid in _device_entries(dv):
            dom = next((e["domain"] for e in entries if e["entry_id"] == cid), None)
            if dom in by_domain:
                by_domain[dom]["devices"] += 1
    for dom, d in by_domain.items():
        d["storage_files"] = [f for f in storage_files
                              if (f == dom or f.startswith(dom + ".") or f.startswith(dom + "_")) and ".bak" not in f and "_backup" not in f]
    return {
        "name": meta.get("name"), "date": meta.get("date"), "protected": bool(meta.get("protected")),
        "ha_version": (meta.get("homeassistant") or {}).get("version"), "type": meta.get("type"),
        "domains": by_domain, "storage_files": storage_files,
    }


def load_summary(config_dir: str) -> dict[str, Any] | None:
    try:
        with open(os.path.join(config_dir, SUMMARY_FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def clear_extracted(config_dir: str) -> None:
    shutil.rmtree(os.path.join(config_dir, EXTRACT_DIR), ignore_errors=True)


def clear(config_dir: str) -> None:
    shutil.rmtree(os.path.join(config_dir, EXTRACT_DIR), ignore_errors=True)
    try:
        os.remove(os.path.join(config_dir, IMPORT_TAR))
    except OSError:
        pass


# ----- registry alignment ---------------------------------------------------


def _build_map(out_dir: str, domain: str, entry_id: str) -> dict[str, Any]:
    ents = _load_store(out_dir, "core.entity_registry").get("entities") or []
    _dstore = _load_store(out_dir, "core.device_registry")
    devs = list(_dstore.get("devices") or []) + list(_dstore.get("child_devices") or [])
    emap = {}
    for x in ents:
        if x.get("platform") != domain or x.get("config_entry_id") != entry_id or x.get("unique_id") is None:
            continue
        emap[_ekey(x["entity_id"].split(".", 1)[0], x["unique_id"])] = {
            "entity_id": x["entity_id"], "name": x.get("name"), "icon": x.get("icon"),
            "disabled_by": x.get("disabled_by"), "hidden_by": x.get("hidden_by")}
    dmap = {}
    for dv in devs:
        if entry_id not in _device_entries(dv):
            continue
        for ident in dv.get("identifiers") or []:
            dmap[json.dumps(list(ident))] = {"name_by_user": dv.get("name_by_user"), "disabled_by": dv.get("disabled_by")}
    return {"domain": domain, "entities": emap, "devices": dmap}


def _ekey(entity_domain: str, unique_id: Any) -> str:
    """Map key of an entity: a unique_id is only unique per platform AND
    entity domain (a sensor and a switch of one device may share it)."""
    return f"{entity_domain}:{unique_id}"


def _ekey_lookup(entities: dict[str, Any], entity_domain: str, unique_id: Any) -> str | None:
    """The stored key for this entity: the domain-qualified one, or a
    bare unique_id written by an older map when it is unambiguous."""
    key = _ekey(entity_domain, unique_id)
    if key in entities:
        return key
    return str(unique_id) if str(unique_id) in entities else None


def _domains_of(entity_id: str) -> list[str]:
    return [entity_id.split(".", 1)[0]]


class RegistryAligner:
    """Applies the stored import maps (one per domain): now for existing
    registry entries, and live for entities/devices the integration creates
    afterwards.  File: {"domains": {<domain>: {"entities": {"<entity domain>:<unique_id>": ...},
    "devices": {identifier_json: ...}}}}."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.path = hass.config.path(MAP_FILE)
        self.maps: dict[str, dict[str, Any]] = self._load()
        self._pending: set[str] = set()
        self._save_handle = None

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        if isinstance(raw.get("domains"), dict):
            return {d: {"entities": m.get("entities") or {}, "devices": m.get("devices") or {}} for d, m in raw["domains"].items()}
        if raw.get("domain"):  # pre-0.6 single-domain file
            return {raw["domain"]: {"entities": raw.get("entities") or {}, "devices": raw.get("devices") or {}}}
        return {}

    def _save(self) -> None:
        """Every save goes through the debounced executor write: nothing
        writes the map on the event loop (the STARTED prune used to)."""
        self._request_save()

    @property
    def pending_counts(self) -> tuple[int, int]:
        return (sum(len(m["entities"]) for m in self.maps.values()), sum(len(m["devices"]) for m in self.maps.values()))

    def merge_map(self, new_map: dict[str, Any]) -> None:
        """Merge one config entry's alignment map into its domain's map,
        keeping other entries/domains pending (imports of several entries
        or integrations at once)."""
        m = self.maps.setdefault(new_map["domain"], {"entities": {}, "devices": {}})
        m["entities"].update(new_map.get("entities") or {})
        m["devices"].update(new_map.get("devices") or {})
        self._save()

    def drop_keys(self, domain: str, entities: list[str], devices: list[str]) -> None:
        """Undo one merge_map (a failed import) without touching what other
        entries/imports left pending for the same domain."""
        m = self.maps.get(domain)
        if not m:
            return
        for k in entities:
            m["entities"].pop(k, None)
        for k in devices:
            m["devices"].pop(k, None)
        self._save()

    @callback
    def _request_save(self) -> None:
        """Debounced save from event callbacks: snapshot on the loop, write
        in the executor (a large map at startup would otherwise block
        the loop on every first state)."""
        if self._save_handle is not None:
            self._save_handle.cancel()

        def _flush() -> None:
            self._save_handle = None
            self.maps = {d: m for d, m in self.maps.items() if m.get("entities") or m.get("devices")}
            snapshot = json.dumps({"domains": self.maps}, indent=1) if self.maps else None
            self._seq = getattr(self, "_seq", 0) + 1
            self.hass.async_add_executor_job(self._write, snapshot, self._seq)  # schedules itself; a future, not a coroutine

        self._save_handle = self.hass.loop.call_later(2, _flush)

    def _write(self, snapshot: str | None, seq: int | None = None) -> None:
        if seq is not None and seq < getattr(self, "_written_seq", 0):
            return  # a newer snapshot already landed
        if seq is not None:
            self._written_seq = seq
        if snapshot is None:
            try:
                os.remove(self.path)
            except OSError:
                pass
            return
        tmp = f"{self.path}.{os.getpid()}.{id(snapshot)}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(snapshot)
        os.replace(tmp, self.path)

    def drop_domain(self, domain: str) -> None:
        self.maps.pop(domain, None)
        self._save()

    @callback
    def async_start(self) -> None:
        self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_entity)
        self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._on_device)
        self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state)
        if self.maps:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started)

    @callback
    def _on_started(self, _event: Event) -> None:
        self.prune_satisfied()

    def prune_satisfied(self) -> int:
        """Drop map entries the registry already satisfies (an entity that was
        disabled by the alignment never gets a state again, so the
        first-state path cannot pop it; same after a restart)."""
        ereg = er.async_get(self.hass)
        dreg = dr.async_get(self.hass)
        dropped = 0
        for domain, m in self.maps.items():
            for key, want in list(m["entities"].items()):
                eid = None
                for platform_domain in _domains_of(want["entity_id"]):
                    # "<entity domain>:<unique_id>" keys; an older map holds bare unique_ids (which may contain ':' themselves)
                    uid = key[len(platform_domain) + 1:] if key.startswith(platform_domain + ":") else key
                    eid = ereg.async_get_entity_id(platform_domain, domain, uid)
                    if eid:
                        break
                entry = ereg.async_get(eid) if eid else None
                if entry is None:
                    continue
                same_id = entry.entity_id == want["entity_id"]
                same_name = not want.get("name") or entry.name == want["name"]
                same_disabled = (want.get("disabled_by") == "user") == (entry.disabled_by == er.RegistryEntryDisabler.USER)
                same_icon = not want.get("icon") or entry.icon == want["icon"]
                same_hidden = (want.get("hidden_by") == "user") == (entry.hidden_by == er.RegistryEntryHider.USER)
                if same_id and same_name and same_disabled and same_icon and same_hidden:
                    m["entities"].pop(key, None)
                    dropped += 1
            for key, want in list(m["devices"].items()):
                ident = tuple(json.loads(key))
                # async_get_device(identifiers=) is deprecated (identifiers are
                # per config entry now); our map has no entry id, so scan
                found = dreg.async_get_devices(identifiers={ident})
                dev = found[0] if found else None
                if dev is None and hasattr(dreg, "async_get_child_device_by_identifier"):
                    for entry in self.hass.config_entries.async_entries(domain):
                        dev = dreg.async_get_child_device_by_identifier(ident, entry.entry_id)
                        if dev:
                            break
                if dev is not None and (not want.get("name_by_user") or dev.name_by_user == want["name_by_user"]):
                    m["devices"].pop(key, None)
                    dropped += 1
        if dropped:
            self._save()
        return dropped

    @callback
    def _on_entity(self, event: Event) -> None:
        # The registry fires "create" synchronously from async_get_or_create,
        # BEFORE the entity platform finishes adding the entity with the entry
        # it just got; renaming here would leave the state machine on the old
        # id.  Wait for the entity's first state instead: HA then handles a
        # registry rename properly (remove + re-add under the new id).
        if event.data.get("action") != "create":
            return
        platform = event.data["entity_id"].split(".", 1)[0]  # cheap pre-filter; the real check is by registry platform
        entry = er.async_get(self.hass).async_get(event.data["entity_id"])
        m = self.maps.get(entry.platform) if entry else None
        if m and m["entities"]:
            self._pending.add(event.data["entity_id"])
        elif not any(mm["entities"] for mm in self.maps.values()):
            self._pending.clear()

    @callback
    def _on_state(self, event: Event) -> None:
        eid = event.data.get("entity_id")
        if eid in self._pending and event.data.get("new_state") is not None:
            self._pending.discard(eid)
            if self.align_entity(eid):
                self._request_save()

    @callback
    def _on_device(self, event: Event) -> None:
        if event.data.get("action") == "create" and any(m["devices"] for m in self.maps.values()):
            if self.align_device(event.data["device_id"]):
                self._request_save()

    def align_entity(self, entity_id: str) -> bool:
        reg = er.async_get(self.hass)
        entry = reg.async_get(entity_id)
        if entry is None:
            return False
        m = self.maps.get(entry.platform)
        key = _ekey_lookup(m["entities"], entry.domain, entry.unique_id) if m else None
        want = m["entities"][key] if key else None
        if want is None:
            return False
        kwargs: dict[str, Any] = {}
        target = want["entity_id"]
        if target != entity_id and target.split(".", 1)[0] == entry.domain:
            if reg.async_get(target) is None and self.hass.states.get(target) is None:
                kwargs["new_entity_id"] = target
            else:
                _LOGGER.warning("import alignment: %s keeps its id, %s is already taken", entity_id, target)
        if want.get("name"):
            kwargs["name"] = want["name"]
        if want.get("icon"):
            kwargs["icon"] = want["icon"]
        if want.get("disabled_by") == "user":
            kwargs["disabled_by"] = er.RegistryEntryDisabler.USER
        if want.get("hidden_by") == "user":
            kwargs["hidden_by"] = er.RegistryEntryHider.USER
        if kwargs:
            try:
                reg.async_update_entity(entity_id, **kwargs)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("import alignment of %s failed: %s", entity_id, err)
                return False
        m["entities"].pop(key, None)  # only once applied
        return True

    def align_device(self, device_id: str) -> bool:
        reg = dr.async_get(self.hass)
        dev = reg.async_get(device_id)
        if dev is None:
            return False
        idents = [json.dumps(list(ident)) for ident in dev.identifiers]
        hit = [(m, k) for m in self.maps.values() for k in idents if k in m["devices"]]
        if not hit:
            return False
        want = next((m["devices"][k] for m, k in hit if m["devices"][k].get("name_by_user") or m["devices"][k].get("disabled_by") == "user"), None)
        if want:
            try:
                kwargs: dict[str, Any] = {}
                if want.get("name_by_user"):
                    kwargs["name_by_user"] = want["name_by_user"]
                if want.get("disabled_by") == "user":
                    kwargs["disabled_by"] = dr.DeviceEntryDisabler.USER
                child_cls = getattr(dr, "ChildDeviceEntry", None)
                if child_cls is not None and isinstance(dev, child_cls):
                    reg.async_update_child_device(device_id, **kwargs)  # HA 2026.9: zones etc.
                else:
                    reg.async_update_device(device_id, **kwargs)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("import alignment of device %s failed: %s", device_id, err)
                return False
        for m, k in hit:
            m["devices"].pop(k, None)
        return bool(want)

    def align_existing(self) -> dict[str, int]:
        self.prune_satisfied()
        # only entities that are live (have a state): see _on_entity
        n_e = sum(1 for e in list(er.async_get(self.hass).entities.values())
                  if self.hass.states.get(e.entity_id) is not None and self.align_entity(e.entity_id))
        dreg = dr.async_get(self.hass)
        n_d = sum(1 for d in list(dreg.devices) + list(getattr(dreg, "child_devices", []) or []) if self.align_device(d.id))
        self._save()
        pe, pd = self.pending_counts
        return {"entities": n_e, "devices": n_d, "pending_entities": pe, "pending_devices": pd}


# ----- apply ------------------------------------------------------------------


def _forget_cached_stores(hass: HomeAssistant, names: list[str]) -> None:
    """HA's store manager lists .storage once at startup and answers "no such
    file" for anything missing then: a store copied in afterwards (an import,
    the rebuild after a clean start) would load as empty.  Invalidate those
    keys so the integration reads the copied files."""
    try:
        from homeassistant.helpers.storage import get_internal_store_manager

        manager = get_internal_store_manager(hass)
    except Exception as err:  # noqa: BLE001 - internal HA API: without it the old behaviour stays
        _LOGGER.warning("store cache not invalidated after copying %s: %s", names, err)
        return
    for name in names:
        manager.async_invalidate(name)


def _unmask(given: Any, stored: Any) -> Any:
    """The import form may come from the masked GET summary (diagnostics.scrub):
    whatever still equals the masked form of the backup's value at the same
    place (a "***" under a secret key, a secret inside a text, the same in
    list items) gets the stored value back; edited values stay."""
    from .diagnostics import scrub

    if isinstance(given, dict) and isinstance(stored, dict):
        return {k: (stored[k] if k in stored and given[k] != stored[k] and given[k] == scrub({k: stored[k]})[k] else _unmask(v, stored.get(k)))
                for k, v in given.items()}
    if isinstance(given, list) and isinstance(stored, list) and len(given) == len(stored):
        return [_unmask(g, s) for g, s in zip(given, stored)]
    if isinstance(given, str) and isinstance(stored, str) and given != stored and given == scrub(stored):
        return stored
    return given


async def apply(hass: HomeAssistant, aligner: RegistryAligner, domain: str, entry_id: str, data: dict[str, Any] | None,
                options: dict[str, Any] | None, align: bool, copy_storage: bool, running: bool = True,
                installed: bool = True, cleanup: bool = True, allow_existing: bool = False) -> dict[str, Any]:
    cfg = hass.config.config_dir
    out_dir = os.path.join(cfg, EXTRACT_DIR)
    summary = load_summary(cfg)
    if not summary:
        raise ValueError("no inspected backup: upload and inspect one first")
    dom = summary["domains"].get(domain)
    src = next((e for e in (dom or {}).get("entries", []) if e["entry_id"] == entry_id), None)
    if src is None:
        raise ValueError(f"config entry {entry_id} of {domain} not in the backup")
    if hass.config_entries.async_entries(domain) and not allow_existing:
        raise ValueError(f"{domain} already has a config entry here: delete it first (config flow page)")
    if any(e.unique_id and e.unique_id == src.get("unique_id") for e in hass.config_entries.async_entries(domain)):
        raise ValueError(f"an entry with unique_id {src.get('unique_id')} already exists here")
    if "data" not in src:
        raise ValueError(f"{domain} is not installed here, its configuration was not extracted")
    if not installed:
        raise ValueError(f"{domain} is not installed here: install it first")
    if running:
        try:
            integration = await loader.async_get_integration(hass, domain)
        except loader.IntegrationNotFound:
            raise ValueError(f"{domain} is marked running but its code is not loadable") from None
        if integration.is_built_in:
            raise ValueError(f"{domain} is a built-in integration, not something this container runs")

    if data is not None:
        data = _unmask(data, src.get("data") or {})
    if options is not None:
        options = _unmask(options, src.get("options") or {})
    # the original id: integrations name store files and other state after it
    # (<domain>.<entry_id>); only an id already taken here gets a new one
    original_id = src.get("entry_id")
    keep_id = bool(original_id) and hass.config_entries.async_get_entry(original_id) is None
    entry = ConfigEntry(
        entry_id=original_id if keep_id else None,
        domain=domain,
        title=src.get("title") or domain,
        data=data if data is not None else (src.get("data") or {}),
        options=options if options is not None else (src.get("options") or {}),
        version=int(src.get("version") or 1),
        minor_version=int(src.get("minor_version") or 1),
        unique_id=src.get("unique_id"),
        source="import",
        discovery_keys=MappingProxyType({}),
        subentries_data=src.get("subentries") or [],
        pref_disable_new_entities=src.get("pref_disable_new_entities"),
        pref_disable_polling=src.get("pref_disable_polling"),
        # not running: the entry is stored disabled and enabled when the
        # integration is started (HA skips setup of disabled entries)
        disabled_by=None if running else ConfigEntryDisabler.USER,
    )
    copied: list[str] = []   # new files in place (copy succeeded)
    moved: list[str] = []    # originals set aside as .pre-import (recorded BEFORE the move: a failed copy must still restore them)
    merged: dict[str, Any] = {"entities": {}, "devices": {}}

    def _copy() -> None:
        for f in dom.get("storage_files", []):
            s = os.path.join(out_dir, ".storage", f)
            # a store named after the old id follows the entry to its new one
            name = f.replace(original_id, entry.entry_id) if original_id and not keep_id else f
            d = os.path.join(cfg, ".storage", name)
            if os.path.isfile(s):
                if os.path.isfile(d):  # keep what was here: a failed import must put it back
                    moved.append(name)
                    os.replace(d, d + ".pre-import")
                shutil.copyfile(s, d)
                copied.append(name)

    def _undo() -> None:
        aligner.drop_keys(domain, list(merged["entities"]), list(merged["devices"]))
        for f in dict.fromkeys(moved + copied):
            d = os.path.join(cfg, ".storage", f)
            try:
                if f in moved:
                    os.replace(d + ".pre-import", d)  # the original, whatever state the copy left d in
                else:
                    os.remove(d)
            except OSError:
                pass

    def _commit() -> None:
        for f in moved:
            try:
                os.remove(os.path.join(cfg, ".storage", f) + ".pre-import")
            except OSError:
                pass

    try:
        if copy_storage:
            await hass.async_add_executor_job(_copy)
            _forget_cached_stores(hass, copied)
        if align:
            merged = await hass.async_add_executor_job(_build_map, out_dir, domain, entry_id)
            aligner.merge_map(merged)
        await hass.config_entries.async_add(entry)
        if running and entry.state is not ConfigEntryState.LOADED:
            reason = entry.reason or entry.state.value
            await hass.config_entries.async_remove(entry.entry_id)
            _undo()
            raise ValueError(f"the entry did not load ({reason}); it was removed again, fix the options and retry")
    except ValueError:
        raise
    except Exception as err:  # noqa: BLE001
        _undo()
        raise ValueError(f"{type(err).__name__}: {err}") from None
    finally:
        # Done with the other instance's .storage either way (it holds every
        # integration's credentials): remove the extraction right away
        # (apply_all keeps it until its last entry).
        if cleanup:
            await hass.async_add_executor_job(clear, cfg)
    await hass.async_add_executor_job(_commit)
    result: dict[str, Any] = {"entry_id": entry.entry_id, "state": entry.state.value, "copied_storage": copied, "cleaned_up": True}
    if align:
        try:
            result["alignment"] = aligner.align_existing()
        except Exception as err:  # noqa: BLE001 - the entry is in; alignment continues live
            result["alignment_error"] = f"{type(err).__name__}: {err}"
    return result


async def apply_all(hass: HomeAssistant, aligner: RegistryAligner, domains: list[str] | None, align: bool, copy_storage: bool,
                    running: str | None, installed: set[str]) -> dict[str, Any]:
    """Import every config entry of every installed integration found in the
    inspected backup (or of ``domains`` only), data/options as they are.
    Domains that already have a config entry here are skipped."""
    cfg = hass.config.config_dir
    summary = load_summary(cfg)
    if not summary:
        raise ValueError("no inspected backup: upload and inspect one first")
    todo = [(d, e) for d, info in sorted(summary["domains"].items()) if d in installed and (not domains or d in domains)
            for e in info.get("entries", [])]
    results: list[dict[str, Any]] = []
    preexisting = {d for d, _ in todo if hass.config_entries.async_entries(d)}
    try:
        for domain, entry in todo:
            if domain in preexisting:
                results.append({"domain": domain, "entry_id": entry["entry_id"], "skipped": "already has a config entry here"})
                continue
            try:
                # several entries of one domain (e.g. two hubs) are all imported
                r = await apply(hass, aligner, domain, entry["entry_id"], None, None, align, copy_storage,
                                running=(domain == running), installed=True, cleanup=False, allow_existing=True)
                results.append({"domain": domain, "entry_id": entry["entry_id"], **r})
            except ValueError as err:
                results.append({"domain": domain, "entry_id": entry["entry_id"], "error": str(err)})
    finally:
        await hass.async_add_executor_job(clear, cfg)
    return {"imported": [r for r in results if "state" in r], "skipped": [r for r in results if "skipped" in r],
            "failed": [r for r in results if "error" in r], "cleaned_up": True}


# ----- clean start on a Home Assistant downgrade -----------------------------


def stage_rebuild(config_dir: str, backup_name: str, domain: str | None, ha_version: str, target: str) -> dict[str, Any]:
    """Prepare a clean start on an older Home Assistant.  The import source
    is the pre-change backup: the running integration's config entries,
    registries and store files, extracted the way an uploaded HA backup is.
    The plan file tells the entrypoint to empty ``.storage`` at the next
    boot; once HA has started, async_finish_rebuild imports the integration
    again.  Blocking."""
    out_dir = os.path.join(config_dir, EXTRACT_DIR)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    domains = {domain} if domain else set()
    try:
        with zipfile.ZipFile(os.path.join(config_dir, backupkit.BACKUP_DIR, backup_name)) as zf:
            for name in zf.namelist():
                if name.endswith("/") or ".." in name.split("/") or not _wanted(name, domains):
                    continue
                dest = os.path.join(out_dir, name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as fh:
                    shutil.copyfileobj(src, fh)
        summary = _summarize(out_dir, {"name": backup_name, "date": time.strftime("%Y-%m-%dT%H:%M:%S"), "type": REBUILD_TYPE,
                                       "homeassistant": {"version": ha_version}}, domains)
        try:
            os.remove(os.path.join(out_dir, ".storage", "core.config_entries"))  # the summary keeps what the rebuild needs
        except OSError:
            pass
        with open(os.path.join(config_dir, SUMMARY_FILE), "w", encoding="utf-8") as fh:
            json.dump(summary, fh)
        write_json(os.path.join(config_dir, REBUILD_FILE), {
            "stage": "reset", "from": ha_version, "to": target, "backup": backup_name, "domain": domain,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    info = summary["domains"].get(domain or "", {})
    return {"domain": domain, "entries": len(info.get("entries", [])), "entities": info.get("entities", 0),
            "storage_files": info.get("storage_files", [])}


def drop_rebuild(config_dir: str) -> bool:
    """Forget a scheduled clean start (a newer choice replaces it)."""
    path = os.path.join(config_dir, REBUILD_FILE)
    had = os.path.isfile(path)
    try:
        os.remove(path)
    except OSError:
        pass
    summary = load_summary(config_dir)
    if summary and summary.get("type") == REBUILD_TYPE:
        clear_extracted(config_dir)
    return had


def _read_plan(config_dir: str) -> dict[str, Any] | None:
    try:
        with open(os.path.join(config_dir, REBUILD_FILE), encoding="utf-8") as fh:
            plan = json.load(fh)
        return plan if isinstance(plan, dict) else None
    except (OSError, ValueError):
        return None


async def async_finish_rebuild(hass: HomeAssistant, aligner: RegistryAligner, installer: Any) -> None:
    """After the entrypoint emptied .storage for a downgrade: import the
    integration's config entries, store files and registry customisations
    from the pre-change backup, once Home Assistant has started."""
    cfg = hass.config.config_dir
    plan = await hass.async_add_executor_job(_read_plan, cfg)
    if not plan:
        summary = await hass.async_add_executor_job(load_summary, cfg)
        if summary and summary.get("type") == REBUILD_TYPE:  # a plan the entrypoint dropped
            await hass.async_add_executor_job(clear_extracted, cfg)
        return
    if plan.get("stage") != "import":
        return  # still waiting for the restart that switches the version
    if hass.state is not CoreState.running:
        started = hass.loop.create_future()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, lambda _e: started.done() or started.set_result(None))
        await started
    from .import_views import _locked

    domain, backup, to = plan.get("domain"), plan.get("backup"), plan.get("to")
    head = f"Home Assistant {to} started with a clean configuration"
    tail = f" The previous configuration is in backup {backup}."
    try:
        if not domain:
            msg = f"{head}; no integration was running, so there was nothing to rebuild.{tail}"
        elif domain != installer.running:
            msg = f"{head}; {domain} is not running now, so it was not rebuilt.{tail}"
        else:
            res = await _locked(apply_all(hass, aligner, [domain], True, True, domain, set(installer.state.installed)))
            ok, failed = res["imported"], res["failed"]
            if not ok and not failed:
                msg = f"{head}; {domain} had no config entry to rebuild.{tail}"
            else:
                msg = (f"{head}; {domain}: {len(ok)} config entr{'y' if len(ok) == 1 else 'ies'} rebuilt"
                       + (f", {len(failed)} failed ({'; '.join(f['error'] for f in failed)})" if failed else "") + f".{tail}")
    except Exception as err:  # noqa: BLE001 - reported, the plan must not run again
        msg = f"{head}; rebuilding {domain} failed: {type(err).__name__}: {err}.{tail}"
    finally:
        await hass.async_add_executor_job(drop_rebuild, cfg)
    _LOGGER.info(msg)
    events.emit("rebuild", msg, backup=backup, version=to)
    pn.async_create(hass, msg, title="Home Assistant version change", notification_id="hri_ha_rebuild")
