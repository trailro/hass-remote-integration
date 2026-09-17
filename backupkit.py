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
import posixpath

from jsonio import fsync_dir, ha_vkey, write_json
import shutil
import struct
import time
import zipfile

BACKUP_DIR = "backups"
STATE_DIR = "integration_manager"
# A scheduled restore is ONE atomic fact: the meta file (written last, with
# os.replace) names the archive copy and the parts.  The archive is copied
# under a unique name first, so a failure anywhere leaves the previous
# schedule (or none) intact and never an archive paired with foreign parts.
# the archive of a schedule whose meta names none: written before 0.7.0 (every schedule since records "zip")
LEGACY_PENDING_ZIP = os.path.join(STATE_DIR, "restore-pending.zip")
PENDING_META = os.path.join(STATE_DIR, "restore-pending.json")
PENDING_GLOB = "restore-pending*.zip"
# a restore that was applied but whose outcome could not be recorded (a full disk): the meta is renamed
# to this (a rename needs no free space), so the next boot does not apply the same restore again
APPLIED_META = os.path.join(STATE_DIR, "restore-applied.json")
# the same for a restore that failed and was put back: without it a full disk re-applies it at every boot
FAILED_META = os.path.join(STATE_DIR, "restore-failed.json")
# a Home Assistant version as jsonio.ha_vkey parses it; anything else ("unknown", "dev") says nothing about the version
_HA_VERSION_RE = re.compile(r"\s*\d+\.\d+(?:\.\d+)?(?:b\d+)?\s*")


def known_ha_version(value) -> str | None:
    return value if isinstance(value, str) and _HA_VERSION_RE.fullmatch(value) else None

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
    f"{STATE_DIR}/restore-pending*.zip", f"{STATE_DIR}/restore-pending.json", f"{STATE_DIR}/restore-applied.json", f"{STATE_DIR}/restore-failed.json", f"{STATE_DIR}/*.tmp", f"{STATE_DIR}/pre-restore-*", f"{STATE_DIR}/ha-install.log",
    f"{STATE_DIR}/staging-*", f"{STATE_DIR}/staging-*/*", f"{STATE_DIR}/backups", f"{STATE_DIR}/backups/*",
    f"{STATE_DIR}/import.tar", f"{STATE_DIR}/import.tar.tmp", f"{STATE_DIR}/import-extracted", f"{STATE_DIR}/import-extracted/*",
    ".storage/*.log", ".storage/core.uuid",
    # the port Home Assistant was set up with: a backup from a container on another HRI_PORT (a second
    # container) would pin a foreign port here and every boot would stop at run.py's port check
    ".storage/http",
    # a store being written (HA's temporary file: tmp + 8 random characters) and an import's set-aside original
    ".storage/tmp" + "[a-z0-9_]" * 8, ".storage/*.pre-import", ".storage/*.pre-import.done",
    # the record of what happened (timeline, resource history, change reports) must survive a restore
    f"{STATE_DIR}/events.jsonl*", f"{STATE_DIR}/resource_history.json*", f"{STATE_DIR}/change_reports.json*",
    f"{STATE_DIR}/latest_versions.json*", f"{STATE_DIR}/mqtt_undiscover.json",  # a restore must not bring back older "latest" versions
    # what the broker holds is outside the volume: an older ledger or cleanup list would forget retained data still
    # on the broker (the main HA keeps those entities) or clear data published since
    f"{STATE_DIR}/mqtt_identity.json*", f"{STATE_DIR}/mqtt_cleanup_pending.json*",
)
KEEP_DEFAULT = 5
INFO_MAX = 64 * 1024  # backup-info.json is a few hundred bytes; a huge one is a zip bomb
# files in a backup, as the Home Assistant backup import (ha_import.MAX_MEMBERS): a volume holds a few thousand, and
# every member costs memory and time to read even when empty (500000 took 17 s and 286 MB to validate)
MAX_MEMBERS = 100_000


def _central_directory(fh) -> tuple[int, int, int] | None:
    """(members the end record declares, start, size of the central directory), located as zipfile's _EndRecData
    and _EndRecData64 locate them (Python 3.14); None where zipfile finds no archive or a corrupt one: it refuses
    that itself."""
    fh.seek(0, 2)
    end = fh.tell()
    if end < 22:
        return None
    fh.seek(end - 22)
    rec, at = fh.read(22), end - 22
    if not (rec[:4] == b"PK\x05\x06" and rec[-2:] == b"\0\0"):  # else a comment follows the record
        first = max(end - 0xFFFF - 22, 0)
        fh.seek(first)
        data = fh.read(0xFFFF + 22)
        i = data.rfind(b"PK\x05\x06")
        if i < 0 or len(data) - i < 22:
            return None
        rec, at = data[i:i + 22], first + i
    _sig, _disk, _cd_disk, _here, total, cd_size, _cd_offset, _comment = struct.unpack("<4s4H2LH", rec)
    location = at
    if at >= 20:
        fh.seek(at - 20)
        sig, disk, reloff, disks = struct.unpack("<4sLQL", fh.read(20))
        if sig == b"PK\x06\x07":  # zip64: zipfile takes the count and the directory from the zip64 record
            rec_at = at - 20 - 56
            if disk != 0 or disks > 1 or reloff > rec_at:
                return None
            fh.seek(reloff)
            data, extra = fh.read(56), rec_at - reloff
            if not data.startswith(b"PK\x06\x06") and reloff != rec_at:
                fh.seek(rec_at)
                data, extra = fh.read(56), 0
            if len(data) != 56 or not data.startswith(b"PK\x06\x06"):
                return None
            _sig, size, _made, _needs, _disk, _cd_disk, _here, total, cd_size, cd_offset = struct.unpack("<4sQ2H2L4Q", data)
            if cd_offset + cd_size != reloff or size + 12 != 56 + extra:
                return None
            location = rec_at - extra
    return (total, location - cd_size, cd_size) if location >= cd_size else None


def zip_has_more_members(fh, limit: int) -> bool:
    """Whether the zip archive in the seekable binary ``fh`` holds more than ``limit`` members, told from its end
    records and central directory headers alone: zipfile.ZipFile builds an object for every member while it opens
    the archive, before any count can be checked (300000 empty members took 4.5 s and 178 MB).  The headers are
    walked (up to ``limit`` + 1 of them) because zipfile reads all of them whatever count the end record declares.
    An archive zipfile cannot open is left to zipfile to refuse."""
    found = _central_directory(fh)
    if found is None:
        return False
    total, start, cd_size = found
    if total > limit:
        return True
    fh.seek(start)
    count = done = 0
    while done < cd_size:
        head = fh.read(46)
        if len(head) != 46 or head[:4] != b"PK\x01\x02":
            return False
        count += 1
        if count > limit:
            return True
        name, extra, comment = struct.unpack_from("<3H", head, 28)
        fh.seek(name + extra + comment, 1)
        done += 46 + name + extra + comment
    return False


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
    name = reserve_name(bdir, f"{stamp}{'-' + safe if safe else ''}")
    final = os.path.join(bdir, name)
    _drop_dead_partials(bdir)
    fd, tmp = tempfile.mkstemp(dir=bdir, prefix=f".{name}.", suffix=".tmp")
    count = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
                for path, rel in iter_files(config_dir):
                    if count >= MAX_MEMBERS:
                        # not a backup that could be restored (validate refuses it): said now, not at the restore
                        raise ValueError(f"the configuration holds more than {MAX_MEMBERS} files: no backup was made")
                    try:
                        zf.write(path, rel)
                    except FileNotFoundError:
                        continue  # vanished while zipping (a deploy in progress)
                    count += 1
                info = {"created": stamp, "label": label, "files": count, "tool": "hass-remote-integration", "ha_version": storage_version or ha_version(config_dir)}
                zf.writestr("backup-info.json", json.dumps(info))
            fh.flush()
            os.fsync(fh.fileno())  # a power loss right after the rename must not leave a named but empty or torn backup
        os.replace(tmp, final)
        fsync_dir(bdir)
    except BaseException:
        for leftover in (tmp, final):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise
    return {"name": name, "bytes": os.path.getsize(final), "mtime": os.path.getmtime(final), "created": stamp, "label": label,
            "files": count, "ha_version": info["ha_version"]}


def reserve_name(bdir: str, stem: str) -> str:
    """<stem>.zip, else <stem>-2.zip, -3 ...: the name is reserved atomically (an empty file), so two backups
    or uploads with one name never share it and an existing backup is never overwritten."""
    name, n = f"{stem}.zip", 2
    while True:
        try:
            os.close(os.open(os.path.join(bdir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            return name
        except FileExistsError:
            name = f"{stem}-{n}.zip"
            n += 1


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


_DESCRIBED: dict[str, tuple[tuple[int, ...], dict]] = {}  # path -> ((mtime_ns, size, ino, dev), record)
_DESCRIBED_LOCK = threading.Lock()
_DESCRIBED_MAX = 512


def describe(config_dir: str, name: str) -> dict:
    """Read once per file version: every GET /api/backups and every prune lists all backups, and opening a zip
    reads its whole central directory."""
    path = os.path.join(config_dir, BACKUP_DIR, name)
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev)  # a same-size copy moved over it within one mtime tick is another inode
    with _DESCRIBED_LOCK:
        hit = _DESCRIBED.get(path)
    if hit is not None and hit[0] == key:
        return dict(hit[1])
    info = {}
    try:
        with open(path, "rb") as fh:
            if zip_has_more_members(fh, MAX_MEMBERS + 1):
                raise ValueError("too many members")  # listed without its info, as any archive that is no backup
        with zipfile.ZipFile(path) as zf:
            meta = zf.getinfo("backup-info.json")  # not "in namelist()": a list of every member, at every listing
            if meta.file_size <= INFO_MAX:
                info = json.loads(zf.read(meta))
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        pass
    if not isinstance(info, dict):
        info = {}  # an edited or foreign backup-info.json must not break the list
    record = {"name": name, "bytes": st.st_size, "mtime": st.st_mtime,
              "created": info.get("created"), "label": info.get("label", ""), "files": info.get("files"),
              "ha_version": known_ha_version(info.get("ha_version"))}
    with _DESCRIBED_LOCK:
        if len(_DESCRIBED) >= _DESCRIBED_MAX:
            _DESCRIBED.clear()
        _DESCRIBED[path] = (key, record)
    return dict(record)


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
    uploaded today is not the newest one.  Never later than the file time: an upload claiming a future
    date must not rank above, and so push out, the backups really made since."""
    mtime = float(b.get("mtime") or 0)
    try:
        return min(time.mktime(time.strptime(str(b.get("created") or ""), "%Y%m%d-%H%M%S")), mtime)
    except (TypeError, ValueError, OverflowError):
        return mtime


UPLOAD_GRACE_S = 7 * 86400  # an uploaded (usually older) backup is kept at least this long, whatever its date


def restore_needs(config_dir: str) -> set[str]:
    """Backups a restore still needs, whoever calls prune: the source and the pre-restore copy of a
    scheduled (or retried) restore, and the copy a failed rollback left as the way back."""
    out = set()
    meta = _pending_meta(config_dir) or {}
    for key in ("name", "pre_restore"):
        if isinstance(meta.get(key), str):
            out.add(meta[key])
    try:
        with open(os.path.join(config_dir, STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            last = json.load(fh).get("last_restore")
        if isinstance(last, dict) and isinstance(last.get("recovery_source"), str):
            out.add(last["recovery_source"])
    except (OSError, ValueError, AttributeError):
        pass
    return out


def prune(config_dir: str, keep: int = KEEP_DEFAULT, protect: set[str] | None = None) -> list[str]:
    """Delete the oldest backups beyond `keep` (0 or less = keep all);
    names in `protect` (e.g. the recorded pre-update backup, the backup the
    caller just made) are never removed, nor what a restore needs, nor an
    upload younger than UPLOAD_GRACE_S."""
    if keep <= 0:
        return []
    removed = []
    protect = set(protect or ()) | restore_needs(config_dir)
    kept = 0
    now = time.time()
    for b in list_backups(config_dir):
        if b["name"] in protect or (b["name"].startswith("upload-") and now - float(b.get("mtime") or 0) < UPLOAD_GRACE_S):
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
        with open(path, "rb") as fh:
            if zip_has_more_members(fh, MAX_MEMBERS + 1):  # + backup-info.json; before zipfile reads the members
                raise ValueError(f"the archive holds more than {MAX_MEMBERS} files: not a backup of this tool")
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            for n in names:
                member = n[:-1] if n.endswith("/") else n
                # only the normal spelling: "a/./auth_key" or "a//auth_key" would pass the exclusions and still land on a/auth_key
                if n.startswith("/") or "\\" in n or posixpath.normpath(member) != member or any(p in ("", ".", "..") for p in member.split("/")):
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
            if not known_ha_version(info.get("ha_version")):
                # an edited or foreign backup-info.json must not break version checks, and "unknown" is no
                # version: it would sort older than any and skip the confirmation a backup without one needs
                info.pop("ha_version", None)
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


def schedule_restore(config_dir: str, name: str, parts: list[str] | None = None, for_version: str | None = None, force: bool = False) -> str:
    """Copy a listed backup to the pending slot; entrypoint applies it at
    the next process start.  `parts` (subset of PARTS) restores only those
    trees: e.g. ["storage"] = registries + config entries, ["manager"] =
    integration_manager state/settings/versions/patches.  ``force``: restore
    .storage from a backup that does not record its Home Assistant version."""
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
    if (parts is None or "storage" in parts) and not info.get("ha_version") and not force:
        # it may come from a newer Home Assistant, which the one booting cannot read
        raise UnknownVersion("the backup does not record the Home Assistant version it was made on: restoring its .storage "
                             "on a version older than that one breaks the configuration (restore anyway with force)")
    with _PENDING_LOCK:
        import tempfile

        # a name no earlier schedule can have (two in the same millisecond used to share one): a failure
        # below removes only this operation's files, never the archive a confirmed schedule points at
        fd, dst = tempfile.mkstemp(dir=os.path.join(config_dir, STATE_DIR), prefix="restore-pending-", suffix=".zip")
        os.close(fd)
        zip_name = os.path.basename(dst)
        try:
            tmp = os.path.join(os.path.dirname(dst), f".{zip_name}.tmp")  # hidden: never mistaken for an archive, cleaned below
            with open(src, "rb") as s, open(tmp, "wb") as d:
                shutil.copyfileobj(s, d, 1 << 20)
                d.flush()
                os.fsync(d.fileno())  # a torn copy after a power loss fails validation and the restore is dropped
            os.replace(tmp, dst)
            fsync_dir(os.path.dirname(dst))
            # the meta file is the commit point: archive AND parts change together
            write_json(os.path.join(config_dir, PENDING_META), {"name": name, "parts": parts or list(PARTS), "zip": zip_name, "for_version": for_version,
                                                               "ha_version": info.get("ha_version"), "force": bool(force)})
        except BaseException:
            for leftover in (os.path.join(os.path.dirname(dst), f".{zip_name}.tmp"), dst):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            raise
        _drop_stale_pending(config_dir, keep=zip_name)
        return dst


class UnknownVersion(ValueError):
    """A .storage restore of a backup without a recorded Home Assistant version, not forced."""


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
    path = os.path.join(config_dir, STATE_DIR, meta.get("zip") or os.path.basename(LEGACY_PENDING_ZIP))
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


class BelongsToVersionChange(ValueError):
    """A restore scheduled with the Home Assistant version change ha.json still records: it goes with that change."""

    def __init__(self, for_version: str) -> None:
        super().__init__(f"the scheduled restore belongs to the switch to Home Assistant {for_version}")
        self.for_version = for_version


def _scheduled_change_to(config_dir: str) -> str | None:
    """The version of the change ha.json records (scheduled from System, not yet applied or dropped)."""
    try:
        with open(os.path.join(config_dir, STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            change = json.load(fh).get("change")
    except (OSError, ValueError, AttributeError):
        return None
    return change.get("to") if isinstance(change, dict) else None


def cancel_restore(config_dir: str, only_zip: str | None = None, by_hand: bool = False) -> bool:
    """``only_zip``: cancel only while the schedule is still that archive's (checked under the same lock a
    new schedule takes, so a restore scheduled by someone else in between is never cancelled).

    ``by_hand`` (Cancel restore in the UI): a restore that belongs to the version change ha.json still records
    raises BelongsToVersionChange and stays.  Cancelled alone, the entrypoint cancels that switch one boot later
    (its restore "did not happen") while System still shows it scheduled; it goes with the switch.  A leftover
    for a change ha.json no longer records is cancelled like any other.  Checked under the lock too: the
    schedule judged is the one cancelled."""
    with _PENDING_LOCK:
        meta = _pending_meta(config_dir) or {}
        if only_zip is not None and meta.get("zip") != only_zip:
            return False
        for_version = meta.get("for_version")
        if by_hand and for_version and pending(config_dir) and _scheduled_change_to(config_dir) == for_version:
            raise BelongsToVersionChange(str(for_version))
        had = pending(config_dir)
        try:
            os.remove(os.path.join(config_dir, PENDING_META))  # first: from here on nothing is scheduled
        except OSError:
            pass
        _drop_stale_pending(config_dir)
        return had


def drop_orphan_schedule(config_dir: str, log=print, record=None) -> dict | None:
    """A schedule whose archive copy is gone (removed by hand, lost): nothing can be restored from it, but its meta
    kept the backup it names from being deleted or pruned for good, and a version change waiting for it was
    cancelled while System showed no restore scheduled.  Dropped and recorded as a failed restore (``record``, as
    apply_pending records an outcome); nothing on the volume changed, so a record that cannot be written does not
    keep it either.  Also removes archive copies no meta names.  None when there was no such schedule."""
    with _PENDING_LOCK:
        meta_path = os.path.join(config_dir, PENDING_META)
        if not os.path.lexists(meta_path):
            _drop_stale_pending(config_dir)  # an archive without its meta was never a schedule
            return None
        if pending_archive(config_dir) is not None:
            return None
        meta = _pending_meta(config_dir) or {}
        name = meta.get("name") if isinstance(meta.get("name"), str) else None
        result = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": False, "backup": name, "parts": pending_parts(config_dir),
                  "for_version": meta.get("for_version") if isinstance(meta.get("for_version"), str) else None,
                  "error": f"the scheduled restore of {name or 'a backup'} was dropped: its copy of the archive "
                           f"({meta.get('zip') or os.path.basename(LEGACY_PENDING_ZIP)}) is gone from the volume; nothing was restored"}
        log(f"restore: {result['error']}")
        if record is not None:
            try:
                record(result)
            except Exception as err:  # noqa: BLE001
                log(f"restore: outcome not recorded ({err})")
        try:
            os.remove(meta_path)
        except OSError as err:
            log(f"restore: {PENDING_META} could not be removed ({err})")
        _drop_stale_pending(config_dir)
        return result


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
                    info = json.loads(zf.read("backup-info.json"))
                    made_on = info.get("ha_version") if isinstance(info, dict) else None
        except (OSError, zipfile.BadZipFile, ValueError, AttributeError):
            made_on = None
    return known_ha_version(made_on)


def pending_forced(config_dir: str) -> bool:
    """The scheduled restore may bring back .storage of a backup without a recorded Home Assistant version.
    A schedule written before that was checked (no "force" key) was accepted by the rules of its time."""
    meta = _pending_meta(config_dir) or {}
    return meta.get("force") is True or "force" not in meta


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
        if os.path.islink(dest):
            os.remove(dest)  # a (dangling) link: open() would write its target outside the volume
        with zf.open(name) as s, open(dest, "wb") as d:
            shutil.copyfileobj(s, d)
        n += 1
    return n


def _move_into(staging: str, config_dir: str) -> None:
    """Move what was extracted into ``staging`` to the same places under ``config_dir``.  A symbolic link on the
    way (a directory, or a file) is replaced by a real directory or the file, never written through."""
    for name in sorted(os.listdir(staging)):
        s_path, d_path = os.path.join(staging, name), os.path.join(config_dir, name)
        if os.path.isdir(s_path):
            for root, dirs, files in os.walk(s_path):
                rel_root = os.path.relpath(root, s_path)
                target_root = os.path.join(d_path, rel_root) if rel_root != "." else d_path
                if os.path.islink(target_root):  # top-down: every parent was checked before
                    os.remove(target_root)  # replaced by a real directory, never written through
                os.makedirs(target_root, exist_ok=True)
                for f in files:
                    os.replace(os.path.join(root, f), os.path.join(target_root, f))
        else:
            os.replace(s_path, d_path)


def _wipe_trees(config_dir: str, names: list[str], parts: list[str] | None = None) -> None:
    """Remove what the backup replaces so files it lacks do not linger;
    ha.json, local backups, logs and restore artefacts are kept.  The root
    YAML files go too when the yaml part is restored (a file created after
    the backup, secrets.yaml included, must not survive a rollback).
    Symbolic links are removed as links, never followed: a restore writes
    real files and directories in their place."""
    linked = _linked_tops(config_dir, names)
    if linked:
        raise ValueError(f"{', '.join(linked)} is a symbolic link: a restore never deletes or writes through one")
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
            # isdir() is true for a link to a directory, and rmtree refuses links (ignore_errors hid it)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)


def _linked_tops(config_dir: str, names: list[str]) -> list[str]:
    tops = sorted({n.split("/", 1)[0] for n in names if "/" in n})
    return [t for t in tops if os.path.islink(os.path.join(config_dir, t))]


def _sync() -> None:
    try:
        os.sync()
    except (AttributeError, OSError):
        pass


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
        return drop_orphan_schedule(config_dir, log, record)
    import glob as _glob

    for old in _glob.glob(os.path.join(config_dir, STATE_DIR, "staging-restore-*")):
        shutil.rmtree(old, ignore_errors=True)  # left behind by a restore a power loss interrupted
    result = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": False, "error": ""}
    staging = os.path.join(config_dir, STATE_DIR, f"staging-restore-{int(time.time())}")
    rollback_staging = staging + "-rollback"
    pre = None
    wiped = False
    names: list[str] = []
    # "ok", "failed" (nothing changed, or put back), "rollback_failed"; None: interrupted (KeyboardInterrupt,
    # SystemExit) - the last two keep the schedule, so the next boot retries from the same pre-restore copy
    outcome = None
    try:
        meta = _pending_meta(config_dir) or {}
        result["backup"] = meta.get("name")  # the source: protected from pruning afterwards
        parts = pending_parts(config_dir)
        result["parts"] = parts
        pre = None
        if meta.get("pre_restore"):  # a retry after a restore a power loss interrupted: its first copy is the real "before"
            try:
                validate(os.path.join(config_dir, BACKUP_DIR, str(meta["pre_restore"])))
                pre = {"name": str(meta["pre_restore"])}
                # an earlier attempt got past that copy and may have left the volume half restored (a kill during
                # the moves, a rollback that failed): whatever fails now, before the wipe too, puts the copy back
                wiped = True
            except Exception:  # noqa: BLE001 - gone or unreadable: take a new one
                pre = None
        validate(src)
        if pre is None:
            pre = create(config_dir, "pre-restore", storage_version)
            try:
                write_json(os.path.join(config_dir, PENDING_META), {**meta, "pre_restore": pre["name"]})
            except OSError as err:
                # not recorded, a retry after a kill during the moves would take its "before" copy from the half-wiped volume
                raise OSError(err.errno, f"the pre-restore copy {pre['name']} could not be recorded in the schedule ({err.strerror or err}); nothing was changed") from None
        log(f"restore: pre-restore copy {pre['name']}")
        with zipfile.ZipFile(src) as zf:
            names = _select(_names(zf), parts)
            if not names:
                raise ValueError(f"the backup has nothing for the selected parts {parts}")
            linked = _linked_tops(config_dir, names)
            if linked:
                raise ValueError(f"{', '.join(linked)} is a symbolic link: a restore never deletes or writes through one")
            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging)
            count = _extract_to(zf, names, staging)  # fails here -> nothing touched yet
            wiped = True  # before: a failure halfway through the wipe must still roll back
            _wipe_trees(config_dir, names, parts)
            _move_into(staging, config_dir)
        for rel in SECRET_FILES:
            if os.path.isfile(os.path.join(config_dir, rel)):
                os.chmod(os.path.join(config_dir, rel), 0o600)
        _sync()  # the restored files are on disk before the outcome is recorded and the schedule goes
        result.update(ok=True, files=count, pre_restore=pre["name"])
        outcome = "ok"
        log(f"restore: applied {count} files")
    except BaseException as err:
        interrupted = not isinstance(err, Exception)
        result["error"] = f"{type(err).__name__}: {err}"
        log(f"restore {'INTERRUPTED' if interrupted else 'FAILED'}: {result['error']}")
        if wiped and pre:
            shutil.rmtree(staging, ignore_errors=True)  # before the rollback: on a full disk it needs that space
            try:
                with zipfile.ZipFile(os.path.join(config_dir, BACKUP_DIR, pre["name"])) as zf:
                    before = _select(_names(zf), result.get("parts"))
                    # an overlay would keep files the failed restore had already
                    # placed: clear the same trees first, then put back exactly
                    # what was there
                    _wipe_trees(config_dir, names or before, result.get("parts"))  # names: none when a retry failed before reading the archive
                    # through a staging directory and the same moves as the restore: a tree the wipe leaves alone
                    # (one the failed restore did not have) may hold a symbolic link to a directory
                    shutil.rmtree(rollback_staging, ignore_errors=True)
                    os.makedirs(rollback_staging)
                    _extract_to(zf, before, rollback_staging)
                    _move_into(rollback_staging, config_dir)
                _sync()
                result["rolled_back_to"] = pre["name"]
                log(f"restore: put back {pre['name']}")
                outcome = None if interrupted else "failed"
            except Exception as err2:  # noqa: BLE001
                result["error"] += f"; rollback to {pre['name']} FAILED: {err2}"
                result["recovery_source"] = pre["name"]  # the way back by hand; protected from pruning (restore_needs)
                log(f"{result['error']}; the configuration from before the restore is in backup {pre['name']}, the restore is retried at the next boot")
                outcome = None if interrupted else "rollback_failed"
        elif not interrupted:
            outcome = "failed"
        if interrupted:
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(rollback_staging, ignore_errors=True)
        if outcome in (None, "rollback_failed"):
            # the volume may be half restored: the schedule (with its pre-restore copy) stays for a retry
            if outcome == "rollback_failed" and record is not None:
                try:
                    record(result)
                except Exception as err:  # noqa: BLE001
                    log(f"restore: outcome not recorded ({err})")
            _drop_stale_pending(config_dir, keep=os.path.basename(src))
    if outcome == "rollback_failed":
        return result
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
    marked = False
    if recorded:
        try:
            os.remove(os.path.join(config_dir, PENDING_META))
        except OSError:
            pass
    else:
        # a rename needs no free space: the next boot records the outcome and does not apply the restore again
        # (a failed one would otherwise be validated, extracted and rolled back at every boot of a full disk)
        marker = APPLIED_META if result.get("ok") else FAILED_META
        try:
            os.replace(os.path.join(config_dir, PENDING_META), os.path.join(config_dir, marker))
            marked = True
            log(f"restore: {'applied' if result.get('ok') else 'failed and not retried'}, outcome recorded at the next boot (ha.json could not be written)")
        except OSError as err:
            log(f"restore: {'applied' if result.get('ok') else 'failed'} but neither recorded nor marked ({err}): it would be applied again at the next boot")
    keep = None if (recorded or marked) else os.path.basename(src)
    _drop_stale_pending(config_dir, keep=keep)  # not recorded: its archive stays with the schedule
    return result
