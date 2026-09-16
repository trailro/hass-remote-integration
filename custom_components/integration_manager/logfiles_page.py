"""Log files the running integration writes, shown as a table.

Discovery, in this order: paths found in the running integration's config
entries (any string value ending in .log, plus rotated siblings), the
registry's ``log_dir`` directory, and ``*.log`` / ``*.log.*`` files in the
config dir root that are not Home Assistant's own.  ``/logfiles`` is the
page, ``/api/log_files`` the file list and ``/api/log_files/tail`` the last
lines of one file (default 50).

How a line becomes columns is the user's ``log_format`` setting (rules in
``clean_log_format``); without one, or when a line does not match, the line
is shown whole."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any

try:
    import regex as _regex  # matching with a time limit; the stdlib re has none
except ImportError:  # pragma: no cover - requirements.txt installs it
    _regex = None

from aiohttp import web
from homeassistant.core import HomeAssistant

from .http_util import ManagerView
from .ui import load_template, render

ACTIVE_S = 3600  # written within the last hour = "active"
NOT_OURS = ("home-assistant.log", "OZW_Log", "ha-install.log")
DEFAULT_LINES = 50
MAX_LINES = 5000
MAX_SCAN_BYTES = 32 * 1024 * 1024  # a filter that matches nothing must not read a 7-day log
# lines the filter matched only inside a value the mask removes: each costs the one-line rules (~35 us), so a
# search inside the password of every line stops here (~0.7 s) instead of at the byte budget
MAX_MASKED_OUT = 20_000

FORMAT_KEYS = ("pattern", "hide", "dim", "color_by", "colors")
FORMAT_COLORS = ("ok", "warn", "bad", "accent", "muted")
MAX_PATTERN = 2000
MAX_COLUMNS = 30
MAX_COLORS = 50
MATCH_BUDGET_S = 2.0  # per request: a pattern too slow for the lines on screen falls back to whole lines
MAX_MATCH_CHARS = 4096

LOGFILES_HTML = load_template("logfiles")
# the key of the file ids, new on every start: an id is a keyed hash of the file's real name, so the page can
# select a file whose masked name it shares with another without the real name ever reaching the page, and
# nobody can test a guess of that name against an id
_FILE_ID_KEY = secrets.token_bytes(32)


def clean_log_format(value: Any) -> tuple[dict[str, Any], str | None]:
    """Validate a log format; returns (format, error).  Empty clears it.

    A format is a JSON object: ``pattern`` (required) is a Python regular
    expression matched at the start of each line, whose named groups become
    the columns in order; ``hide`` lists groups not shown, ``dim`` groups
    shown muted; ``color_by`` names the group whose value picks the row
    colour from ``colors`` (value -> one of FORMAT_COLORS)."""
    if value is None or value == "" or value == {}:
        return {}, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as err:
            return {}, f"not valid JSON ({err})"
    if not isinstance(value, dict):
        return {}, "must be a JSON object"
    unknown = sorted(set(value) - set(FORMAT_KEYS))
    if unknown:
        return {}, f"unknown keys: {', '.join(map(str, unknown))} (allowed: {', '.join(FORMAT_KEYS)})"
    pattern = value.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return {}, "pattern is required"
    if len(pattern) > MAX_PATTERN:
        return {}, f"pattern is longer than {MAX_PATTERN} characters"
    try:
        rx = (_regex or re).compile(pattern)
    except (re.error, getattr(_regex, "error", re.error)) as err:
        return {}, f"pattern does not compile: {err}"
    groups = sorted(rx.groupindex, key=rx.groupindex.get)
    if not groups:
        return {}, "pattern needs at least one named group, (?P<name>...)"
    if len(groups) > MAX_COLUMNS:
        return {}, f"at most {MAX_COLUMNS} named groups"
    out: dict[str, Any] = {"pattern": pattern}
    for key in ("hide", "dim"):
        names = value.get(key) or []
        if not isinstance(names, list) or any(n not in groups for n in names):
            return {}, f"{key} must be a list of group names from the pattern ({', '.join(groups)})"
        if names:
            out[key] = list(dict.fromkeys(names))
    if all(g in out.get("hide", []) for g in groups):
        return {}, "hide leaves no column to show"
    color_by = value.get("color_by")
    if color_by not in (None, ""):
        if color_by not in groups:
            return {}, f"color_by must be a group name from the pattern ({', '.join(groups)})"
        out["color_by"] = color_by
    colors = value.get("colors") or {}
    if (not isinstance(colors, dict) or len(colors) > MAX_COLORS
            or any(len(str(k)) > 100 or c not in FORMAT_COLORS for k, c in colors.items())):
        return {}, f"colors maps values of color_by to one of {', '.join(FORMAT_COLORS)} (at most {MAX_COLORS})"
    if colors:
        if "color_by" not in out:
            return {}, "colors needs color_by"
        out["colors"] = {str(k): c for k, c in colors.items()}
    return out, None


def _entry_paths(hass, domain: str | None) -> list[str]:
    """String values ending in .log anywhere in the running domain's config
    entries (an integration that lets the user choose its log file)."""
    out: list[str] = []
    if not domain or hass is None:
        return out

    def walk(v: Any) -> None:
        if isinstance(v, str) and v.lower().endswith(".log"):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    for e in hass.config_entries.async_entries(domain):
        walk(dict(e.data))
        walk(dict(e.options))
    return out


def _log_files(config_dir: str, installer, entry_paths: list[str]) -> list[dict[str, Any]]:
    """Blocking.  ``entry_paths``: _entry_paths() read on the event loop."""
    domain = installer.running if installer else None
    now = time.time()
    seen: dict[str, dict[str, Any]] = {}

    def add(path: str, source: str) -> None:
        if os.path.islink(path):
            return  # a link could name any file under the config dir (secrets.yaml) as a log
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(config_dir) + os.sep) or not os.path.isfile(real):
            return  # only files under the config dir
        rel = os.path.relpath(real, config_dir)
        if rel in seen or rel.startswith(("integration_manager/", "venv-", "backups/", ".storage/")):
            return
        if any(os.path.basename(rel).startswith(x) for x in NOT_OURS):
            return
        try:
            st = os.stat(real)
        except OSError:
            return  # rotated away between the listing and the stat
        if st.st_nlink > 1:
            # a hard link is a second name for one file, so nothing about the path says whether this
            # is a log or secrets.yaml under a .log name (realpath cannot tell them apart the way it
            # does for a symlink).  Rotation by rename or by copy leaves one link, so a file with
            # more than one was linked on purpose and nothing legitimate is lost by skipping it.
            return
        seen[rel] = {"name": rel, "path": real, "bytes": st.st_size, "mtime": st.st_mtime, "active": now - st.st_mtime < ACTIVE_S, "source": source}

    for p in entry_paths:
        base = p if os.path.isabs(p) else os.path.join(config_dir, p)
        add(base, "config entry")
        d, n = os.path.dirname(base), os.path.basename(base)
        try:
            for name in os.listdir(d):
                if name.startswith(n + "."):  # rotated siblings (my.log.2026-09-10 ...)
                    add(os.path.join(d, name), "config entry (rotated)")
        except OSError:
            pass
    sub_dir = installer.spec(domain).get("log_dir") if installer and domain else None
    if isinstance(sub_dir, str) and sub_dir and not sub_dir.startswith("/") and ".." not in sub_dir:
        try:
            for name in os.listdir(os.path.join(config_dir, sub_dir)):
                if name.endswith(".log") or ".log." in name:
                    add(os.path.join(config_dir, sub_dir, name), "registry log_dir")
        except OSError:
            pass
    try:
        for name in os.listdir(config_dir):
            if name.endswith(".log") or ".log." in name:
                add(os.path.join(config_dir, name), "config root")
    except OSError:
        pass
    out = list(seen.values())
    # active first, current file before rotated ones, newest first
    out.sort(key=lambda f: (not f["active"], not f["name"].endswith(".log"), -f["mtime"]))
    return out


def _file_id(name: str) -> str:
    """The id of a listed file: what the Log files page selects a file by.
    It only names a file of the listing it is resolved against (the tail
    view compares it with the id of every file _log_files returns), so it
    cannot reach a file the listing does not offer."""
    return hmac.new(_FILE_ID_KEY, name.encode("utf-8", "surrogateescape"), hashlib.sha256).hexdigest()[:32]


def _tail(path: str, lines: int, needle: str) -> tuple[list[str], int]:
    """Last `lines` lines matching `needle`, reading the file backwards in
    blocks so a 7-day log is never loaded whole.

    Key material is masked as each block is read, before `needle` decides
    which lines survive: the window the caller gets back is the window a
    scrubber can no longer make sense of, so a search for bytes of a key
    ("MIIF...") would otherwise pick the body out of its block and hand it
    back with the BEGIN line the scrubber needs left behind.  The masking
    runs on the whole block, which is contiguous, so the state of a block
    that spans two reads carries from the newer to the older one; the search
    then decides on the masked text, and finds nothing where a key was.
    Masking the scanned lines and not scrubbing them is what keeps the cost:
    it is a length test and one substring test per line, so a tail of a 99 MB
    log stays in the milliseconds.  A line the needle matches is masked by the
    one-line rules as well (a password, a token, a cookie) and has to match
    again: a search for "hunter" otherwise returned ``password=***`` for as
    long as the guess was a prefix of the password.  Only matching lines pay
    for those rules, and the scan stops after MAX_MASKED_OUT lines that
    matched only inside a masked value."""
    from .diagnostics import _scrub_one_line_rules, mask_key_material_lines  # diagnostics imports this module

    needle = needle.lower()
    found: list[str] = []
    scanned = masked_out = 0
    in_block = False  # inside a key block whose END marker a newer block already passed
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        buf = b""
        block = 64 * 1024
        scanned_bytes = 0
        while pos > 0 and len(found) < lines and scanned_bytes < MAX_SCAN_BYTES and masked_out < MAX_MASKED_OUT:
            step = min(block, pos)
            scanned_bytes += step
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            parts = buf.split(b"\n")
            buf = parts[0]  # possibly partial first line, keep for next round
            chunk, in_block = mask_key_material_lines(
                [raw.decode("utf-8", errors="replace") for raw in parts[1:]], in_block)
            for text in reversed(chunk):
                if not text.strip():
                    continue
                scanned += 1
                if needle and needle not in text.lower():
                    continue
                if needle and needle not in _scrub_one_line_rules(text).lower():
                    masked_out += 1
                    if masked_out >= MAX_MASKED_OUT:
                        break
                    continue
                found.append(text)
                if len(found) >= lines:
                    break
        if pos == 0 and buf.strip() and len(found) < lines and scanned_bytes < MAX_SCAN_BYTES + block and masked_out < MAX_MASKED_OUT:
            text = mask_key_material_lines([buf.decode("utf-8", errors="replace")], in_block)[0][0]
            scanned += 1
            if not needle or (needle in text.lower() and needle in _scrub_one_line_rules(text).lower()):
                found.append(text)
    found.reverse()
    return found, scanned


def _tail_masked(path: str, lines: int, needle: str) -> tuple[list[str], int]:
    """_tail with secrets masked by the diagnostics scrubber (the rules of the zip).

    _tail has already masked key material, which is the part the search must
    not be able to select on; the rest of the rules (a password in a line, a
    bearer token, a cookie) match within one line; _tail searched the text
    they leave, and they run here on the lines that survived the search, not
    on everything the scan read.  The window is masked as one text, which can
    mask more than the lines did one by one (a BEGIN line the search kept
    above lines that look like a body), so the search is asked once more."""
    from .diagnostics import scrub_lines  # diagnostics imports this module

    found, scanned = _tail(path, lines, needle)
    return [line for line in scrub_lines(found) if needle.lower() in line.lower()], scanned


def _format_lines(fmt: dict[str, Any], raw_lines: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """(columns, rows, error); a row has ``cells`` (None when the line did not
    match), ``raw`` and ``color``.  Matching has a time budget per request:
    a user's pattern can backtrack for hours on one line, and this runs in
    the executor on every follow poll."""
    if not fmt:
        return [], [{"raw": raw, "cells": None, "color": None} for raw in raw_lines], None
    if _regex is None:  # the stdlib re cannot be stopped mid-match: no formatting rather than a hung server
        return [], [{"raw": raw, "cells": None, "color": None} for raw in raw_lines], \
            "formatting needs the regex package, missing from this Home Assistant venv: restart the container to install it"
    rx = _regex.compile(fmt["pattern"])
    hide, dim = set(fmt.get("hide") or ()), set(fmt.get("dim") or ())
    shown = [g for g in sorted(rx.groupindex, key=rx.groupindex.get) if g not in hide]
    color_by, colors = fmt.get("color_by"), fmt.get("colors") or {}
    deadline = time.monotonic() + MATCH_BUDGET_S
    error = None
    rows = []
    for raw in raw_lines:
        m = None
        if error is None and len(raw) <= MAX_MATCH_CHARS:
            left = deadline - time.monotonic()
            try:
                if left <= 0:
                    raise TimeoutError
                m = rx.match(raw, timeout=left)
            except TimeoutError:
                error = f"the format needed more than {MATCH_BUDGET_S:g} s for these lines, so the rest are shown whole: simplify the pattern"
        if not m:
            rows.append({"raw": raw, "cells": None, "color": None})
            continue
        d = m.groupdict()
        rows.append({"raw": raw, "cells": [(d.get(g) or "").strip() for g in shown],
                     "color": colors.get((d.get(color_by) or "").strip()) if color_by else None})
    return [{"name": g, "dim": g in dim} for g in shown], rows, error

class LogFilesPageView(ManagerView):
    url = "/logfiles"

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=render(LOGFILES_HTML, "/logfiles"), content_type="text/html")


class LogFilesView(ManagerView):
    url = "/api/log_files"

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            # the names and sizes of the files, from a walk of the config dir: for this UI, like the tail,
            # not for a request any page can make
            return self.json_message("X-Requested-With: fetch required", status_code=400)
        from .diagnostics import scrub  # diagnostics imports this module

        files = await self.hass.async_add_executor_job(_log_files, self.hass.config.config_dir, self.installer, _entry_paths(self.hass, self.installer.running))
        # a name comes from the config dir (an integration that names its log file after what it
        # connects to) and from the registry's log_dir: scrubbed like the lines inside the file
        # two names can mask to the same text (logs/session-token=alpha.log, logs/session-token=beta.log): the
        # page selects a file by its id, the masked name is only its label
        return self.json([{**{k: v for k, v in f.items() if k != "path"}, "name": scrub(f["name"]), "id": _file_id(f["name"])}
                          for f in files])


class LogFileTailView(ManagerView):
    url = "/api/log_files/tail"

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            return self.json_message("X-Requested-With: fetch required", status_code=400)  # log lines are for this UI, not for any page's requests
        from .diagnostics import scrub  # diagnostics imports this module

        q = request.query
        try:
            lines = max(1, min(int(q.get("lines", DEFAULT_LINES) or DEFAULT_LINES), MAX_LINES))
        except ValueError:
            return self.json_message("lines must be an integer", status_code=400)
        fmt, fmt_error = clean_log_format(self.installer.settings.data.get("log_format"))
        files = await self.hass.async_add_executor_job(_log_files, self.hass.config.config_dir, self.installer, _entry_paths(self.hass, self.installer.running))
        if not files:
            return self.json({"path": None, "bytes": None, "columns": [], "lines": [], "total_lines_scanned": 0, "format_error": fmt_error})
        # never open arbitrary paths: a file of this listing, by its id (the page) or by the masked name the
        # listing shows (an API caller).  Not by its real name: the listing never gives that out, and accepting
        # it would confirm a guess of what the mask hides
        if q.get("id"):
            matches = [f for f in files if _file_id(f["name"]) == q["id"]]
        elif q.get("file"):
            matches = [f for f in files if scrub(f["name"]) == q["file"]]
        else:
            matches = files[:1]
        if not matches:
            return self.json_message("unknown file", status_code=404)
        if len(matches) > 1:
            return self.json_message("several log files show this name: select the file by its id", status_code=409)
        chosen = matches[0]
        try:
            raw_lines, scanned = await self.hass.async_add_executor_job(_tail_masked, chosen["path"], lines, q.get("q", ""))
        except OSError as err:
            return self.json_message(f"cannot read the file (rotated away?): {err}", status_code=404)
        columns, rows, slow = await self.hass.async_add_executor_job(_format_lines, fmt, raw_lines)
        return self.json({"path": scrub(chosen["path"]), "bytes": chosen["bytes"], "total_lines_scanned": scanned,
                          "columns": columns, "lines": rows, "format_error": fmt_error or slow})
