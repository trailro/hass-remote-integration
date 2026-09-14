"""Optional password for the web UI and the API.

``HRI_PASSWORD`` (or ``HRI_PASSWORD_FILE``, e.g. a Docker secret; it wins)
set and not empty: every page and API call needs a session cookie from
``/login``, or the password as ``Authorization: Bearer <password>`` for
scripts.  Unset or empty: no login, as before.

The session cookie carries its expiry and an HMAC over it and a tag of the
password, keyed with a random key kept on the volume: changing the password
logs every browser out, and the key never leaves the container.  Logging
out ends every session (a stateless cookie cannot be revoked alone): a
timestamp on the volume invalidates every cookie issued before it.  Failed
attempts are slowed down; after MAX_FAILURES within FAILURE_WINDOW_S from one
address that address is refused until the window passes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.core import HomeAssistant

from . import events
from .http_util import ManagerView, with_body
from .ui import load_template

_LOGGER = logging.getLogger(__name__)

COOKIE = "hri_session"
SESSION_S = 30 * 86400
MAX_FAILURES = 5
FAILURE_WINDOW_S = 900
OPEN_PATHS = frozenset({"/login", "/api/login", "/static/hri.css"})
DATA_KEY = "integration_manager_auth"

LOGIN_HTML = load_template("login")


def _configured_password() -> str:
    """Blocking: the password from HRI_PASSWORD_FILE or HRI_PASSWORD ('' = no login)."""
    path = os.environ.get("HRI_PASSWORD_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as err:
            # fail closed: a password was meant to be set, so the UI must not open without one
            _LOGGER.error("HRI_PASSWORD_FILE %s is not readable (%s): nobody can log in until it is fixed", path, err)
            return secrets.token_urlsafe(32)
    password = os.environ.get("HRI_PASSWORD", "")
    return password if password.strip() else ""


def _load_key(path: str) -> bytes:
    """Blocking: the session signing key, created on first use (mode 600)."""
    try:
        with open(path, "rb") as fh:
            key = fh.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    os.replace(path + ".tmp", path)
    return key


class Auth:
    def __init__(self, password: str, key: bytes = b"", revoked_path: str | None = None) -> None:
        self.enabled = bool(password)
        self.revoked_path = revoked_path
        self.revoked_before = 0  # epoch: sessions issued before it are invalid (logout)
        self._digest = hashlib.sha256(password.encode()).digest()
        self._tag = hashlib.sha256(b"session:" + password.encode()).hexdigest()[:16]
        self._key = key
        self._failures: dict[str, list[float]] = {}

    # ----- credentials -------------------------------------------------------

    def check_password(self, password: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self._digest)

    def new_session(self) -> str:
        return self._sign(max(int(time.time()), self.revoked_before) + SESSION_S)

    def _sign(self, expires: int) -> str:
        mac = hmac.new(self._key, f"{expires}.{self._tag}".encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{mac}"

    def valid_session(self, value: str) -> bool:
        try:
            expires = int(value.split(".", 1)[0])
        except (ValueError, AttributeError):
            return False
        return expires > time.time() and expires - SESSION_S >= self.revoked_before and hmac.compare_digest(self._sign(expires), value)

    def load_revoked(self) -> None:
        """Blocking."""
        try:
            with open(self.revoked_path or "", encoding="utf-8") as fh:
                self.revoked_before = int(fh.read().strip() or 0)
        except (OSError, ValueError):
            self.revoked_before = 0

    def revoke_all(self) -> None:
        """Blocking: every session issued until now ends."""
        self.revoked_before = int(time.time()) + 1
        if self.revoked_path:
            with open(self.revoked_path + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(str(self.revoked_before))
            os.replace(self.revoked_path + ".tmp", self.revoked_path)

    # ----- brute-force brake -------------------------------------------------

    def locked_for(self, client: str) -> int:
        """Seconds this address is still refused (0 = may try)."""
        now = time.monotonic()
        recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
        if recent:
            self._failures[client] = recent
        else:
            self._failures.pop(client, None)
        return int(FAILURE_WINDOW_S - (now - recent[0])) + 1 if len(recent) >= MAX_FAILURES else 0

    def failed(self, client: str) -> None:
        attempts = self._failures.setdefault(client, [])
        attempts.append(time.monotonic())
        if len(attempts) == MAX_FAILURES:
            events.emit("auth", f"{MAX_FAILURES} failed logins from {client}: refused for {FAILURE_WINDOW_S // 60} minutes", client=client)
        if len(self._failures) > 1000:  # many addresses: forget the oldest
            for c in sorted(self._failures, key=lambda c: self._failures[c][-1])[:500]:
                self._failures.pop(c, None)

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


def _client(request: web.Request) -> str:
    return request.remote or "unknown"


async def async_setup_auth(hass: HomeAssistant) -> Auth:
    """Install the password check (after the host guard) when a password is set."""
    password = await hass.async_add_executor_job(_configured_password)
    if not password:
        auth = Auth("")
        hass.data[DATA_KEY] = auth
        return auth
    key = await hass.async_add_executor_job(_load_key, hass.config.path("integration_manager", "auth_key"))
    auth = Auth(password, key, hass.config.path("integration_manager", "auth_revoked"))
    await hass.async_add_executor_job(auth.load_revoked)
    hass.data[DATA_KEY] = auth

    @web.middleware
    async def password_guard(request: web.Request, handler):
        if request.path in OPEN_PATHS or auth.valid_session(request.cookies.get(COOKIE, "")):
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
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
        return web.HTTPFound("/login?next=" + quote(request.path_qs, safe=""))

    try:
        hass.http.app.middlewares.append(password_guard)
    except Exception as err:  # noqa: BLE001 - a frozen app: refuse to run the UI open instead
        _LOGGER.error("password check not installed (%s): the UI refuses every request", err)
        raise
    _LOGGER.info("web UI password enabled")
    return auth


class LoginPageView(ManagerView):
    url = "/login"

    def __init__(self, auth: Auth) -> None:
        self.auth = auth

    async def get(self, request: web.Request) -> web.Response:
        if not self.auth.enabled:
            return web.HTTPFound("/")
        return web.Response(text=LOGIN_HTML, content_type="text/html")


class LoginView(ManagerView):
    url = "/api/login"

    def __init__(self, auth: Auth) -> None:
        self.auth = auth

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        if not self.auth.enabled:
            return self.json({"ok": True, "note": "no password is set"})
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
        response.set_cookie(COOKIE, self.auth.new_session(), max_age=SESSION_S, path="/", httponly=True, samesite="Strict",
                            secure=request.secure)
        return response


class LogoutView(ManagerView):
    url = "/api/logout"

    def __init__(self, auth: Auth | None = None) -> None:
        self.auth = auth

    @with_body
    async def post(self, request: web.Request, body: dict[str, Any]) -> web.Response:
        if self.auth is not None and self.auth.enabled:
            await request.app["hass"].async_add_executor_job(self.auth.revoke_all)
        response = self.json({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response
