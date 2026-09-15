"""Backup and restore of the /config volume, shared by the manager component
(create / list / download / upload / schedule restore) and entrypoint.py
(apply a scheduled restore before Home Assistant starts, so no registry
file is open while it is overwritten).

What a backup holds: everything that is configuration or state -
``.storage/`` (registries, config entries, the integration's caches),
``custom_components/`` (the installed integration and the manager),
``integration_manager/`` (state, MQTT config, registry, ha.json),
``configuration.yaml`` and any other top-level yaml.
What it never holds: HA venvs (reinstalled by the entrypoint), log files,
caches, local backups themselves.
"""

from __future__ import annotations

import fnmatch
import json
import re
import threading
import os

from jsonio import ha_vkey, write_json
import shutil
import time
import zipfile

BACKUP_DIR = "backups"
STATE_DIR = "integration_manager"
# A scheduled restore is ONE atomic fact: the meta file (written last, with
# os.replace) names the archive copy and the parts.  The archive is copied
# under a unique name first, so a failure anywhere leaves the previous
# schedule (or none) intact and never an archive paired with foreign parts.
PENDING = os.path.join(STATE_DIR, "restore-pending.zip")  # legacy archive name, still honoured
PENDING_META = os.path.join(STATE_DIR, "restore-pending.json")
PENDING_GLOB = "restore-pending*.zip"
# a restore that was applied but whose outcome could not be recorded (a full disk): the meta is renamed
# to this (a rename needs no free space), so the next boot does not apply the same restore again
APPLIED_META = os.path.join(STATE_DIR, "restore-applied.json")
_PENDING_LOCK = threading.Lock()  # schedule, cancel and apply never interleave (two schedules would drop each other's archive)
PARTS = ("storage", "custom_components", "manager", "yaml")  # selectable restore parts
MARKER = "integration_manager/state.json"  # every backup must carry it
MAX_UNCOMPRESSED = 4 * 1024**3  # an archive that unpacks to more would fill the volume during a restore
SECRET_FILES = (f"{STATE_DIR}/settings.json", f"{STATE_DIR}/mqtt.json")  # mode 600 again after a restore

# relative to the config dir; directories are recursed
INCLUDE_DIRS = (".storage", "custom_components", STATE_DIR)
INCLUDE_ROOT_GLOBS = ("*.yaml", "*.yml")
EXCLUDE_GLOBS = (
    f"{STATE_DIR}/auth_key", f"{STATE_DIR}/auth_key.tmp", f"{STATE_DIR}/auth_revoked", f"{STATE_DIR}/auth_revoked.tmp",  # a restore must not revive logged-out sessions
    "venv-*", "venv-current", "backups", "backups/*", "*.log",
    "*.log.*", "__pycache__", "*/__pycache__", "*/__pycache__/*", "*.pyc", "deps", "deps/*", "tts", "tts/*",
    f"{STATE_DIR}/restore-pending*.zip", f"{STATE_DIR}/restore-pending.json", f"{STATE_DIR}/restore-applied.json", f"{STATE_DIR}/*.tmp", f"{STATE_DIR}/pre-restore-*", f"{STATE_DIR}/ha-install.log",
    f"{STATE_DIR}/staging-*", f"{STATE_DIR}/staging-*/*", f"{STATE_DIR}/backups", f"{STATE_DIR}/backups/*",
    f"{STATE_DIR}/import.tar", f"{STATE_DIR}/import.tar.tmp", f"{STATE_DIR}/import-extracted", f"{STATE_DIR}/import-extracted/*",
    ".storage/*.log", ".storage/core.uuid",
    # the record of what happened (timeline, resource history, change reports) must survive a restore
    f"{STATE_DIR}/events.jsonl*", f"{STATE_DIR}/resource_history.json*", f"{STATE_DIR}/change_reports.json*",
    f"{STATE_DIR}/latest_versions.json*", f"{STATE_DIR}/mqtt_undiscover.json",  # a restore must not bring back older "latest" versions
)
KEEP_DEFAULT = 5
INFO_MAX = 64 * 1024  # backup-info.json is a few hundred bytes; a huge one is a zip bomb


def _excluded(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, g) for g in EXCLUDE_GLOBS)


def iter_files(config_dir: str):
    """Yield (abs_path, rel_path) of everything a backup should contain."""
    for name in sorted(os.listdir(config_dir)):
        path = os.path.join(config_dir, name)
        if os.path.isfile(path) and any(fnmatch.fnmatch(name, g) for g in INCLUDE_ROOT_GLOBS) and not _excluded(name):
            yield path, name
    for top in INCLUDE_DIRS:
        base = os.path.join(config_dir, top)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            rel_root = os.path.relpath(root, config_dir)
            dirs[:] = sorted(d for d in dirs if not _excluded(os.path.join(rel_root, d)))
            for f in sorted(files):
                rel = os.path.join(rel_root, f)
                if not _excluded(rel):
                    yield os.path.join(root, f), rel


def create(config_dir: str, label: str = "", storage_version: str | None = None) -> dict:
    """Write <config>/backups/<timestamp>[-label].zip and return its record.
    ``storage_version``: the Home Assistant version that wrote .storage, when
    it is not the current one (a crash fallback: the crashed version is still
    recorded as current)."""
    import tempfile

    bdir = os.path.join(config_dir, BACKUP_DIR)
    os.makedirs(bdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"\.{2,}", ".", "".join(ch for ch in label if ch.isalnum() or ch in "-_."))[:48]
    name = f"{stamp}{'-' + safe if safe else ''}.zip"
    n = 2
    while True:  # the name is reserved atomically: two backups with one label in the same second never share it
        try:
            os.close(os.open(os.path.join(bdir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            break
        except FileExistsError:
            name = f"{stamp}{'-' + safe if safe else ''}-{n}.zip"
            n += 1
    final = os.path.join(bdir, name)
    _drop_dead_partials(bdir)
    fd, tmp = tempfile.mkstemp(dir=bdir, prefix=f".{name}.", suffix=".tmp")
    os.close(fd)
    count = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, rel in iter_files(config_dir):
                try:
                    zf.write(path, rel)
                except FileNotFoundError:
                    continue  # vanished while zipping (a deploy in progress)
                count += 1
                info = {"created": stamp, "label": label, "files": count, "tool": "hass-remote-integration", "ha_version": storage_version or ha_version(config_dir)}
            zf.writestr("backup-info.json", json.dumps(info))
        os.replace(tmp, final)
    except BaseException:
        for leftover in (tmp, final):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise
    return {"name": name, "bytes": os.path.getsize(final), "mtime": os.path.getmtime(final), "created": stamp, "label": label,
            "files": count, "ha_version": info["ha_version"]}


def ha_version(config_dir: str) -> str | None:
    """Version of the venv currently selected on this volume (ha.json)."""
    try:
        with open(os.path.join(config_dir, STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            return json.load(fh).get("current")
    except (OSError, ValueError):
        return None


def boot_version(config_dir: str) -> str | None:
    """The Home Assistant version the next boot runs: the wanted one, else the current."""
    try:
        with open(os.path.join(config_dir, STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("desired") or data.get("current")
    except (OSError, ValueError, AttributeError):
        return None


def _allowed(rel: str) -> bool:
    """Only paths a backup is allowed to write: the included trees and the
    root files (never e.g. venv-current/... or arbitrary new dirs)."""
    top = rel.split("/", 1)[0]
    if top in INCLUDE_DIRS and "/" in rel:
        return True
    return "/" not in rel and any(fnmatch.fnmatch(rel, g) for g in INCLUDE_ROOT_GLOBS)


def describe(config_dir: str, name: str) -> dict:
    path = os.path.join(config_dir, BACKUP_DIR, name)
    info = {}
    try:
        with zipfile.ZipFile(path) as zf:
            if "backup-info.json" in zf.namelist() and zf.getinfo("backup-info.json").file_size <= INFO_MAX:
                info = json.loads(zf.read("backup-info.json"))
    except (OSError, zipfile.BadZipFile, ValueError):
        pass
    return {"name": name, "bytes": os.path.getsize(path), "mtime": os.path.getmtime(path),
            "created": info.get("created"), "label": info.get("label", ""), "files": info.get("files"),
            "ha_version": info.get("ha_version") if isinstance(info.get("ha_version"), str) else None}


PARTIAL_STALE_S = 6 * 3600  # older than any backup takes to write: left by a process killed mid-backup


def _drop_dead_partials(bdir: str) -> None:
    """The hidden temp file and the empty name reservation of a backup whose
    process was killed while writing it."""
    now = time.time()
    for n in os.listdir(bdir):
        path = os.path.join(bdir, n)
        try:
            st = os.stat(path)
            if now - st.st_mtime > PARTIAL_STALE_S and (n.endswith(".tmp") or (n.endswith(".zip") and st.st_size == 0)):  # also an upload a kill cut off
                os.remove(path)
        except OSError:
            continue


def list_backups(config_dir: str) -> list[dict]:
    bdir = os.path.join(config_dir, BACKUP_DIR)
    if not os.path.isdir(bdir):
        return []
    out = []
    for n in os.listdir(bdir):
        if not n.endswith(".zip") or n.startswith("."):
            continue
        try:
            if os.path.getsize(os.path.join(bdir, n)) == 0:
                continue  # a name reserved by a backup still being written (or one killed while writing)
            out.append(describe(config_dir, n))
        except FileNotFoundError:
            continue  # pruned by a concurrent request between listdir and stat
    return sorted(out, key=_made_at, reverse=True)


def _made_at(b: dict) -> float:
    """When the backup was made (backup-info.json), not when its file last changed: an older backup
    uploaded today is not the newest one.  The file time for backups without that record."""
    try:
        return time.mktime(time.strptime(str(b.get("created") or ""), "%Y%m%d-%H%M%S"))
    except (TypeError, ValueError, OverflowError):
        return float(b.get("mtime") or 0)


def prune(config_dir: str, keep: int = KEEP_DEFAULT, protect: set[str] | None = None) -> list[str]:
    """Delete the oldest backups beyond `keep` (0 or less = keep all);
    names in `protect` (e.g. the recorded pre-update backup) are never
    removed."""
    if keep <= 0:
        return []
    removed = []
    protect = protect or set()
    kept = 0
    for b in list_backups(config_dir):
        if b["name"] in protect:
            continue
        kept += 1
        if kept <= keep:
            continue
        try:
            os.remove(os.path.join(config_dir, BACKUP_DIR, b["name"]))
        except FileNotFoundError:
            continue  # pruned by a concurrent backup already
        removed.append(b["name"])
    return removed


def validate(path: str) -> dict:
    """Raise ValueError unless `path` is a backup made by this tool (or at
    least carries the state marker) with safe member paths."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            for n in names:
                if n.startswith("/") or ".." in n.split("/") or "\\" in n:
                    raise ValueError(f"unsafe path in archive: {n}")
            if MARKER not in names or not any(n.startswith(".storage/") and not n.endswith("/") for n in names):
                # a restore wipes whole trees before extracting: a zip that
                # covers only a marker would empty .storage / integration_manager
                # (core.config_entries itself only exists after a first config entry)
                raise ValueError("not a full backup of this tool (needs integration_manager/state.json and files under .storage/)")
            if sum(i.file_size for i in zf.infolist()) > MAX_UNCOMPRESSED:  # before testzip decompresses everything
                raise ValueError(f"the archive unpacks to more than {MAX_UNCOMPRESSED // 1024**3} GB: not a backup of this tool")
            try:
                bad = zf.testzip()
            except Exception as err:  # noqa: BLE001 - zlib/CRC errors surface as assorted exceptions
                raise ValueError(f"corrupt archive: {err}") from None
            if bad is not None:
                raise ValueError(f"corrupt member in archive: {bad}")
            if "backup-info.json" in names and zf.getinfo("backup-info.json").file_size > INFO_MAX:
                raise ValueError("backup-info.json is implausibly large")
            info = json.loads(zf.read("backup-info.json")) if "backup-info.json" in names else {}
            if not isinstance(info, dict):
                info = {}
            if not isinstance(info.get("ha_version"), str):
                info.pop("ha_version", None)  # an edited or foreign backup-info.json must not break version checks
            return {"files": len(names), **info}
    except zipfile.BadZipFile as err:
        raise ValueError(f"not a zip file: {err}") from None
    except OSError as err:  # missing or unreadable: callers expect ValueError
        raise ValueError(f"cannot read the backup: {err}") from None


def _part_of(rel: str) -> str:
    top = rel.split("/", 1)[0]
    return {".storage": "storage", "custom_components": "custom_components", STATE_DIR: "manager"}.get(top, "yaml" if "/" not in rel else "")


def _select(names: list[str], parts: list[str] | None) -> list[str]:
    if not parts:
        return names
    return [n for n in names if _part_of(n) in parts]


def schedule_restore(config_dir: str, name: str, parts: list[str] | None = None, for_version: str | None = None) -> str:
    """Copy a listed backup to the pending slot; entrypoint applies it at
    the next process start.  `parts` (subset of PARTS) restores only those
    trees: e.g. ["storage"] = registries + config entries, ["manager"] =
    integration_manager state/settings/versions/patches."""
    src = os.path.join(config_dir, BACKUP_DIR, name)
    info = validate(src)
    if parts is not None:
        bad = [x for x in parts if x not in PARTS]
        if bad or not parts:
            raise ValueError(f"parts must be a non-empty subset of {', '.join(PARTS)}")
    # ``for_version``: a restore that belongs to a Home Assistant version change,
    # applied only when that version boots (entrypoint.apply_config_changes)
    boot = for_version or boot_version(config_dir)
    # only .storage has a version: the other parts restore on any Home Assistant
    if (parts is None or "storage" in parts) and info.get("ha_version") and boot and ha_vkey(info["ha_version"]) > ha_vkey(boot):
        raise ValueError(f"backup was made on Home Assistant {info['ha_version']}, newer than {boot} that boots next: update HA first")
    with _PENDING_LOCK:
        import tempfile

        # a name no earlier schedule can have (two in the same millisecond used to share one): a failure
        # below removes only this operation's files, never the archive a confirmed schedule points at
        fd, dst = tempfile.mkstemp(dir=os.path.join(config_dir, STATE_DIR), prefix="restore-pending-", suffix=".zip")
        os.close(fd)
        zip_name = os.path.basename(dst)
        try:
            tmp = os.path.join(os.path.dirname(dst), f".{zip_name}.tmp")  # hidden: never mistaken for an archive, cleaned below
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            # the meta file is the commit point: archive AND parts change together
            write_json(os.path.join(config_dir, PENDING_META), {"name": name, "parts": parts or list(PARTS), "zip": zip_name, "for_version": for_version,
                                                               "ha_version": info.get("ha_version")})
        except BaseException:
            for leftover in (os.path.join(os.path.dirname(dst), f".{zip_name}.tmp"), dst):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            raise
        _drop_stale_pending(config_dir, keep=zip_name)
        return dst


def _pending_meta(config_dir: str) -> dict | None:
    try:
        with open(os.path.join(config_dir, PENDING_META), encoding="utf-8") as fh:
            meta = json.load(fh)
        return meta if isinstance(meta, dict) else None
    except (OSError, ValueError):
        return None


def pending_archive(config_dir: str) -> str | None:
    """Path of the scheduled archive, if the schedule is complete."""
    meta = _pending_meta(config_dir)
    if meta is None:
        return None
    path = os.path.join(config_dir, STATE_DIR, meta.get("zip") or os.path.basename(PENDING))
    return path if os.path.isfile(path) else None


def _drop_stale_pending(config_dir: str, keep: str | None = None) -> None:
    import glob as _glob

    leftovers = _glob.glob(os.path.join(config_dir, STATE_DIR, ".restore-pending*.zip.tmp")) \
        + _glob.glob(os.path.join(config_dir, STATE_DIR, "restore-pending*.zip.tmp"))  # copies a kill interrupted (also 0.11.0's names)
    for p in leftovers:
        try:
            os.remove(p)
        except OSError:
            pass
    for p in _glob.glob(os.path.join(config_dir, STATE_DIR, PENDING_GLOB)):
        if keep and os.path.basename(p) == keep:
            continue
        try:
            os.remove(p)
        except OSError:
            pass


def cancel_restore(config_dir: str) -> bool:
    with _PENDING_LOCK:
        had = pending(config_dir)
        try:
            os.remove(os.path.join(config_dir, PENDING_META))  # first: from here on nothing is scheduled
        except OSError:
            pass
        _drop_stale_pending(config_dir)
        return had


def pending_for_version(config_dir: str) -> str | None:
    """The Home Assistant version a scheduled restore belongs to (None: scheduled by hand)."""
    return (_pending_meta(config_dir) or {}).get("for_version")


def pending_ha_version(config_dir: str) -> str | None:
    """The Home Assistant version the scheduled backup was made on (a restore
    scheduled by an older manager did not record it: read from the archive)."""
    meta = _pending_meta(config_dir) or {}
    made_on = meta.get("ha_version")
    if "ha_version" not in meta and (path := pending_archive(config_dir)):
        try:
            with zipfile.ZipFile(path) as zf:
                if "backup-info.json" in zf.namelist() and zf.getinfo("backup-info.json").file_size <= INFO_MAX:
                    made_on = json.loads(zf.read("backup-info.json")).get("ha_version")
        except (OSError, zipfile.BadZipFile, ValueError, AttributeError):
            made_on = None
    return made_on if isinstance(made_on, str) else None


def pending_parts(config_dir: str) -> list[str]:
    meta = _pending_meta(config_dir) or {}
    try:
        return [x for x in meta.get("parts") if x in PARTS] or list(PARTS)
    except (AttributeError, TypeError):
        return list(PARTS)


def pending(config_dir: str) -> bool:
    return pending_archive(config_dir) is not None


def _extract_to(zf: zipfile.ZipFile, names: list[str], root: str) -> int:
    n = 0
    for name in names:
        if name.endswith("/") or not _allowed(name):
            continue
        dest = os.path.join(root, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(name) as s, open(dest, "wb") as d:
            shutil.copyfileobj(s, d)
        n += 1
    return n


def _wipe_trees(config_dir: str, names: list[str], parts: list[str] | None = None) -> None:
    """Remove what the backup replaces so files it lacks do not linger;
    ha.json, local backups, logs and restore artefacts are kept.  The root
    YAML files go too when the yaml part is restored (a file created after
    the backup, secrets.yaml included, must not survive a rollback)."""
    if "yaml" in (parts if parts is not None else PARTS):
        for entry in os.listdir(config_dir):
            path = os.path.join(config_dir, entry)
            if os.path.isfile(path) and any(fnmatch.fnmatch(entry, g) for g in INCLUDE_ROOT_GLOBS) and not _excluded(entry):
                os.remove(path)
    for top in INCLUDE_DIRS:
        if not any(n.startswith(top + "/") for n in names):
            continue
        d = os.path.join(config_dir, top)
        for entry in os.listdir(d) if os.path.isdir(d) else []:
            rel = os.path.join(top, entry)
            if top == STATE_DIR and (entry in ("ha.json", "ha-install.log", "backups") or entry.startswith(("pre-restore-", "restore-pending"))):
                continue
            if rel in SECRET_FILES and rel not in names:
                continue  # settings and MQTT credentials the backup does not have stay as they are
            if _excluded(rel):
                continue
            path = os.path.join(d, entry)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)


def _names(zf: zipfile.ZipFile) -> list[str]:
    # a backup made before a file was excluded still carries it: never restored either
    return [n for n in zf.namelist() if n != "backup-info.json" and n != f"{STATE_DIR}/ha.json" and not _excluded(n)]


def apply_pending(config_dir: str, log=print, record=None, storage_version: str | None = None) -> dict | None:
    """Called by entrypoint.py with HA stopped.  Order: validate (CRC) ->
    pre-restore backup -> extract into a staging dir -> wipe + move into
    place.  If anything fails after the wipe, the pre-restore backup is
    put back, so the volume never boots empty."""
    src = pending_archive(config_dir)
    if src is None:
        _drop_stale_pending(config_dir)  # an archive without its meta was never a schedule
        return None
    import glob as _glob

    for old in _glob.glob(os.path.join(config_dir, STATE_DIR, "staging-restore-*")):
        shutil.rmtree(old, ignore_errors=True)  # left behind by a restore a power loss interrupted
    result = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": False, "error": ""}
    staging = os.path.join(config_dir, STATE_DIR, f"staging-restore-{int(time.time())}")
    pre = None
    wiped = False
    names: list[str] = []
    try:
        validate(src)
        meta = _pending_meta(config_dir) or {}
        result["backup"] = meta.get("name")  # the source: protected from pruning afterwards
        pre = None
        if meta.get("pre_restore"):  # a retry after a restore a power loss interrupted: its first copy is the real "before"
            try:
                validate(os.path.join(config_dir, BACKUP_DIR, str(meta["pre_restore"])))
                pre = {"name": str(meta["pre_restore"])}
            except Exception:  # noqa: BLE001 - gone or unreadable: take a new one
                pre = None
        if pre is None:
            pre = create(config_dir, "pre-restore", storage_version)
            try:
                write_json(os.path.join(config_dir, PENDING_META), {**meta, "pre_restore": pre["name"]})
            except OSError:
                pass  # best effort: without it a retry takes another copy
        log(f"restore: pre-restore copy {pre['name']}")
        parts = pending_parts(config_dir)
        result["parts"] = parts
        with zipfile.ZipFile(src) as zf:
            names = _select(_names(zf), parts)
            if not names:
                raise ValueError(f"the backup has nothing for the selected parts {parts}")
            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging)
            count = _extract_to(zf, names, staging)  # fails here -> nothing touched yet
            wiped = True  # before: a failure halfway through the wipe must still roll back
            _wipe_trees(config_dir, names, parts)
            for name in sorted(os.listdir(staging)):
                s_path, d_path = os.path.join(staging, name), os.path.join(config_dir, name)
                if os.path.isdir(s_path):
                    for root, dirs, files in os.walk(s_path):
                        rel_root = os.path.relpath(root, s_path)
                        target_root = os.path.join(d_path, rel_root) if rel_root != "." else d_path
                        os.makedirs(target_root, exist_ok=True)
                        for f in files:
                            os.replace(os.path.join(root, f), os.path.join(target_root, f))
                else:
                    os.replace(s_path, d_path)
        for rel in SECRET_FILES:
            if os.path.isfile(os.path.join(config_dir, rel)):
                os.chmod(os.path.join(config_dir, rel), 0o600)
        result.update(ok=True, files=count, pre_restore=pre["name"])
        log(f"restore: applied {count} files")
    except Exception as err:  # noqa: BLE001
        result["error"] = f"{type(err).__name__}: {err}"
        log(f"restore FAILED: {result['error']}")
        if wiped and pre:
            try:
                with zipfile.ZipFile(os.path.join(config_dir, BACKUP_DIR, pre["name"])) as zf:
                    before = _select(_names(zf), result.get("parts"))
                    # an overlay would keep files the failed restore had already
                    # placed: clear the same trees first, then put back exactly
                    # what was there
                    _wipe_trees(config_dir, names, result.get("parts"))
                    _extract_to(zf, before, config_dir)
                result["rolled_back_to"] = pre["name"]
                log(f"restore: put back {pre['name']}")
            except Exception as err2:  # noqa: BLE001
                result["error"] += f"; rollback to {pre['name']} FAILED: {err2}"
                log(result["error"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # the outcome is recorded (ha.json) BEFORE the schedule goes: a power loss or a full disk in
        # between must not leave a replaced configuration that nothing knows about; not recorded =
        # the restore stays scheduled and is applied again at the next boot
        recorded = True
        if record is not None:
            try:
                recorded = record(result) is not False
            except Exception as err:  # noqa: BLE001
                log(f"restore: outcome not recorded ({err})")
                recorded = False
        applied_unrecorded = False
        if recorded:
            try:
                os.remove(os.path.join(config_dir, PENDING_META))
            except OSError:
                pass
        elif result.get("ok"):
            try:
                os.replace(os.path.join(config_dir, PENDING_META), os.path.join(config_dir, APPLIED_META))
                applied_unrecorded = True
                log("restore: applied, outcome recorded at the next boot (ha.json could not be written)")
            except OSError as err:
                log(f"restore: applied but neither recorded nor marked ({err}): it would be applied again at the next boot")
        keep = None if (recorded or applied_unrecorded) else os.path.basename(src)
        _drop_stale_pending(config_dir, keep=keep)  # not recorded: its archive stays with the schedule
    return result
