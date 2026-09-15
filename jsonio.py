"""Shared by the entrypoint, run.py and the manager component (all import
it from /app): one atomic JSON writer, one tolerant reader, one version
key.  Six slightly different tmp+replace writers used to live around the
code base, one of them not atomic at all."""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any


def write_json(path: str, data: Any, *, indent: int = 2, fsync: bool = True, mode: int | None = None, sort_keys: bool = False) -> None:
    """tmp file next to the target (unique per write: two threads saving the
    same file at once each finish with a complete document, the later one
    wins), optional fsync, optional chmod, then an atomic replace."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=indent, sort_keys=sort_keys)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        if fsync:
            fsync_dir(d)  # the rename itself: without it a power loss can bring back the old file, or none
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # not every filesystem syncs a directory; the data itself was synced
    finally:
        os.close(fd)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def vkey(v: str | None) -> tuple[int, ...]:
    """Sort key for version strings / tags: the first four integers."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4])


_STABLE_TAG = re.compile(r"[vV]?\d+(?:\.\d+){0,3}")


def is_stable_tag(tag: str | None) -> bool:
    """A plain release number (1.2, v1.2.3); not a beta, an rc, a branch, a commit or "local"."""
    return bool(tag) and bool(_STABLE_TAG.fullmatch(str(tag)))


def ha_vkey(v: str | None) -> tuple[int, ...]:
    """Sort key for Home Assistant versions: a beta (2026.9.0b2) sorts before
    its release (2026.9.0), unlike vkey."""
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)(?:b(\d+))?\s*", v or "")
    if not m:
        return vkey(v) + (1, 0)
    y, mo, p, b = m.groups()
    return (int(y), int(mo), int(p), 0 if b is not None else 1, int(b or 0))
