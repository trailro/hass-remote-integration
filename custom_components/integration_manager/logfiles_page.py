"""Log files the running integration writes, shown as a table.

Discovery, in this order: paths found in the running integration's config
entries (any string value ending in .log, plus rotated siblings), the
registry's ``log_dir`` directory, and ``*.log`` / ``*.log.*`` files in the
config dir root that are not Home Assistant's own.  ``/logfiles`` is the
page, ``/api/log_files`` the file list and ``/api/log_files/tail`` the last
lines of one file (default 50) and ``/api/log_files/download`` the whole
of one file, masked the same way and streamed as an attachment.

How a line becomes columns is the user's ``log_format`` setting (rules in
``clean_log_format``); without one, or when a line does not match, the line
is shown whole."""

from __future__ import annotations

import codecs
import errno
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from typing import Any
from urllib.parse import unquote_plus

try:
    import regex as _regex  # matching with a time limit; the stdlib re has none
except ImportError:  # pragma: no cover - requirements.txt installs it
    _regex = None

from aiohttp import web
from homeassistant.core import HomeAssistant

from .hostguard import CSP
from .http_util import ManagerView
from .ui import load_template, render

ACTIVE_S = 3600  # written within the last hour = "active"
NOT_OURS = ("home-assistant.log", "OZW_Log", "ha-install.log")
DEFAULT_LINES = 50
MAX_LINES = 5000
MAX_SCAN_BYTES = 32 * 1024 * 1024  # a filter that matches nothing must not read a 7-day log
# lines a search runs the one-line rules on (~40 us each): a log with a token on every line stops here (~0.8 s)
# instead of at the byte budget.  Counted on every line the rules could change, whatever the search is for, so
# where a search stops says nothing about what the rules hide
MAX_MASKED_OUT = 20_000
# a download sends the last MAX_SCAN_BYTES of a longer file: the tail's budget, for the same reason.  Masking is
# what the operator is given the file for and it costs per line, so an uncapped download would hold an executor
# thread for as long as the log is long (and the page hands the answer to the browser as one blob).  The last
# bytes are the newest lines, which is what a log is downloaded for; the answer says so in the file name and in
# the X-Log-Truncated header, and the page says so next to the button
DOWNLOAD_CHUNK = 256 * 1024  # text masked per executor job: the event loop runs between chunks
MAX_LINE_CHARS = 1024 * 1024  # a file with no newline in it must not be held whole before it can be masked
MAX_DOWNLOAD_NAME = 120
# what the one-line rules of diagnostics._scrub_one_line_rules need to find before they change a line: a line
# holding none of these, as it is or percent-decoded (logbuffer.mask_query_secrets decides on the decoded name and
# path: ?%73ession=), comes out of them unchanged, so the search skips them for it.  A rule added there needs its
# literal here (the tests list every rule the function runs, and compare the two)
_RULE_LITERALS = ("pass", "token", "secret", "credential", "psk", "hmac", "key", "webhook_id", "cloudhook_url", "pin",
                  "sig", "code", "otp", "pwd", "pw", "session", "irk", "ltk", "csrk", "cookie", "authorization",
                  "bearer", "basic", "auth", "://", "gh", "github_pat_",
                  "api")  # an access line of a log search, in any spelling of its path: its values are masked (logbuffer)
# the rules are case-insensitive, and re's IGNORECASE takes four characters beyond ASCII for letters: lower() alone
# leaves the long s and the dotless i as they are, and turns the dotted I into "i" plus a combining dot
_FOLD = str.maketrans({"\u017f": "s", "\u0131": "i", "\u0130": "i", "\u212a": "k"})


def _rules_may_change(text: str) -> bool:
    folded = text.translate(_FOLD).lower()
    if "%" in text:
        # decoded as well as raw: decoding can also take a literal apart (%ab + asic is not "basic" any more)
        folded += "\n" + unquote_plus(text).translate(_FOLD).lower()
    return any(k in folded for k in _RULE_LITERALS)


FORMAT_KEYS = ("pattern", "hide", "dim", "color_by", "colors")
FORMAT_COLORS = ("ok", "warn", "bad", "accent", "muted")
MAX_PATTERN = 2000
MAX_COLUMNS = 30
MAX_COLORS = 50
MATCH_BUDGET_S = 2.0  # per request: a pattern too slow for the lines on screen falls back to whole lines
MAX_MATCH_CHARS = 4096
# compiling has no time limit: the regex package's compile (in its C part) expands a counted repeat once per copy
# ((?P<a>a{60000}){60000}, 22 characters, ran 23 s and was killed for its memory).  A pattern is refused when its elements, each counted once
# per copy the repeats around it make, add up to more than this (~5 ms to compile at the limit, whatever the element)
MAX_PATTERN_WEIGHT = 10_000
_MAX_FLAG_PASSES = 32  # the regex package retries without a bound; each pass adds at least one global flag

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
    colour from ``colors`` (value -> one of FORMAT_COLORS).

    Blocking: it compiles the pattern (cached per pattern)."""
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
    rx, error = _compiled(pattern)
    if error:
        return {}, error
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


def _pattern_weight(pattern: str, limit: int) -> int:
    """Blocking: the elements of ``pattern`` as the regex package parses it,
    each counted once per copy the repeats around it make ({n} and {m,n}
    make n, {m,} makes m, {0} one), counting stopped once past ``limit``.

    The package's own parser, not a scan of the text: it is the lexer the
    compile uses, so a count spelled ``{6 0 0 0 0}`` in verbose mode, a brace
    in a character class or an escaped one reads here as it reads there."""
    core = _regex._regex_core
    global_flags = 0
    for _ in range(_MAX_FLAG_PASSES):
        source = core.Source(pattern)
        info = core.Info(global_flags, source.char_type, {})
        source.ignore_space = bool(info.flags & core.VERBOSE)
        try:
            tree = core._parse_pattern(source, info)
            break
        except core._UnscopedFlagSet:
            # a global flag after the start: parsed again with the flags seen so far set, as the compile does (once
            # per such flag: (?b), (?e), (?p), (?r) can all follow one another)
            global_flags = info.global_flags
    else:
        raise RuntimeError(f"still finding global flags after {_MAX_FLAG_PASSES} passes")
    total = 0
    stack = [(tree, 1)]
    while stack and total <= limit:
        node, copies = stack.pop()
        total += copies
        if isinstance(node, core.GreedyRepeat):  # lazy and possessive repeats are subclasses
            copies *= max(1, node.min_count if node.max_count is None else node.max_count)
        for name, value in vars(node).items():
            if name != "_key":
                stack.extend((child, copies) for child in (value if isinstance(value, (list, tuple)) else (value,))
                             if isinstance(child, core.RegexBase))
    return total


@functools.lru_cache(maxsize=16)
def _compiled(pattern: str) -> tuple[Any, str | None]:
    """Blocking: (compiled pattern, None) or (None, why not), cached per
    pattern (the Log files page formats with the stored one on every poll)."""
    try:
        if _regex is not None:  # the stdlib re compiles a counted repeat as one instruction
            try:
                weight = _pattern_weight(pattern, MAX_PATTERN_WEIGHT)
            except (_regex.error, RecursionError):
                raise
            except Exception as err:  # noqa: BLE001 - never a 500, never an unchecked pattern
                # the parser is internal to the regex package, which is not pinned: a release that changed it (or a
                # pattern it parses in a way not foreseen here) must neither let the pattern through unchecked nor
                # answer 500
                return None, f"pattern cannot be checked with this version of the regex package ({type(err).__name__}: {err})"
            if weight > MAX_PATTERN_WEIGHT:
                return None, (f"pattern repeats too much: with its counted repeats ({{n}}, {{m,n}}) written out it is longer "
                              f"than {MAX_PATTERN_WEIGHT} elements (use * or + for a field of any length)")
        return (_regex or re).compile(pattern), None
    except (re.error, getattr(_regex, "error", re.error), ValueError) as err:  # ValueError: conflicting flags, (?aL)
        return None, f"pattern does not compile: {err}"
    except RecursionError:
        return None, "pattern does not compile: groups nested too deeply"


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
    root = os.path.realpath(config_dir)  # a config dir reached through a link: names relative to where the files are

    def add(path: str, source: str) -> None:
        if os.path.islink(path):
            return  # a link could name any file under the config dir (secrets.yaml) as a log
        real = os.path.realpath(path)
        if not real.startswith(root + os.sep) or not os.path.isfile(real):
            return  # only files under the config dir
        rel = os.path.relpath(real, root)
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


def open_log_file(path: str) -> Any:
    """A listed log file opened for reading, checked at the file that is
    actually opened and not at the name.

    Raises OSError (a view answers 404, the zip writes the reason in place of
    the tail) when the file is gone, is a link, or has more than one name.
    The listing skips a symlink and a file with more than one hard link (see
    _log_files), but it was taken before this request: a name that became a
    link in between is refused here, so a listed ``my.log`` pointing at
    ``secrets.yaml`` cannot hand that file out.  Every reader of a listed path
    goes through here - the tail, the download and the diagnostics zip - so
    the check cannot be had by one and missed by another."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise OSError(errno.EPERM, "not a plain file with a single name")
        return open(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


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
    log stays in the milliseconds.  A search also decides on the text the
    one-line rules (a password, a token, a cookie) leave: a search for "hunter"
    otherwise returned ``password=***`` for as long as the guess was a prefix
    of the password.  The rules run on every scanned line they could change,
    not only on the lines the search matched: when only matching lines paid
    for them, a search matching inside a password on many lines hit the budget
    (and took the time) that a wrong guess did not, and total_lines_scanned
    answered the question the rows no longer did.  Lines holding none of the
    rules' literals skip them, and the scan stops after MAX_MASKED_OUT lines
    that ran them."""
    from .diagnostics import _scrub_one_line_rules, mask_key_material_lines  # diagnostics imports this module

    needle = needle.lower()
    found: list[str] = []
    scanned = ruled = 0
    in_block = False  # inside a key block whose END marker a newer block already passed

    def matches(text: str) -> bool:
        nonlocal ruled
        if not needle:
            return True
        if _rules_may_change(text):
            ruled += 1
            text = _scrub_one_line_rules(text)  # before the search, whether or not the raw line holds the needle
        return needle in text.lower()

    def cut(raw: bytes, over: int) -> tuple[str, int]:
        # a line longer than MAX_LINE_CHARS keeps its start (time, level, logger); the rest is counted, not held
        over += max(0, len(raw) - MAX_LINE_CHARS)
        return raw[:MAX_LINE_CHARS].decode("utf-8", errors="replace"), over

    def shown(text: str, over: int) -> str:
        # said after masking and searching: the note must not change what either decides
        return f"{text} [... {over} more bytes of this line not shown]" if over else text

    with open_log_file(path) as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        buf = b""
        over = 0  # bytes of the partial line in buf already cut off its end
        block = 64 * 1024
        scanned_bytes = 0
        while pos > 0 and len(found) < lines and scanned_bytes < MAX_SCAN_BYTES and ruled < MAX_MASKED_OUT:
            step = min(block, pos)
            scanned_bytes += step
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            parts = buf.split(b"\n")
            buf = parts[0]  # possibly partial first line, keep for next round
            cuts = [cut(raw, 0) for raw in parts[1:]]
            if cuts and over:
                cuts[-1] = cut(parts[-1], over)  # the long line ends here: its start is in this block
                over = 0
            if len(buf) > MAX_LINE_CHARS:
                # a line with no newline for this long: only its start is kept, so no read copies more than that
                over += len(buf) - MAX_LINE_CHARS
                buf = buf[:MAX_LINE_CHARS]
            chunk, in_block = mask_key_material_lines([t for t, _ in cuts], in_block)
            for text, (_, cut_off) in zip(reversed(chunk), reversed(cuts)):
                if not text.strip():
                    continue
                scanned += 1
                if matches(text):
                    found.append(shown(text, cut_off))
                    if len(found) >= lines:
                        break
                if ruled >= MAX_MASKED_OUT:
                    break
        if pos == 0 and buf.strip() and len(found) < lines and scanned_bytes < MAX_SCAN_BYTES + block and ruled < MAX_MASKED_OUT:
            first, cut_off = cut(buf, over)
            text = mask_key_material_lines([first], in_block)[0][0]
            scanned += 1
            if matches(text):
                found.append(shown(text, cut_off))
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


def _mask_forward_lines(lines: list[str], in_block: bool) -> tuple[list[str], bool]:
    """``lines`` masked for a read that goes forward through a file, plus the
    key-block state to hand to the next batch.

    diagnostics.scrub_lines does the masking the page shows (a whole PEM
    block, key material with no marker left above it, and the one-line
    rules), and it only sees the batch it is given: a block whose ``BEGIN``
    line was in an earlier batch is not a block to it.  So the marker is
    tracked here across batches, and every line below such a ``BEGIN`` is
    masked whole until the ``END`` marker, whatever it holds.
    diagnostics.mask_key_material_lines carries the same state the other way,
    for a tail, which reads backwards from an ``END`` marker upwards."""
    from .diagnostics import scrub_lines  # diagnostics imports this module

    inside = []
    for line in lines:
        begin, end = "-----BEGIN " in line, "-----END " in line
        inside.append(in_block and not end)
        if begin or end:
            in_block = begin and not end  # a marker pair on one line opens nothing
    return ["***" if hidden else text for hidden, text in zip(inside, scrub_lines(lines))], in_block


class _MaskedDownload:
    """Blocking: one log file read forward and masked, a chunk at a time.

    The file is never held: a chunk of text is read, masked and handed to the
    caller, which writes it to the response before asking for the next one.
    A file longer than MAX_SCAN_BYTES is sent from its end (the newest lines,
    as a tail), starting at the first whole line; ``truncated`` says so."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.truncated = False
        self._fh: Any = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")  # a multi-byte character across a chunk boundary
        self._pending = ""
        self._in_block = False
        self._left = MAX_SCAN_BYTES

    def open(self) -> None:
        """Raises OSError (the view answers 404) when the file is gone, is a
        link, or has more than one name (open_log_file does the checking)."""
        self._fh = open_log_file(self.path)
        size = self._fh.seek(0, os.SEEK_END)
        start = max(0, size - MAX_SCAN_BYTES)
        self._fh.seek(start)
        if start:
            self.truncated = True
            self._fh.readline(MAX_LINE_CHARS)  # the line the cut fell in: dropped rather than sent as half a line

    def chunk(self) -> bytes:
        """The next masked chunk, or b"" at the end of what is sent."""
        while True:
            raw = self._fh.read(min(DOWNLOAD_CHUNK, self._left)) if self._left > 0 else b""
            self._left -= len(raw)
            self._pending += self._decoder.decode(raw, not raw)
            if not raw:
                lines, self._pending = ([self._pending] if self._pending else []), ""
                return self._masked(lines, terminated=False)  # the file's last line, as it ended
            lines = self._pending.split("\n")
            self._pending = lines.pop()  # a line the chunk cut in two: masked with the rest of it, next round
            terminated = True
            if len(self._pending) > MAX_LINE_CHARS:
                # a file with no newline in it: sent on without one, so what follows joins it again
                lines.append(self._pending)
                self._pending, terminated = "", False
            if lines:
                return self._masked(lines, terminated)

    def _masked(self, lines: list[str], terminated: bool) -> bytes:
        if not lines:
            return b""
        masked, self._in_block = _mask_forward_lines(lines, self._in_block)
        return ("\n".join(masked) + ("\n" if terminated else "")).encode("utf-8")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _download_name(masked_name: str, truncated: bool) -> str:
    """A file name for the Content-Disposition, from the masked name the
    listing shows: the real name is never given out, and what is left is cut
    down to what every file system takes (the mask itself writes ``***``).

    A mask can take the extension with it (``session-token=alpha.log`` is
    masked from the ``=`` on), so a name left without one gets ``.log``: what
    is saved is a log file and should open as one."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", masked_name)[-MAX_DOWNLOAD_NAME:].strip("-.") or "log"
    if not os.path.splitext(safe)[1]:
        safe += ".log"
    if truncated:
        root, ext = os.path.splitext(safe)
        safe = f"{root}-last-{MAX_SCAN_BYTES // (1024 * 1024)}MiB{ext}"
    return safe


async def _select_file(hass: HomeAssistant, installer, query) -> tuple[dict[str, Any] | None, str | None, int]:
    """(file, refusal, status) for the file ``query`` names in the listing as
    it is now, shared by the tail and the download so both are reached the
    same way.

    Never an arbitrary path: a file of this listing, by its id (the page) or
    by the masked name the listing shows (an API caller).  Not by its real
    name, which the listing never gives out and which would confirm a guess
    at what the mask hides.  An empty listing is (None, None, 0): what to
    answer for it is the caller's."""
    from .diagnostics import scrub  # diagnostics imports this module

    files = await hass.async_add_executor_job(
        _log_files, hass.config.config_dir, installer, _entry_paths(hass, installer.running))
    if not files:
        return None, None, 0
    if query.get("id"):
        matches = [f for f in files if _file_id(f["name"]) == query["id"]]
    elif query.get("file"):
        matches = [f for f in files if scrub(f["name"]) == query["file"]]
    else:
        matches = files[:1]
    if not matches:
        return None, "unknown file", 404
    if len(matches) > 1:
        return None, "several log files show this name: select the file by its id", 409
    return matches[0], None, 200


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
    rx, error = _compiled(fmt["pattern"])
    if rx is None:
        return [], [{"raw": raw, "cells": None, "color": None} for raw in raw_lines], error
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
        fmt, fmt_error = await self.hass.async_add_executor_job(clean_log_format, self.installer.settings.data.get("log_format"))
        chosen, refusal, status = await _select_file(self.hass, self.installer, q)
        if chosen is None:
            if not status:  # no log file at all: an empty window, not an error
                return self.json({"path": None, "bytes": None, "columns": [], "lines": [], "total_lines_scanned": 0, "format_error": fmt_error})
            return self.json_message(refusal, status_code=status)
        try:
            raw_lines, scanned = await self.hass.async_add_executor_job(_tail_masked, chosen["path"], lines, q.get("q", ""))
        except OSError as err:
            # strerror, not the exception: its text carries the path it failed on
            return self.json_message(f"cannot read the file (rotated away?): {err.strerror or type(err).__name__}", status_code=404)
        columns, rows, slow = await self.hass.async_add_executor_job(_format_lines, fmt, raw_lines)
        return self.json({"path": scrub(chosen["path"]), "bytes": chosen["bytes"], "total_lines_scanned": scanned,
                          "columns": columns, "lines": rows, "format_error": fmt_error or slow})


class LogFileDownloadView(ManagerView):
    """The whole of one log file, masked as the page masks it, as an attachment.

    The gates are the tail's: the header this UI sends, and a file of the
    current listing reached by its id or its masked name, so a link on any
    page cannot pull a log and no path can be asked for.  Masking runs on the
    stream, chunk by chunk in the executor, so the answer is never built in
    memory and a long file cannot be handed over faster than it can be
    masked.  A file longer than MAX_SCAN_BYTES is sent from its end and the
    answer says so (the file name, and X-Log-Truncated)."""

    url = "/api/log_files/download"

    def __init__(self, hass: HomeAssistant, installer) -> None:
        self.hass = hass
        self.installer = installer

    async def get(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("X-Requested-With") != "fetch":
            return self.json_message("X-Requested-With: fetch required", status_code=400)  # a log file is for this UI, not for any page's requests
        from .diagnostics import scrub  # diagnostics imports this module

        chosen, refusal, status = await _select_file(self.hass, self.installer, request.query)
        if chosen is None:
            return self.json_message(refusal or "the running integration writes no log file", status_code=status or 404)
        reader = _MaskedDownload(chosen["path"])
        try:
            await self.hass.async_add_executor_job(reader.open)
        except OSError as err:
            # strerror, not the exception: its text carries the path it failed on
            return self.json_message(f"cannot read the file (rotated away, or no longer a plain file): "
                                     f"{err.strerror or type(err).__name__}", status_code=404)
        name = _download_name(scrub(chosen["name"]), reader.truncated)
        response = web.StreamResponse(headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            # prepared inside the handler, so the host guard no longer gets to put the policy on it
            "Content-Security-Policy": CSP,
            **({"X-Log-Truncated": str(MAX_SCAN_BYTES)} if reader.truncated else {}),
        })
        try:
            await response.prepare(request)
            while chunk := await self.hass.async_add_executor_job(reader.chunk):
                await response.write(chunk)
            await response.write_eof()
        except OSError:
            pass  # rotated away while it was being sent: the answer has begun, so it can only end short
        finally:
            await self.hass.async_add_executor_job(reader.close)
        return response
