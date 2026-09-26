"""Optional password for the web UI and the API.

``HRI_PASSWORD`` (or ``HRI_PASSWORD_FILE``, e.g. a Docker secret; it wins)
set and not empty: every page and API call needs a session cookie from
``/login``, or the password as ``Authorization: Bearer <password>`` for
scripts.  Unset or empty: no login, as before (except the app on the host
network, whose port only ingress may use then: LAN_REFUSED).  A ``HRI_PASSWORD_FILE``
that cannot be read, or that is there but empty, and a ``HRI_PASSWORD`` of
only whitespace, are a password that was meant to be set: the UI stays
closed with a password nobody knows, and the login page says why.

The session cookie carries its expiry and an HMAC over it and a tag of the
password, keyed with a random key kept on the volume: changing the password
logs every browser out, and the key never leaves the container.  Logging
out ends every session (a stateless cookie cannot be revoked alone): a
timestamp on the volume invalidates every cookie issued before it.  Failed
attempts are slowed down; after MAX_FAILURES within FAILURE_WINDOW_S from one
address that address is refused until the window passes, and after
GLOBAL_MAX_FAILURES within GLOBAL_WINDOW_S from all addresses together (many
addresses, e.g. an IPv6 range) every password attempt is refused until the
count drops.  The cookie name carries the port (as the app on its own network,
where the port inside is always the same, the container's host name): browsers send cookies to every
port of a host, so two instances on one host would otherwise share one name.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import socket
import time
from typing import Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.core import HomeAssistant
from jsonio import fsync_dir

from . import events
from .http_util import ManagerView, with_body
from .ingress import is_ingress
from .ui import load_template

_LOGGER = logging.getLogger(__name__)

LEGACY_COOKIE = "hri_session"  # the name before it carried the port: a valid one is moved to COOKIE on its next request


def _cookie_name() -> str:
    """hri_session_<port>; as the app the port inside is 8087 for every app, so two apps on one host would share it:
    there the container's host name, which the Supervisor sets per app, reduced to the characters a cookie name takes.
    Not on the host network: the host's own name there, and a port the Supervisor gave this app alone."""
    if os.environ.get("HRI_APP") and os.environ.get("HRI_HOST_NETWORK") != "1":
        host = re.sub(r"[^a-z0-9-]", "_", socket.gethostname().strip().lower().rstrip("."))
        if host:
            return f"hri_session_{host}"
    return f"hri_session_{os.environ.get('HRI_PORT', '8087').strip() or '8087'}"


COOKIE = _cookie_name()
SESSION_S = 30 * 86400
MAX_FAILURES = 5
FAILURE_WINDOW_S = 900
GLOBAL_MAX_FAILURES = 30
GLOBAL_WINDOW_S = 300
MAX_KEYS = 1000
OPEN_PATHS = frozenset({"/login", "/api/login", "/static/hri.css", "/static/login.js"})
DATA_KEY = "integration_manager_auth"
# the app on the host network (HRI_HOST_NETWORK, entrypoint.apply_app_info) shares the host's interfaces, so its port is
# on the LAN: without a password nothing but ingress is served there (the entrypoint's status page says the same)
LAN_REFUSED = ("Set the app's password to use hass-remote-integration on its port: the app runs on the host network, "
               "so the port is open to your network. The HRI sidebar panel works without it.")

LOGIN_HTML = load_template("login")


def _configured_password() -> tuple[str, str]:
    """Blocking: (password, why it cannot be used) from HRI_PASSWORD_FILE or
    HRI_PASSWORD; ('', '') = no login.  A reason means a password was meant to
    be set but did not arrive: a random one is returned, so the admin surface
    stays closed and the reason is what the login page shows."""
    path = os.environ.get("HRI_PASSWORD_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                password = fh.read().strip()
        except OSError as err:
            # fail closed: a password was meant to be set, so the UI must not open without one
            return secrets.token_urlsafe(32), f"HRI_PASSWORD_FILE {path} is not readable ({err})"
        except ValueError:  # the error names the bytes of the password: not repeated
            return secrets.token_urlsafe(32), f"HRI_PASSWORD_FILE {path} is not UTF-8 text"
        if not password:
            # a Docker secret declared but never populated, or a file truncated by a full disk, reads
            # as "": the same mistake as an unreadable one, and must not open the UI either
            return secrets.token_urlsafe(32), f"HRI_PASSWORD_FILE {path} is empty"
        return password, ""
    # a CR or LF at either end (an .env file with Windows line ends) is dropped: neither can be typed into the login
    # form (a password input strips them) nor sent in a header, so no usable password loses anything.  Spaces are
    # kept, unlike in the file: they can be typed, and a password may end with one on purpose
    password = os.environ.get("HRI_PASSWORD", "").strip("\r\n")
    if password and not password.strip():
        # only spaces, tabs and the like: a password was meant to be set (a template that rendered blank) and
        # did not arrive, like an empty HRI_PASSWORD_FILE; taken as it is, it would be guessed in a few tries
        return secrets.token_urlsafe(32), "HRI_PASSWORD is set but holds only whitespace"
    try:
        password.encode()
    except UnicodeEncodeError:  # bytes that are not UTF-8 arrive as lone surrogates, which no login can send
        return secrets.token_urlsafe(32), "HRI_PASSWORD holds bytes that are not UTF-8"
    return password, ""


def _load_key(path: str) -> tuple[bytes, OSError | None]:
    """Blocking: the session signing key, created on first use (mode 600), and
    the error when a new one could not be written.  The key only signs session
    cookies, so one that is not on the volume (a full or read-only volume on the
    first boot, or after a restore: backups leave it out) is still a random key
    that works: the password is checked all the same, and only the sessions end
    at the next restart, which tries the write again.  Refusing to start instead
    would take the login page, and with it the UI that frees the disk."""
    try:
        with open(path, "rb") as fh:
            key = fh.read()
        if len(key) >= 32:
            return key, None
    except OSError:
        pass
    key = secrets.token_bytes(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        os.replace(path + ".tmp", path)
    except OSError as err:
        try:
            os.unlink(path + ".tmp")  # a partial write: the space it holds is what the volume is short of
        except OSError:
            pass
        return key, err
    return key, None


class Auth:
    def __init__(self, password: str, key: bytes = b"", revoked_path: str | None = None, unusable: str = "") -> None:
        self.enabled = bool(password)
        # set when the configured password never arrived: the password here is a random one nobody
        # knows, and this is the text the login page shows instead of "wrong password"
        self.unusable = unusable
        self.revoked_path = revoked_path
        # session generation, raised by every logout and signed into each cookie: only cookies of the
        # current generation are valid.  Stored as a number that only grows and is never below the time
        # of the logout, so an older image reading the same file still treats it as "revoked before".
        self.generation = 0
        self._digest = hashlib.sha256(password.encode()).digest()
        self._tag = hashlib.sha256(b"session:" + password.encode()).hexdigest()[:16]
        self._key = key
        self._failures: dict[str, list[float]] = {}
        self._global: list[float] = []  # every failure, whatever the address
        self._global_locked = False

    # ----- credentials -------------------------------------------------------

    def check_password(self, password: str) -> bool:
        try:
            candidate = hashlib.sha256(password.encode()).digest()
        except UnicodeEncodeError:  # undecodable bytes in a header
            return False
        return hmac.compare_digest(candidate, self._digest)

    def new_session(self) -> str:
        return self._sign(int(time.time()) + SESSION_S, self.generation)

    def _sign(self, expires: int, generation: int) -> str:
        mac = hmac.new(self._key, f"{expires}.{generation}.{self._tag}".encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{generation}.{mac}"

    def valid_session(self, value: str) -> bool:
        try:
            expires_s, generation_s, _mac = value.split(".", 2)
            expires, generation = int(expires_s), int(generation_s)
        except (ValueError, AttributeError):
            return False  # a cookie of the format before generations: log in again
        try:
            return expires > time.time() and generation == self.generation and hmac.compare_digest(self._sign(expires, generation), value)
        except TypeError:  # a non-ASCII cookie
            return False

    def load_revoked(self) -> None:
        """Blocking.  No file: no logout yet.  A file that is there but cannot be read as a number (torn by a
        power loss before this wrote it with fsync, or damaged) held a logout: every session issued before now
        ends, rather than the ones that logout ended coming back."""
        try:
            with open(self.revoked_path or "", encoding="utf-8") as fh:
                self.generation = int(fh.read().strip())
        except FileNotFoundError:
            self.generation = 0
        except (OSError, ValueError) as err:
            self.generation = int(time.time())
            _LOGGER.warning("%s unreadable (%s): every session issued before now has ended", self.revoked_path, type(err).__name__)

    def revoke_all(self) -> None:
        """Blocking: every session issued until now ends, also one issued in
        the same second as an earlier logout.  Written to the volume so it
        holds across a restart; when the write fails (a full or read-only
        volume) the sessions still end now, and the OSError tells the caller:
        the file keeps the generation before, so at a restart the sessions
        issued before this logout are valid again and the ones issued after it
        end."""
        generation = max(int(time.time()) + 1, self.generation + 1)
        try:
            if self.revoked_path:
                with open(self.revoked_path + ".tmp", "w", encoding="utf-8") as fh:
                    fh.write(str(generation))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(self.revoked_path + ".tmp", self.revoked_path)
                fsync_dir(os.path.dirname(self.revoked_path))  # the rename: without it a power loss brings the old file back
        finally:
            self.generation = generation

    # ----- brute-force brake -------------------------------------------------

    @staticmethod
    def _left(attempts: list[float], now: float) -> int:
        recent = [t for t in attempts if now - t < FAILURE_WINDOW_S]
        return int(FAILURE_WINDOW_S - (now - recent[-MAX_FAILURES])) + 1 if len(recent) >= MAX_FAILURES else 0

    def _global_left(self, now: float) -> int:
        self._global = [t for t in self._global if now - t < GLOBAL_WINDOW_S][-GLOBAL_MAX_FAILURES:]
        if len(self._global) < GLOBAL_MAX_FAILURES:
            self._global_locked = False
            return 0
        return int(GLOBAL_WINDOW_S - (now - self._global[0])) + 1

    def _prune(self, now: float) -> None:
        """Forget unlocked addresses, oldest first, down to half the table; a locked one is never forgotten
        (an attacker filling the table from fresh addresses must not free the address it has locked)."""
        unlocked = sorted((c for c, ts in self._failures.items() if not self._left(ts, now)), key=lambda c: self._failures[c][-1])
        for c in unlocked[:max(0, len(self._failures) - MAX_KEYS // 2)]:
            self._failures.pop(c, None)

    def _table_full(self, client: str, now: float) -> bool:
        if client in self._failures or len(self._failures) < MAX_KEYS:
            return False
        self._prune(now)
        return len(self._failures) >= MAX_KEYS

    def locked_for(self, client: str) -> int:
        """Seconds this address is still refused (0 = may try)."""
        now = time.monotonic()
        recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
        if recent:
            self._failures[client] = recent
        else:
            self._failures.pop(client, None)
        own = self._left(recent, now)
        if not own and self._table_full(client, now):  # every slot holds a locked address: a new one counts as locked
            own = min(self._left(ts, now) for ts in self._failures.values())
        return max(own, self._global_left(now))

    def failed(self, client: str) -> None:
        now = time.monotonic()
        self._global.append(now)
        if not self._global_locked and self._global_left(now):
            self._global_locked = True
            _LOGGER.warning("%s failed logins from all addresses within %s s: every login refused for up to %s s",
                            GLOBAL_MAX_FAILURES, GLOBAL_WINDOW_S, GLOBAL_WINDOW_S)
            events.emit("auth", f"{GLOBAL_MAX_FAILURES} failed logins within {GLOBAL_WINDOW_S // 60} minutes from many addresses: "
                                f"every login refused for up to {GLOBAL_WINDOW_S // 60} minutes")
        if self._table_full(client, now):
            return  # counted in the global budget; locked_for already treats this address as locked
        attempts = self._failures.setdefault(client, [])
        attempts.append(now)
        if len(attempts) == MAX_FAILURES:
            events.emit("auth", f"{MAX_FAILURES} failed logins from {client}: refused for {FAILURE_WINDOW_S // 60} minutes", client=client)

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


def client_key(remote: str | None) -> str:
    """The lockout key of an address: an IPv4-mapped IPv6 address is its IPv4
    address, any other IPv6 address its /64 (one host holds a whole /64 and
    would otherwise get a fresh budget per address)."""
    try:
        ip = ipaddress.ip_address((remote or "").split("%", 1)[0])
    except ValueError:
        return remote or "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _client(request: web.Request) -> str:
    return client_key(request.remote)


def login_location(path_qs: str) -> str:
    """The login page relative to ``path_qs`` and ``next`` relative to the login page: the same answer reaches the
    page on the app's port and under an ingress prefix the request never saw."""
    path = path_qs.split("?", 1)[0]
    up = "../" * max(0, path.count("/") - 1)
    return f"{up}login?next=" + quote(path_qs.lstrip("/"), safe="")


_cookie_secure_warned = False


def _cookie_secure() -> bool:
    global _cookie_secure_warned  # one warning per process, not one per login
    value = os.environ.get("HRI_COOKIE_SECURE", "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value not in ("", "0", "false", "no", "off") and not _cookie_secure_warned:
        _cookie_secure_warned = True
        _LOGGER.warning("HRI_COOKIE_SECURE=%r is not 1/true/yes/on or 0/false/no/off: the session cookie is not marked "
                        "Secure", os.environ.get("HRI_COOKIE_SECURE", ""))
    return False


def _set_session_cookie(response: web.StreamResponse, request: web.Request, value: str, max_age: int) -> None:
    response.set_cookie(COOKIE, value, max_age=max_age, path="/", httponly=True, samesite="Strict",
                        secure=request.secure or _cookie_secure())  # behind a TLS proxy the request looks plain
    response.del_cookie(LEGACY_COOKIE, path="/")


async def async_setup_auth(hass: HomeAssistant) -> Auth:
    """Install the password check (after the host guard) when a password is set."""
    password, unusable = await hass.async_add_executor_job(_configured_password)
    if not password:
        auth = Auth("")
        hass.data[DATA_KEY] = auth
        if os.environ.get("HRI_APP") and os.environ.get("HRI_HOST_NETWORK") == "1":
            _install_lan_guard(hass)
        return auth
    if unusable:
        _LOGGER.error("%s: nobody can log in until it is fixed", unusable)
        events.emit("auth", f"{unusable}: nobody can log in until it is fixed")
    key, key_error = await hass.async_add_executor_job(_load_key, hass.config.path("integration_manager", "auth_key"))
    if key_error:
        _LOGGER.error("login key not written (%s): kept in memory only, so every session ends at the next restart", key_error)
        events.emit("auth", f"auth_key not written ({key_error}): logins work, but every session ends at the next restart")
    auth = Auth(password, key, hass.config.path("integration_manager", "auth_revoked"), unusable)
    await hass.async_add_executor_job(auth.load_revoked)
    hass.data[DATA_KEY] = auth

    @web.middleware
    async def password_guard(request: web.Request, handler):
        # through Home Assistant's ingress, Home Assistant's login (and ingress_users) is the gate
        if is_ingress(request) or request.path in OPEN_PATHS or auth.valid_session(request.cookies.get(COOKIE, "")):
            return await handler(request)
        legacy = request.cookies.get(LEGACY_COOKIE, "")
        if legacy and auth.valid_session(legacy):  # a session from before the port-specific name: moved over once
            left = int(legacy.split(".", 1)[0]) - int(time.time())  # valid now, so >= 1; after a long handler it was 0, a Max-Age that deletes the cookie
            response = await handler(request)
            if isinstance(response, web.StreamResponse) and not response.prepared:
                _set_session_cookie(response, request, legacy, left)
            return response
        header = request.headers.get("Authorization", "")
        if header[:7].lower() == "bearer ":  # the scheme is case-insensitive (RFC 7235)
            client = _client(request)
            left = auth.locked_for(client)
            if left:
                return web.json_response({"message": f"too many failed attempts: try again in {left // 60 + 1} min"}, status=429)
            if auth.check_password(header[7:]):
                auth.succeeded(client)
                return await handler(request)
            auth.failed(client)
            await asyncio.sleep(1)
        if request.path.startswith("/api/"):
            return web.json_response({"message": "login required (session cookie from /login, or Authorization: Bearer <password>)"}, status=401)
        raise web.HTTPFound(login_location(request.path_qs))

    try:
        hass.http.app.middlewares.append(password_guard)
    except Exception as err:  # noqa: BLE001 - a frozen app: refuse to run the UI open instead
        _LOGGER.error("password check not installed (%s): the UI refuses every request", err)
        raise
    _LOGGER.info("web UI password enabled")
    return auth


def _install_lan_guard(hass: HomeAssistant) -> None:
    """No password on the host network: every request that is not ingress gets 403, whatever its path (the image's
    healthcheck too, which takes any answer below 500 for alive).  Refuses to run without it, as the password check."""

    @web.middleware
    async def lan_guard(request: web.Request, handler):
        if is_ingress(request):
            return await handler(request)
        return web.Response(status=403, content_type="text/plain", text=LAN_REFUSED)

    try:
        hass.http.app.middlewares.append(lan_guard)
    except Exception as err:  # noqa: BLE001 - a frozen app: refuse to run the UI open instead
        _LOGGER.error("host network guard not installed (%s): the UI refuses every request", err)
        raise
    _LOGGER.warning("the app runs on the host network without a password: its port answers only the sidebar panel "
                    "(ingress); set the app's password to use the port")


class LoginPageView(ManagerView):
    url = "/login"

    def __init__(self, auth: Auth) -> None:
        self.auth = auth

    async def get(self, request: web.Request) -> web.Response:
        if not self.auth.enabled or is_ingress(request):
            raise web.HTTPFound("./")
        return web.Response(text=LOGIN_HTML, content_type="text/html")


class LoginView(ManagerView):
    url = "/api/login"

    def __init__(self, auth: Auth) -> None:
        self.auth = auth

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        if not self.auth.enabled:
            return self.json({"ok": True, "note": "no password is set"})
        if self.auth.unusable:  # no attempt can succeed: say why instead of counting it as a wrong password
            return self.json({"ok": False, "error": f"{self.auth.unusable}: no password can be accepted until it is fixed"}, status_code=503)
        client = _client(request)
        left = self.auth.locked_for(client)
        if left:
            return self.json({"ok": False, "error": f"too many failed attempts: try again in {left // 60 + 1} min"}, status_code=429)
        password = body.get("password")
        if not isinstance(password, str) or not self.auth.check_password(password):
            self.auth.failed(client)
            await asyncio.sleep(1)
            return self.json({"ok": False, "error": "wrong password"}, status_code=401)
        self.auth.succeeded(client)
        response = self.json({"ok": True})
        _set_session_cookie(response, request, self.auth.new_session(), SESSION_S)
        return response


class LogoutView(ManagerView):
    url = "/api/logout"

    def __init__(self, auth: Auth | None = None) -> None:
        self.auth = auth

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        response = self.json({"ok": True})
        if self.auth is not None and self.auth.enabled:
            try:
                await request.app["hass"].async_add_executor_job(self.auth.revoke_all)
            except OSError as err:
                # every session has ended all the same, this browser's cookie is deleted below: only a restart undoes it
                _LOGGER.error("logout not recorded on the volume (%s): every session ended, but the ones issued before "
                              "it are valid again after a restart", err)
                response = self.json({"ok": False, "error": f"logged out, but the logout could not be recorded on the volume ({err}): "
                                                            "every session has ended, and the ones issued before it are valid again "
                                                            "after the container restarts (log out again once the volume is fixed)"},
                                     status_code=500)
        response.del_cookie(COOKIE, path="/")
        response.del_cookie(LEGACY_COOKIE, path="/")
        return response
