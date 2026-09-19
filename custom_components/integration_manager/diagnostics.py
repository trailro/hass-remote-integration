"""GET /api/diagnostics: a zip for bug reports (upstream or here): versions,
manifest, requirement versions, patches, health, MQTT/HA/manager status,
a memory snapshot, the last log records and the tail of the integration's newest log file.  Secrets are
scrubbed (values of keys that look like passwords/tokens, the GitHub token,
the MQTT password, the text of a log search in a request line).
The raw credential-bearing files (settings.json, mqtt.json on the volume) are
excluded; sanitized public views of them are included (settings.json,
mqtt-config.json)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
import zipfile
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from .http_util import ManagerView

import logbuffer

from . import events, notifications
from .installer import Installer
from .logfiles_page import _entry_paths, _log_files, open_log_file
from .memdiag import snapshot as memory_snapshot
from .mqtt_publisher import SECRET_NAME_ENDINGS, SECRET_NAME_WORDS

# names ending in "key" that are known not to be secrets (everything else ending in "key" is masked)
_PLAIN_KEYS = logbuffer.PLAIN_KEYS
# the names the MQTT history, status and log mask (mqtt_publisher): anywhere in a name, and as a word of its own
_ENDINGS, _WORDS = "|".join(SECRET_NAME_ENDINGS), "|".join(SECRET_NAME_WORDS)
# "code" names an OAuth secret - code, user_code, device_code, pin_code and every other spelling of it.
# A code that reports a result is not one, and a bundle with every HTTP status masked is a bundle nobody
# can debug from, so those are excluded by name: a name nobody listed is masked rather than printed.  The
# dict rule and the text rule share this, so a name masks the same whether it arrives as a key or as a line
_RESULT_CODE_NAMES = ("status", "error", "exit", "return", "reason", "http", "response")
_NOT_RESULT_CODE = "".join(rf"(?<!{name}_)" for name in _RESULT_CODE_NAMES)
_SECRET_KEY = re.compile(
    rf"({_ENDINGS}|bearer|cookie|hmac|authorization|webhook_id|cloudhook_url|signature"
    rf"|(?:^|[_-]){_NOT_RESULT_CODE}code$"
    r"|(api|access|private|local|encryption|device|client|master|app|user|shared|signing|session|auth|link|network|aes|ssl)[_-]?key"
    rf"|^(?!{_PLAIN_KEYS}$).*key$"  # any *key: Z-Wave (lr_)s2_*_key, security_key, api-key, ...
    r"|(^|[_-])(irk|ltk|csrk|pwd|pw|sig|session_?id)$|(^|[_-])otp([_-]|$)"  # BLE bonding keys, one-time codes
    rf"|(^|[_-])(pass|{_WORDS})$)", re.I)
# a quoted value, to its closing quote: an escaped quote inside it (\" or \') does not end it, and a value whose quote
# never closes (a line the logger cut) is masked to the end of the text.  A name and value that are themselves inside a
# JSON string have their quotes escaped (\"password\": \"x\"): that value ends at the same run of backslashes and the
# same quote it opened with.  Every repeat is possessive and its branches start on different characters, so the match
# never backtracks: the one-line rules run on every line a search reads, whatever the line holds, so the runs
# that are not driven by a quote (_VALUE_CLOSE and _URL_CRED below) carry a bound instead
_QUOTED = (r"\"(?:[^\"\\]++|\\[\s\S])*+\"?|'(?:[^'\\]++|\\[\s\S])*+'?"
           r"|(?P<esc_run>\\++)(?P<esc_quote>[\"'])(?:[^\\]++|(?!(?P=esc_run)(?P=esc_quote))\\++[\"']?)*+(?:(?P=esc_run)(?P=esc_quote))?")
# a quoted value is often wrapped, and in more than one layer: a string repr (b'x', rb"x"), a constructor
# (SecretStr('x'), pydantic.SecretStr(value='x')), a container ({'password': ['x']}, ('user', 'x')), a repr
# (<SecretStr 'x'>), an auth scheme (Bearer 'x').  Without this the value alternative below took the unquoted
# branch, which stops at the first quote, and masked the wrapper instead of the secret ("password=b'hunter2'"
# -> "password=***'hunter2'").  One layer is an optional dotted name and an opening bracket, then optionally a
# keyword name or the type name of a repr inside it; after the layers come an optional auth scheme and one of
# Python's one- or two-letter string prefixes.  A bare identifier is never a layer of its own, because a secret
# that merely abuts a quote looks exactly like one (`RuntimeError("... access_token=SECRET")` ends the value at
# the closing quote of the message, and taking SECRET for a prefix printed it and masked the quote after it) -
# an identifier only counts inside a bracket that opened before it.  Every layer eats a bracket, so the repeat
# is bounded by the brackets on the line and nothing is read twice, and the whole prefix ends in a lookahead for
# the quote, so a value that is not quoted after all costs one failed test and no retries
_VALUE_SCHEMES = r"Bearer|Basic|Token|Digest|Negotiate|NTLM|OAuth|Hawk|ApiKey|SSWS"
_VALUE_WRAP = (r"(?:[A-Za-z_][\w.]*+)?+(?P<open>[(\[{<])[ \t]*+"
               r"(?:[A-Za-z_]\w*+(?:[ \t]*+=(?!=)|[ \t]++))?+")
_VALUE_PREFIX = (rf"(?P<pre>(?:{_VALUE_WRAP})*+(?:(?:{_VALUE_SCHEMES})[ \t]++)?+"
                 r"(?:[bBrRuUfF]{1,2})?+(?=\\*+['\"]))?+")
# a value that came inside a bracket is masked through to that bracket's close, so the rest of a tuple or a
# list goes with it: "auth=('user', 'hunter2')" masked 'user' and printed the password next to it.  The run is
# bounded and possessive, and the tail only runs at all when the prefix did open a bracket, so the plain
# "name": "value" of a JSON line keeps the comma and everything after it
_VALUE_CLOSE = r"(?(open)(?:[^)\]}>\r\n]{0,256}+(?P<close>[)\]}>]))?+)"
# an auth scheme belongs to the value it introduces ("token: Bearer abc..."): without this the value ended at
# the space after the scheme, so the scheme was masked and the token printed, and _BEARER never saw the line.
# This is the unquoted branch; the quoted one takes its scheme from _VALUE_PREFIX, because a possessive scheme
# here ate "Bearer " and then failed on the quote after it, and the whole line came out unmasked
_VALUE_SCHEME = rf"(?:(?:{_VALUE_SCHEMES})\s++)?+"
_SECRET_TEXT = re.compile(
    rf"((?:{_ENDINGS}|hmac|webhook_id|cloudhook_url|signature|(?<![A-Za-z0-9]){_NOT_RESULT_CODE}code|(?<![A-Za-z0-9])(?:{_WORDS})"
    rf"|\bpwd|\w_pw\b|\bsession_?id|\b(?:irk|ltk|csrk|sig)\b|\b(?!{_PLAIN_KEYS}\b)\w*key"
    r"|(?:api|access|private|local|encryption|device|client|master|app|shared|signing|session|auth|link|network|aes|ssl)[_-]?key)"
    r"(?:\\*+['\"])?\s*[=:]\s*)"
    rf"({_VALUE_PREFIX}(?:{_QUOTED}){_VALUE_CLOSE}|{_VALUE_SCHEME}[^'\",\s}}]+)", re.I)
# every name _SECRET_TEXT knows ends in a letter, then the = or :, so a text without this has nothing it masks; the
# rule is the costly one (a Logs page search masks every record it passes), and most log lines fail this test
_SECRET_TEXT_HINT = re.compile(r"[a-z](?:\\*+['\"])?\s*[=:]", re.I)
# the whole value of an Authorization header, scheme included (Digest, a custom scheme, a bare token)
_AUTH_TEXT = re.compile(rf"(authorization(?:\\*+['\"])?\s*[=:]\s*)"
                        rf"({_VALUE_PREFIX}(?:{_QUOTED}){_VALUE_CLOSE}|(?:[A-Za-z-]+\s+)?[^'\",\s}}]+)", re.I)
# Cookie / Set-Cookie: every cookie of the header, to the end of the line
_COOKIE_TEXT = re.compile(rf"(\b(?:set-)?cookie(?:\\*+['\"])?\s*[=:]\s*)({_QUOTED}|[^\r\n]+)", re.I)
# a whole PEM block.  A truncated one (a cut log tail): the rest of the BEGIN line, and the lines under it that are
# nothing but base64; the first line that is not ends it before its first character (a log line under a BEGIN line
# that never got its END marker is not body, and shows the same whatever else the window holds)
_PEM = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(?:[A-Za-z0-9+/=\s\\]*?-----END \1-----"
                  r"|[A-Za-z0-9+/=\\ \t]*(?:\r?\n[ \t]*[A-Za-z0-9+/=\\]+[ \t]*(?=\r?\n|\Z))*)")
# a line that is nothing but base64: the body of a key whose BEGIN line the caller never saw
# (a search that selected this line alone, the start of a tail window, a page boundary).  40
# characters is shorter than any line of a real key body and longer than the identifiers that
# appear alone on a log line.  The two long runs that are not key material are spelled out: a
# hex digest (a git sha is 40 characters), and a path or an MQTT topic, which are lower case
# and "/" where a key body of this length is, with certainty, mixed case.
_KEY_BODY_LINE = re.compile(r"(?![0-9a-fA-F]+\Z)(?![a-z0-9/]+\Z)[A-Za-z0-9+/]{40,}={0,2}")
_PEM_BODY_LINE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
# inside a block that is known to be open, the body can carry the logger's prefix (an integration
# that logs a key one line per record); a run that long is not a topic or a path
_PEM_BODY_TAIL = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}\Z")
# case-insensitive: a logger that lower-cases its headers writes "authorization: bearer ..."
_BEARER = re.compile(r"\b(Bearer|Basic)\s+([A-Za-z0-9._~+/=-]{8,})", re.I)
# user:password@host: the password may hold "/" and "@" (urlsplit cuts the authority at the first "/"), so it
# runs to the first "@" that a host-like part follows.  The first one, not the last: after it comes the path,
# and an "@" in a path is not the end of a credential ("http://u:p@host/users/@me").  That is also why
# "rtsp://admin:p@ss/w0rd@192.168.1.5" keeps "@ss/w0rd" - the two are the same text to this rule, and a run
# reaching for the last "@" masks the path of the first.  Nothing here may grow faster than the line: log
# content is device-influenced (a compact JSON body on one line), and the cost is paid on an executor thread
# holding the diagnostics lock.  Every part is therefore bounded - the scheme (unbounded, it backtracked from
# every letter of "a.a.a...", 2.3 s on 40 kB and 9.1 s on 80 kB), the credential, the host of the lookahead -
# and the caller skips the rule outright on a text with no "@" in it.  An "&" and a backslash end the host as
# a "/" does, so a line carrying two credentialed URLs (?a=url&b=url, a URL inside a JSON string) has both
# masked instead of neither
_URL_CRED_MAX = 256
# past the bound the credential is masked to the end of its run instead of being left whole: a 300-character
# JWT in "https://oauth2:<jwt>@gitlab..." found no "@" within the bound and came out printed in full.  Two
# runs, both possessive, so a failed attempt is one scan and no retries.  The first has no length limit and
# excludes "/", so it stops within the gap to the next "//" on the line and stays linear however many URLs
# that line holds; a "/" is not legal in a userinfo anyway (RFC 3986), so that is every credential a URL is
# meant to carry, at any length.  The second admits "/" as well, for the ones urlsplit tolerates, and pays
# for it with a bound - nothing stops its scan before the end of the line, and the scan would then be run
# again from every "scheme://x:" after it
_URL_CRED_LONG = 1024
_URL_CRED_RUN = r"[^\s\"'<>,;&]"  # the tail stops where one URL on a line ends and the next begins
_URL_CRED = re.compile(rf"((?<![a-z0-9+.-])[a-z][a-z0-9+.-]{{0,31}}://[^/\s:@]*:)"
                       rf"(?:\S{{1,{_URL_CRED_MAX}}}?(?P<at>@)"
                       rf"(?=[A-Za-z0-9._~%\[\]:-]{{0,255}}(?:[/?#\s\"'<>,;)&\\]|$))"
                       rf"|[^\s@/\"'<>,;&]{{{_URL_CRED_MAX},}}+@{_URL_CRED_RUN}*+"
                       rf"|[^\s@\"'<>,;&]{{{_URL_CRED_MAX},{_URL_CRED_LONG}}}+@{_URL_CRED_RUN}*+)", re.I)
_GH_TOKEN = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
LOG_FILE_TAIL = 500
DIAG_CACHE_S = 10  # a link any page can hit: one build at a time, repeats within this window get the same zip


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        # any non-empty value under a secret key, whatever its type (a numeric pin, a list of tokens, an auth block)
        # (a block whose own keys name its secrets, e.g. auth: {username, password}, is masked field by field)
        return {k: ("***" if _SECRET_KEY.search(str(k)) and v not in (None, "", [], {}) and not isinstance(v, bool)
                    and not (isinstance(v, dict) and any(_SECRET_KEY.search(str(k2)) for k2 in v)) else scrub(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _scrub_one_line_rules(_PEM.sub(lambda m: f"-----BEGIN {m.group(1)}-----***-----END {m.group(1)}-----", value))
    return value


def _mask_value(match: re.Match[str]) -> str:
    """The name, and the value as ``***`` inside the quotes it opened with.

    A wrapper the value came in (``b'x'``, ``SecretStr('x')``) is kept in
    front of the masked quotes, and the bracket it opened is put back after
    them, so the line keeps its shape and only the secret goes; a rule without
    the ``pre`` group has no wrapper to keep."""
    groups = match.groupdict()
    pre, close = groups.get("pre") or "", groups.get("close") or ""
    value = match.group(2)[len(pre):]
    if groups.get("esc_run"):
        quote = match.group("esc_run") + match.group("esc_quote")
    elif value[:1] in ('"', "'"):
        quote = value[0]
    else:
        return match.group(1) + "***"
    return match.group(1) + pre + quote + "***" + quote + close


def _scrub_one_line_rules(value: str) -> str:
    """Every rule whose match stays within one line; the PEM block is the one
    that spans lines and is masked by the caller."""
    # the log search text and credentials in a URL: process.log no longer receives them (logbuffer masks them
    # before a record is written), but lines written by an older version still hold them
    value = logbuffer.mask_query_secrets(value)
    value = _COOKIE_TEXT.sub(_mask_value, value)
    if _SECRET_TEXT_HINT.search(value):
        value = _SECRET_TEXT.sub(_mask_value, value)
    value = _AUTH_TEXT.sub(_mask_value, value)
    # a token, not "Basic information": anything but a plain word (base64 without padding is often letters only)
    value = _BEARER.sub(lambda m: m.group(0) if re.fullmatch(r"[A-Z]?[a-z]+", m.group(2)) else f"{m.group(1)} ***", value)
    if "@" in value:  # no "@" in the text, no credential run to find: the whole rule is skipped
        # the branch that found the closing "@" keeps it, so the host stays readable; the one that ran past the
        # bound masked the credential to the end of its run and has no "@" to put back.  A lambda, not a
        # function: tests/test_r9_web.py reads the names this function uses to check the search prefilter
        value = _URL_CRED.sub(lambda m: m.group(1) + ("***@" if m.group("at") else "***"), value)
    return _GH_TOKEN.sub("***", value)


def _mask_pem_in_place(match: re.Match[str]) -> str:
    """A PEM block masked without changing how many lines it occupies: the
    marker on the first line, ``***`` for every further line it covered
    (below the BEGIN line a match covers whole lines, or ends on an END
    marker)."""
    return f"-----BEGIN {match.group(1)}-----***-----END {match.group(1)}-----" + "\n***" * match.group(0).count("\n")


def mask_key_material_lines(lines: list[str], in_block: bool = False) -> tuple[list[str], bool]:
    """Key material masked line by line, without needing the whole block.

    The PEM rule needs the BEGIN marker and the body in one text, so it only
    works on a text nobody has cut: filtering first and scrubbing the
    survivors, or scrubbing a window that starts below the BEGIN line, hands
    back the body of a key the rule can no longer recognise.  Two rules that
    do not need the marker above the body:

    * read from the end, the base64 run above an ``-----END ...-----`` marker
      is that block's body whether its BEGIN line is in this batch or not.
      ``in_block`` is that state, carried in and out, so a caller reading a
      file backwards block by block passes it from one batch to the older one;
    * a line that is nothing but base64 and too long to be an identifier is
      masked on its own, which is what closes a search, a page or a tail whose
      window holds neither marker.

    ``lines`` come in and go out in file order.  Masking only ever replaces a
    line with ``***``, so it can drop a caller's match but never create one,
    and a caller may mask first and search afterwards.  Every line a tail
    reads passes through here, so the tests a line usually fails come first:
    a substring search for the marker, then a length and a space (a log line
    has a space in it long before it has forty base64 characters)."""
    out = list(lines)
    for i in range(len(out) - 1, -1, -1):
        line = out[i]
        if "-----" in line:
            # the BEGIN line of the block being masked, or its END marker (or both, inline)
            in_block = "-----END " in line and "-----BEGIN " not in line
            continue
        if in_block:
            text = line.strip()
            if not text:
                continue  # a blank line neither ends the block nor needs masking
            if _PEM_BODY_LINE.fullmatch(text):
                out[i] = "***"
                continue
            if (m := _PEM_BODY_TAIL.search(text)) is not None:
                out[i] = line[:line.index(text)] + text[:m.start()] + "***"
                continue
            in_block = False  # not body after all: the log line above a stray END marker
        if len(line) >= 40 and " " not in line and _KEY_BODY_LINE.fullmatch(line.strip()):
            out[i] = "***"
    return out, in_block


def scrub_lines(texts: list[str]) -> list[str]:
    """scrub() for callers that hold the text split up - a file's lines, a
    buffer's records - and show it that way.

    A PEM block spans lines, so scrubbing each piece on its own can never match
    it: the BEGIN line comes back masked while the key body on the lines after
    it is printed verbatim, which reads as masked and is not.  The pieces are
    scrubbed as one text and come back with the newlines they went in with, so
    the caller keeps one element per element and its line numbering.

    The pieces are often not the whole log either - a search kept some records,
    a page ended, a tail window began below the BEGIN line - so the line rules
    (mask_key_material_lines) run as well, and recognise key material that has
    no marker left above it."""
    masked, _ = mask_key_material_lines(_PEM.sub(_mask_pem_in_place, "\n".join(texts)).split("\n"))
    lines = [_scrub_one_line_rules(line) for line in masked]
    out, at = [], 0
    for text in texts:
        count = text.count("\n") + 1
        out.append("\n".join(lines[at:at + count]))
        at += count
    return out


def scrub_text(text: str) -> str:
    """scrub() for a text that is a log: the line rules apply to it as well, so
    a window that cut a key's BEGIN marker off does not print the body."""
    return "\n".join(scrub_lines(text.split("\n")))


def log_records_text(records: list[dict[str, Any]]) -> str:
    """The zip's log.txt: the newest records the handler holds, one line each.

    The messages are scrubbed as lines and the prefix is put on afterwards,
    the way the Logs page does it: this window is the newest 1000 records, so
    it can start below the BEGIN line of a key the integration logged, and a
    line that is key material is only recognisable as key material while it is
    still the whole line."""
    masked = scrub_lines([str(r.get("message") or "") for r in records])
    return "\n".join(f"{r.get('ts', '')} {r.get('level', '')} [{r.get('logger')}] {text}"
                     for r, text in zip(records, masked))


def _dump(obj: Any) -> str:
    return json.dumps(scrub(obj), indent=1, default=str, sort_keys=True)


class DiagnosticsView(ManagerView):
    url = "/api/diagnostics"

    def __init__(self, hass: HomeAssistant, installer: Installer, publisher, updater) -> None:
        self.hass = hass
        self.installer = installer
        self.publisher = publisher
        self.updater = updater
        self._lock = asyncio.Lock()
        self._cache: tuple[float, bytes] | None = None

    async def get(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Requested-With") != "fetch":
            # a heavy build that packs logs and statuses: not something a link on any web page may trigger
            return self.json_message("X-Requested-With: fetch required", status_code=400)
        if self._cache and time.monotonic() - self._cache[0] < DIAG_CACHE_S:
            body = self._cache[1]
        elif self._lock.locked():
            return self.json_message("a diagnostics zip is being built: try again in a moment", status_code=429)
        else:
            async with self._lock:
                body = await self._build()
                self._cache = (time.monotonic(), body)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return web.Response(body=body, content_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="hri-diagnostics-{stamp}.zip"'})

    async def _build(self) -> bytes:
        inst = self.installer
        files: dict[str, str] = {}
        status = await inst.status()
        files["manager-status.json"] = _dump(status)
        files["ha-status.json"] = _dump(await self.updater.status())
        files["mqtt-status.json"] = _dump(self.publisher.status())
        files["mqtt-config.json"] = _dump(self.publisher.public_config())
        files["settings.json"] = _dump(inst.settings.public())
        files["health.json"] = _dump(self.publisher.build_health())
        domain = inst.running
        if domain:
            files["manifest.json"] = _dump(inst.installed_manifest(domain) or {})
        files["config-entries.json"] = _dump([
            {"domain": e.domain, "title": e.title, "state": e.state.value, "version": e.version, "minor_version": e.minor_version,
             "disabled_by": e.disabled_by.value if e.disabled_by else None, "source": e.source, "options_keys": sorted(e.options),
             "data_keys": sorted(e.data)} for e in self.hass.config_entries.async_entries()])
        files["packages.txt"] = await self.hass.async_add_executor_job(self._packages)
        if events.EVENTS is not None:
            files["events.json"] = _dump(await self.hass.async_add_executor_job(events.EVENTS.recent, 300))
        files["notifications.json"] = _dump(notifications._rows(self.hass))  # noqa: SLF001
        files["memory.json"] = _dump(await memory_snapshot(self.hass))
        handler = logbuffer.find()
        if handler is not None:
            records, _ = await self.hass.async_add_executor_job(lambda: handler.query(limit=1000))
            files["log.txt"] = log_records_text(records)
        files["log_file.txt"] = await self.hass.async_add_executor_job(self._log_file_tail, _entry_paths(self.hass, self.installer.running))
        files["README.txt"] = ("hass-remote-integration diagnostics, generated " + time.strftime("%Y-%m-%dT%H:%M:%S%z")
                               + "\nSecrets scrubbed. The raw credential-bearing files (settings.json, mqtt.json on the volume) are excluded;"
                               " sanitized public views of them are included (settings.json, mqtt-config.json).\n")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, text in files.items():
                zf.writestr(name, text)
        return buf.getvalue()

    @staticmethod
    def _packages() -> str:
        import importlib.metadata as md

        rows = sorted({f"{d.metadata['Name']}=={d.version}" for d in md.distributions() if d.metadata and d.metadata.get("Name")}, key=str.lower)
        return "\n".join(rows)

    def _log_file_tail(self, entry_paths: list[str]) -> str:
        cfg = self.hass.config.config_dir
        found = _log_files(cfg, self.installer, entry_paths)
        if not found:
            return "(the running integration writes no log file)"
        name, path = found[0]["name"], found[0]["path"]  # the name as the Log files page gives it, relative to the resolved dir
        try:
            # the same opener the tail and the download use: a listed name that became a symlink
            # (or grew a second hard link) since the listing is refused, not read into the zip
            with open_log_file(path) as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 200_000))
                lines = fh.read().decode("utf-8", errors="replace").splitlines()
            # the integration's own log can carry passwords and tokens like any other log, and
            # this window (the last 200 kB, then the last LOG_FILE_TAIL lines) can start inside a key
            return scrub_text(f"# {name}, last {min(LOG_FILE_TAIL, len(lines))} lines\n"
                              + "\n".join(lines[-LOG_FILE_TAIL:]))
        except OSError as err:
            return f"(log file unreadable: {err})"
