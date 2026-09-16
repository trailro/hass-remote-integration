"""GET /api/diagnostics: a zip for bug reports (upstream or here): versions,
manifest, requirement versions, patches, health, MQTT/HA/manager status,
a memory snapshot, the last log records and the tail of the integration's newest log file.  Secrets are
scrubbed (values of keys that look like passwords/tokens, the GitHub token,
the MQTT password); settings.json and mqtt.json are never included."""

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
from .logfiles_page import _entry_paths, _log_files
from .memdiag import snapshot as memory_snapshot

# names ending in "key" that are known not to be secrets (everything else ending in "key" is masked)
_PLAIN_KEYS = r"(?:translation|sort|primary)_key"
_SECRET_KEY = re.compile(
    r"(password|passwd|passphrase|token|secret|credential|bearer|cookie|psk|hmac|passkey|bindkey|authorization|webhook_id|cloudhook_url|pin_code|signature"
    r"|(api|access|private|local|encryption|device|client|master|app|user|shared|signing|session|auth|link|network|aes|ssl)[_-]?key"
    rf"|^(?!{_PLAIN_KEYS}$).*key$"  # any *key: Z-Wave (lr_)s2_*_key, security_key, api-key, ...
    r"|(^|[_-])(irk|ltk|csrk|pwd|pw|sig|session_?id)$|(^|[_-])otp([_-]|$)"  # BLE bonding keys, one-time codes
    r"|^(pin|auth|pass)$|[_-](pin|pass)$)", re.I)
_SECRET_TEXT = re.compile(
    r"((?:password|passwd|passphrase|token|secret|credential|psk|hmac|passkey|bindkey|webhook_id|cloudhook_url|pin_code|signature|\bpin|\bcode|\botp"
    rf"|\bpwd|\w_pw\b|\bsession_?id|\b(?:irk|ltk|csrk|sig)\b|\b(?!{_PLAIN_KEYS}\b)\w*key"
    r"|(?:api|access|private|local|encryption|device|client|master|app|shared|signing|session|auth|link|network|aes|ssl)[_-]?key)"
    r"['\"]?\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^'\",\s}]+)", re.I)
# the whole value of an Authorization header, scheme included (Digest, a custom scheme, a bare token)
_AUTH_TEXT = re.compile(r"(authorization['\"]?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|(?:[A-Za-z-]+\s+)?[^'\",\s}]+)", re.I)
# Cookie / Set-Cookie: every cookie of the header, to the end of the line
_COOKIE_TEXT = re.compile(r"(\b(?:set-)?cookie['\"]?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\r\n]+)", re.I)
# a whole PEM block; a truncated one (a cut log tail) up to the first character that cannot be base64
_PEM = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----[\s\S]*?(?:-----END \1-----|(?=[^A-Za-z0-9+/=\s\\])|\Z)")
_BEARER = re.compile(r"\b(Bearer|Basic)\s+([A-Za-z0-9._~+/=-]{8,})")
# user:password@host: the password may hold "/" or "@" (urlsplit cuts the authority at the first "/"), so it
# runs to the "@" that a host-like part follows
_URL_CRED = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]*:)\S+?@(?=[A-Za-z0-9._~%\[\]:-]*(?:[/?#\s\"'<>,;)]|$))", re.I)
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


def _scrub_one_line_rules(value: str) -> str:
    """Every rule whose match stays within one line; the PEM block is the one
    that spans lines and is masked by the caller."""
    value = _COOKIE_TEXT.sub(lambda m: m.group(1) + (m.group(2)[0] + "***" + m.group(2)[0] if m.group(2)[:1] in ('"', "'") else "***"), value)
    value = _SECRET_TEXT.sub(lambda m: m.group(1) + (m.group(2)[0] + "***" + m.group(2)[0] if m.group(2)[:1] in ('"', "'") else "***"), value)
    value = _AUTH_TEXT.sub(lambda m: m.group(1) + (m.group(2)[0] + "***" + m.group(2)[0] if m.group(2)[:1] in ('"', "'") else "***"), value)
    # a token, not "Basic information": anything but a plain word (base64 without padding is often letters only)
    value = _BEARER.sub(lambda m: m.group(0) if re.fullmatch(r"[A-Z]?[a-z]+", m.group(2)) else f"{m.group(1)} ***", value)
    return _GH_TOKEN.sub("***", _URL_CRED.sub(r"\1***@", value))


def _mask_pem_in_place(match: re.Match[str]) -> str:
    """A PEM block masked without changing how many lines it occupies: the
    marker on the first line, ``***`` for every further line it covered."""
    return f"-----BEGIN {match.group(1)}-----***-----END {match.group(1)}-----" + "\n***" * match.group(0).count("\n")


def scrub_lines(texts: list[str]) -> list[str]:
    """scrub() for callers that hold the text split up - a file's lines, a
    buffer's records - and show it that way.

    A PEM block spans lines, so scrubbing each piece on its own can never match
    it: the BEGIN line comes back masked while the key body on the lines after
    it is printed verbatim, which reads as masked and is not.  The pieces are
    scrubbed as one text and come back with the newlines they went in with, so
    the caller keeps one element per element and its line numbering."""
    masked = _PEM.sub(_mask_pem_in_place, "\n".join(texts)).split("\n")
    lines = [_scrub_one_line_rules(line) for line in masked]
    out, at = [], 0
    for text in texts:
        count = text.count("\n") + 1
        out.append("\n".join(lines[at:at + count]))
        at += count
    return out


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
            files["log.txt"] = scrub("\n".join(
                f"{r.get('ts', '')} {r.get('level', '')} [{r.get('logger')}] {r.get('message')}"
                for r in records))
        files["log_file.txt"] = await self.hass.async_add_executor_job(self._log_file_tail, _entry_paths(self.hass, self.installer.running))
        files["README.txt"] = ("hass-remote-integration diagnostics, generated " + time.strftime("%Y-%m-%dT%H:%M:%S%z")
                               + "\nSecrets scrubbed; settings.json/mqtt.json contents not included.\n")
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
        path = found[0]["path"]
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 200_000))
                lines = fh.read().decode("utf-8", errors="replace").splitlines()
            # the integration's own log can carry passwords and tokens like any other log
            return scrub(f"# {os.path.relpath(path, cfg)}, last {min(LOG_FILE_TAIL, len(lines))} lines\n" + "\n".join(lines[-LOG_FILE_TAIL:]))
        except OSError as err:
            return f"(log file unreadable: {err})"
