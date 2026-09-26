"""Container entrypoint: make sure the wanted Home Assistant version is
installed in a venv ON THE VOLUME, then hand over to run.py inside it.

Why: HA used to be baked into the image, which made "update HA" a rebuild.
Now the image is only Python + this bootstrap; HA lives in
``/config/venv-<version>`` and survives image rebuilds and container
recreation.  The manager UI changes the wanted version in
``/config/integration_manager/ha.json`` and restarts the process; this script
does the rest, keeps the previous venv for rollback, and falls back to the
last working venv if an install fails (the UI then shows the error).

Until Home Assistant is exec'd (an install takes a few minutes on first boot)
a tiny status page answers on the manager port so the browser is not left
with a connection error.
"""

from __future__ import annotations

import html
import http.server
import hashlib
import glob
import ipaddress
import socket
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import backupkit  # /app/backupkit.py: apply a restore scheduled from the UI
from jsonio import fsync_dir, ha_vkey, vkey, write_json

CONFIG_DIR = os.environ.get("HRI_CONFIG", "/config")


def _parse_port(raw: str) -> int | None:
    """None for a value that is not a TCP port: main() says so in the log instead of a traceback at import."""
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


PORT = _parse_port(os.environ.get("HRI_PORT", "8087"))
# The image sets it (Dockerfile ARG HA_VERSION, the one place the weekly canary moves): a literal fallback here
# drifted from it, and a fresh volume without network would have installed that stale version.  main() refuses
# to start without it instead.
DEFAULT_VERSION = os.environ.get("HA_VERSION_DEFAULT", "")
# The oldest version this image installs (ha_updater refuses anything older, with no force).  A
# DEFAULT_VERSION under it would put a version on a fresh volume that the UI then refuses to go back to,
# so the floor wins and says so.
MIN_VERSION = os.environ.get("HA_VERSION_MIN", "")
EXTRA_REQUIREMENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")  # installed next to homeassistant
MAX_BOOT_FAILURES = 3
PIP_IDLE_TIMEOUT_S = 15 * 60  # pip writes a line per package: nothing at all for this long is a hang, not a slow download
PIP_POLL_S = 5
STATUS_RETRY_AFTER_S = 5  # the install page refreshes itself this often; clients polling /api/ may do the same
STATE_DIR = os.path.join(CONFIG_DIR, "integration_manager")
HA_FILE = os.path.join(STATE_DIR, "ha.json")
LOG_FILE = os.path.join(STATE_DIR, "ha-install.log")
APT_LOG_FILE = os.path.join(STATE_DIR, "apt-install.log")  # what apt wrote at the last boot that ran it
APT_LISTS_DIR = "/var/lib/apt/lists"
REBUILD_FILE = os.path.join(STATE_DIR, "rebuild-pending.json")  # custom_components/integration_manager/ha_import.py
# Home Assistant OS runs this image as an app (app/config.yaml): the Supervisor writes the options set on the app's
# Configuration tab to this file and gives the container a SUPERVISOR_TOKEN.  apply_app_options() turns each option
# into the variable a plain Docker install sets, before anything reads it; the exec of run.py inherits them.
APP_OPTIONS_FILE = "/data/options.json"
# the token lets whoever holds it read and rewrite the app's options (the password among them) at http://supervisor:
# HRI needs it for nothing once they are read, so neither Home Assistant nor an integration inherits it
SUPERVISOR_TOKEN_VARS = ("SUPERVISOR_TOKEN", "HASSIO_TOKEN")
# set once the options are applied: what is left of "this is an app" after the token is gone (mqtt_publisher.py reads it)
APP_MARKER = "HRI_APP"
# option: (variable, None for a text or number, or (value for true, value for false) for a bool; None = unset)
APP_OPTIONS = {
    "password": ("HRI_PASSWORD", None),
    "apt_packages": ("HRI_APT_PACKAGES", None),
    "call_timeout": ("HRI_CALL_TIMEOUT", None),
    "ha_version_latest": ("HA_VERSION_LATEST", ("1", "0")),
    "debug": ("HRI_DEBUG", ("1", None)),  # unset rather than "0": the variable a Docker install reads
    "cookie_secure": ("HRI_COOKIE_SECURE", ("1", None)),
    "ingress_users": ("HRI_INGRESS_USERS", None),  # a list of Home Assistant user names: comma separated
}
# The Supervisor starts a stopped or crashed app again only with the app's Watchdog toggle on, which is off by default:
# HRI turns it on once per volume (the marker records that it did), so an operator who turns it off later is not
# overruled.  An app may change its own options with its own token (supervisor/api/middleware/security.py api_bypass,
# /addons/self/...), and the options handler only stores the flag (api/apps.py APIApps.options): the app is not restarted.
SUPERVISOR_OPTIONS_URL = "http://supervisor/addons/self/options"
APP_WATCHDOG_MARKER = os.path.join(STATE_DIR, "app-watchdog-enabled")
# The toggle as it is at boot, read while the token is still there (data.watchdog), for run.py: "1" on, "0" off,
# unset when unknown.  With it on, a restart HRI asks for ends the process and the Supervisor starts a fresh container,
# with Docker's start period for the HEALTHCHECK; otherwise run.py restarts in place.  The restarted entrypoint has no
# token and keeps the value it inherits.
SUPERVISOR_INFO_URL = "http://supervisor/addons/self/info"
APP_WATCHDOG_VAR = "HRI_APP_WATCHDOG"
# the Supervisor, the transport peer of every request Home Assistant's ingress proxies (as ingress.py SUPERVISOR_IP)
SUPERVISOR_IP = "172.30.32.2"
CONSTRAINTS_URL = "https://raw.githubusercontent.com/home-assistant/core/{version}/homeassistant/package_constraints.txt"
PYPI_URL = "https://pypi.org/pypi/homeassistant/json"


def _python_fits(spec: str | None) -> bool:
    """Minimal '>=3.13.2' style check (packaging is not importable from the
    image Python before a venv exists)."""
    if not spec:
        return True
    py = sys.version_info[:3]
    for part in spec.split(","):
        m = re.match(r"\s*(>=|>|<=|<|==|!=)\s*([\d.]+)", part)
        if not m:
            continue
        op, want = m.group(1), tuple(int(x) for x in re.findall(r"\d+", m.group(2)))  # unpadded: ==3.13 is a prefix
        have = py[:len(want)]
        ok = {">=": have >= want, ">": have > want, "<=": have <= want, "<": have < want, "==": have == want, "!=": have != want}[op]
        if not ok:
            return False
    return True


def fits_this_python(version: str) -> bool:
    """False only when PyPI says ``version`` does not support this image's Python
    (a base image bump); offline or unknown counts as fitting."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/homeassistant/{version}/json", timeout=20) as resp:
            spec = (json.load(resp).get("info") or {}).get("requires_python")
    except Exception:  # noqa: BLE001
        return True
    return _python_fits(spec)


def latest_stable() -> str | None:
    """Newest stable Home Assistant on PyPI that supports this image's
    Python and is not older than the image baseline; None when offline."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=20) as resp:
            data = json.load(resp)
    except Exception as err:  # noqa: BLE001
        log(f"PyPI not reachable ({err}); using the image default {DEFAULT_VERSION}")
        return None
    best = None
    for v, files in data.get("releases", {}).items():
        files = [f for f in files or [] if isinstance(f, dict) and not f.get("yanked")]  # a release yanked whole is not installable
        if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", v) or not files or vkey(v) < vkey(DEFAULT_VERSION):
            continue
        if not _python_fits(files[0].get("requires_python")):
            continue
        if best is None or vkey(v) > vkey(best):
            best = v
    return best

# "kind": what keeps Home Assistant from running - "install", or "restore_hold" (hold_after_failed_rollback)
_status = {"phase": "starting", "version": None, "started": time.time(), "kind": "install"}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [entrypoint] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # a full disk must not stop the boot: the UI is where space gets freed


# what ha_updater.set_desired writes: a version from ha.json goes into venv paths (removed before an install), pip's
# argv and the constraints URL, so anything else in it is not a version
VERSION_RE = re.compile(r"\d{4}\.\d{1,2}\.\d+(?:b\d+)?")
VERSION_KEYS = ("desired", "current", "previous", "fallback_from", "proven")


def valid_version(value) -> bool:
    return isinstance(value, str) and VERSION_RE.fullmatch(value) is not None


def _recovered_current() -> str | None:
    current = None
    link = os.path.join(CONFIG_DIR, "venv-current")
    if os.path.islink(link):
        current = os.path.basename(os.readlink(link))[5:]
    if not valid_version(current) or not venv_ok(current):
        current = next(iter(reversed(installed_versions())), None)
    return current


def load_state() -> dict:
    """Missing file = fresh volume.  Present but unparsable = torn write:
    never treat that as fresh (it would reinstall the image default and
    prune the venv that was running); the returned dict then carries
    ``_corrupt`` (never saved: save_state strips it).  A version field that is
    not a Home Assistant version is dropped (as absent); an invalid current is
    recovered from the volume the same way, pruning disabled."""
    try:
        with open(HA_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        current = _recovered_current()
        log(f"ha.json is unreadable; recovered current={current} from the volume, pruning disabled this boot")
        return {"current": current, "desired": current, "last_error": "ha.json was corrupt and has been rebuilt", "_corrupt": True}
    bad = [k for k in VERSION_KEYS if data.get(k) and not valid_version(data[k])]
    for key, fields in (("change", ("to",)), ("recovery", ("for", "from"))):
        if isinstance(data.get(key), dict) and any(data[key].get(f) and not valid_version(data[key][f]) for f in fields):
            bad.append(key)
    if not bad:
        return data
    for k in bad:
        data.pop(k, None)
    log(f"ha.json: {', '.join(bad)} not a Home Assistant version, ignored")
    if "current" in bad:
        data["current"] = _recovered_current()
        data["_corrupt"] = True
        log(f"ha.json: recovered current={data['current']} from the volume, pruning disabled this boot")
    return data


def _count(value) -> int:
    """boot_failures as written by hand or by an older version: anything that is not a number counts as 0.
    json.load reads 1e999 and Infinity as a float infinity, and int() refuses that with OverflowError, not
    ValueError: uncaught it killed the container in _prepare, before Home Assistant was even started."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        log(f"ha.json: boot_failures={value!r} is not a count; counting 0 failed boots")
        return 0


TMP_SWEEP_AGE_S = 600
_JSON_TMP = re.compile(r".+\.json\.[^.]+\.tmp")  # jsonio.write_json's mkstemp names
_YAML_TMP = re.compile(r"\.[a-z0-9_]+\.yaml\.[A-Za-z0-9_]+\.tmp")  # Installer.yaml_write's, in integration_manager/yaml/


def sweep_json_tmp_files() -> None:
    """A kill between jsonio.write_json's (or Installer.yaml_write's) mkstemp and its replace leaves the tmp file
    behind for good (nothing else ever matches its random name).  Only on the volume's top level,
    integration_manager/ and integration_manager/yaml/, only old ones."""
    now = time.time()
    for d, pattern in ((CONFIG_DIR, _JSON_TMP), (STATE_DIR, _JSON_TMP), (os.path.join(STATE_DIR, "yaml"), _YAML_TMP)):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            try:
                if pattern.fullmatch(name) and os.path.isfile(path) and not os.path.islink(path) and now - os.path.getmtime(path) > TMP_SWEEP_AGE_S:
                    os.remove(path)
                    log(f"removed leftover {os.path.relpath(path, CONFIG_DIR)}")
            except OSError:
                pass


def save_state(state: dict) -> bool:
    """Atomic: a torn ha.json would read as {} and silently reinstall the
    image default version (and prune the one that was running)."""
    try:
        write_json(HA_FILE, {k: v for k, v in state.items() if k not in ("_corrupt", "_config_for")})
        return True
    except OSError as err:
        log(f"ha.json not written ({err}): booting anyway")
        return False


def venv_dir(version: str) -> str:
    return os.path.join(CONFIG_DIR, f"venv-{version}")


def venv_ok(version: str) -> bool:
    """A venv is usable only for the Python of THIS image: bin/python is an
    unversioned symlink, so after a python:3.15 image bump an old venv
    would exec fine and then fail every import."""
    d = venv_dir(version)
    ha_pkg = os.path.join(d, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages", "homeassistant", "__init__.py")
    return os.path.isfile(os.path.join(d, ".ok")) and os.path.isfile(os.path.join(d, "bin", "python")) and os.path.isfile(ha_pkg)


def installed_versions() -> list[str]:
    out = []
    for name in os.listdir(CONFIG_DIR):
        if name.startswith("venv-") and name != "venv-current" and venv_ok(name[5:]):
            out.append(name[5:])
    return sorted(out, key=ha_vkey)  # a beta sorts before its release: the fallback never prefers it


SAFE_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")  # as hostguard.py


def _bare_host(host: str) -> str:
    """hostguard._bare: a Host value or an allowed_hosts entry without its port and the one trailing dot."""
    h = host.strip().lower()
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h[:-1] if h.endswith(".") else h


def status_host_ok(host: str) -> bool:
    """The manager's DNS-rebinding rule (hostguard._host_ok) for the page served while HA installs."""
    try:
        with open(os.path.join(STATE_DIR, "settings.json"), encoding="utf-8") as fh:
            extra_raw = str((json.load(fh) or {}).get("allowed_hosts") or "")
    except (OSError, ValueError, AttributeError):
        extra_raw = ""
    # the entries and the Host value go through one rule, hostguard's: a separate port rule for the entries
    # read `name:`, `a:b:8087` and `[name]:8087` differently from the manager
    extra = {_bare_host(x) for x in extra_raw.split(",") if x.strip()}
    h = _bare_host(host or "")
    if not h:
        return False
    # rstrip("."): gethostname() may come back fully qualified with the trailing dot, and hostguard
    # compares its own name without it - the container would be refused here and accepted there
    if h in ("localhost", socket.gethostname().lower().rstrip(".")) or h in extra:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return h.endswith(SAFE_HOST_SUFFIXES)


def install_status() -> dict:
    """The install status for an /api/ caller (a healthcheck, a script waiting for the manager API).  A monitor
    that reads "installing" must not be told that for the minutes a failed restore holds the boot."""
    held = _status.get("kind") == "restore_hold"
    error = ("Home Assistant is not started: a restore failed and could not be put back; the manager API is not up"
             if held else f"Home Assistant is still being installed or prepared ({_status.get('phase')}); the manager API is not up yet")
    return {**_status, "elapsed": int(time.time() - _status["started"]), "installing": not held, "restore_failed": held, "error": error}


def password_configured() -> bool:
    # as auth._configured_password: a password of spaces or tabs is one (the manager locks the UI and says why),
    # only line ends are none - an empty HRI_PASSWORD= line of an .env saved with Windows line ends
    return bool(os.environ.get("HRI_PASSWORD", "").strip("\r\n") or os.environ.get("HRI_PASSWORD_FILE", "").strip())


def _log_tail() -> str:
    try:
        with open(LOG_FILE, "rb") as fh:  # last 64 KB only, the file may be long
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            return "\n".join(fh.read().decode("utf-8", errors="replace").splitlines()[-40:])
    except OSError:
        return ""


ALIVE_PATH = "/api/alive"  # the Dockerfile's HEALTHCHECK asks it


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    # every request is one small GET answered at once (no long poll, no stream): a connection that sends nothing
    # would otherwise hold its thread in readline() for good, and anyone on the LAN could open them by the thousand
    timeout = 30

    def do_GET(self) -> None:  # noqa: N802
        # Home Assistant's ingress (as the app): the Supervisor proxies with the browser's Host of Home Assistant and
        # sets the user (ingress.py).  X-Forwarded-For is not read here, so it needs no stripping
        ingress = bool(os.environ.get(APP_MARKER)) and self.client_address[0] == SUPERVISOR_IP
        if ingress:
            users = {u.strip().casefold() for u in os.environ.get("HRI_INGRESS_USERS", "").split(",") if u.strip()}
            if users and self.headers.get("X-Remote-User-Name", "").casefold() not in users:
                self.send_error(403, "This Home Assistant user may not open hass-remote-integration (ingress_users)")
                return
        elif not status_host_ok(self.headers.get("Host", "")):
            self.send_error(403, "Host not allowed (DNS rebinding guard)")
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == ALIVE_PATH:
            # the image's HEALTHCHECK is liveness: an install in progress is alive, or a watchdog that acts on
            # "unhealthy" (the Supervisor's) restarts it halfway.  Once Home Assistant runs, the manager has no
            # view here: 404, or 401 with a password, both below the 500 the probe takes for dead
            self._send(200, "application/json", b'{"alive": true}')
            return
        # Nothing else here is the manager API: while Home Assistant installs, /api/status, /api/diag/health and
        # any monitor used to get this HTML page with a 200 and call the container healthy for the whole
        # install.  503 says what is true, and the page is served with it too - browsers render the body.
        # no login exists yet: with a password, the version, the phase (apt package names) and a failed restore are for
        # whoever can log in; a healthcheck, a waiting script or a browser needs only "not up yet"
        hide = password_configured() and not ingress
        if path.startswith("/api/"):
            status = install_status()
            if hide:
                status = {"installing": status["installing"], "error": "the manager API is not up yet (Home Assistant "
                          + ("is still being installed or prepared)" if status["installing"] else "is not started)")}
            self._send(503, "application/json", json.dumps(status).encode())
            return
        if _status.get("kind") != "install":
            tail = ""  # the log is of the last install, nothing to do with a restore that failed
        elif password_configured():
            tail = "(the install log is integration_manager/ha-install.log on the volume)"  # no login exists yet: show only the phase
        else:
            # without a password the manager shows the same log to anyone once it runs; until then this is the
            # only place pip's progress appears (pip writes into the file, not into the container log)
            tail = _log_tail()
        if hide:
            heading = ("Home Assistant is still being installed or prepared …" if _status.get("kind") == "install"
                       else "Home Assistant is not started")
            detail = "<p>the details are in the container log · this page refreshes itself</p>"
        else:
            heading = _status.get("title") or f"Installing Home Assistant {_status['version']} …"
            detail = (f"<p>phase: <b>{html.escape(str(_status['phase']))}</b> · {int(time.time() - _status['started'])} s so far"
                      " · this page refreshes itself</p>")
        log_block = f"<pre style='font:12px ui-monospace;color:#8b98a5;white-space:pre-wrap'>{html.escape(tail)}</pre>" if tail else ""
        body = (
            "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5>"
            f"<title>hass-remote-integration · {html.escape(heading)}</title>"
            "<body style='font:14px system-ui;background:#0f1418;color:#e6edf3;padding:24px'>"
            f"<h2>{html.escape(heading)}</h2>"
            f"{detail}"
            f"{log_block}"
        ).encode()
        self._send(503, "text/html; charset=utf-8", body)

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(STATUS_RETRY_AFTER_S))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence
        pass


def start_status_server() -> http.server.ThreadingHTTPServer | None:
    try:
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), _StatusHandler)
    except OSError as err:
        log(f"status page not started: {err}")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# the status server main() runs from its first slow step until right before the exec: answered on the manager port
# for the whole boot, not only while pip runs (PyPI lookups, the requirements install, a restore, venv pruning)
_boot_server: http.server.ThreadingHTTPServer | None = None


def stop_status_server(srv: http.server.ThreadingHTTPServer) -> None:
    """shutdown() only ends serve_forever: the listening socket stays bound until server_close(), and the next
    status page on the same port (the hold after a failed rollback) could not start."""
    srv.shutdown()
    srv.server_close()


def _written(out) -> int:
    try:
        return os.fstat(out.fileno()).st_size
    except OSError:
        return -1


def _run_pip(cmd: list[str], out, idle_timeout: float = PIP_IDLE_TIMEOUT_S, env: dict[str, str] | None = None) -> None:
    """subprocess.run(check=True), with pip in its own process group: a kill takes the whole group, also the
    build backends pip started (a kill of pip alone left those running).

    The budget is on silence, not on the whole run: the fixed wall clock it replaced killed an install that was
    still working (a small machine, a slow mirror, a big wheel) and then threw the venv away, so the retry
    started from zero and ran into the same wall.  pip writes a line per package into ``out``, so the file
    growing is progress; nothing written for ``idle_timeout`` is a hang and still ends the install.

    apt runs through this too (see _apt_install): it writes a line per package the same way."""
    with subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env=env) as proc:
        try:
            written, deadline = -1, time.monotonic() + idle_timeout
            poll = max(0.05, min(PIP_POLL_S, idle_timeout))  # never sleep past the deadline (a short budget in a test)
            while True:
                try:
                    rc = proc.wait(timeout=poll)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if (size := _written(out)) != written:
                    written, deadline = size, time.monotonic() + idle_timeout
                elif time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, idle_timeout)
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            raise
    if rc:
        raise subprocess.CalledProcessError(rc, cmd)


# Debian package names (policy 5.6.1: at least two characters, lowercase letters, digits, "+", "-", "."), with the
# optional :architecture.  What HRI_APT_PACKAGES holds goes into apt-get's argv, so anything else in it - an option,
# a URL, a path, a shell metacharacter - is not a package and is never passed on (nothing here sees a shell either).
# The name has to end in a letter, a digit or "+" (g++): a trailing "-" is apt's own operator, and "libturbojpeg0-"
# in the variable would have apt *remove* that package at every boot instead of installing anything.
APT_PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*[a-z0-9+](?::[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)?")


def apt_packages_wanted() -> tuple[list[str], list[str]]:
    """(packages, refused) from HRI_APT_PACKAGES: names separated by spaces or commas, duplicates dropped."""
    packages: list[str] = []
    refused: list[str] = []
    for token in re.split(r"[\s,]+", os.environ.get("HRI_APT_PACKAGES", "").strip()):
        if not token:
            continue
        if not APT_PACKAGE_RE.fullmatch(token):
            refused.append(token)
        elif token not in packages:
            packages.append(token)
    return packages, refused


def _dpkg_installed(name: str) -> bool:
    """One dpkg-query per package: a name that is not installed is an error exit, not a line, so asking for them
    together cannot say which of them is missing.  No dpkg at all (another base image, a test): let apt decide."""
    try:
        proc = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Status}", name],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.decode("utf-8", errors="replace").strip() == "installed"


def _clean_apt_lists() -> None:
    """The package lists apt-get update downloaded (tens of MB) are of no use after the install, as in the
    Dockerfile; the directory itself stays, apt-get needs it."""
    for name in os.listdir(APT_LISTS_DIR) if os.path.isdir(APT_LISTS_DIR) else []:
        path = os.path.join(APT_LISTS_DIR, name)
        try:
            shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)
        except OSError:
            pass


def _apt_install(packages: list[str]) -> None:
    """apt-get update + install into the container, no shell anywhere; raises like an install that failed.
    Its output goes to apt-install.log (one install per file, as ha-install.log), not into the container log."""
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}  # a package asking a question would hang the boot
    with open(APT_LOG_FILE, "w", encoding="utf-8") as fh:
        fh.write(f"# apt-get install {' '.join(packages)}, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.flush()
        # the same idle budget as pip: a slow mirror runs on, a run that writes nothing for 15 minutes is a hang
        _run_pip(["apt-get", "update"], fh, env=env)
        # Pattern-Only: a name that is no package ("python3.1.", "libc6.dev") is not read as a regex over every
        # package name, which installed hundreds of them; an exact name, with or without :arch, installs as before
        _run_pip(["apt-get", "-o", "APT::Cmd::Pattern-Only=true", "install", "-y", "--no-install-recommends", *packages], fh, env=env)
    _clean_apt_lists()


def ensure_apt_packages(state: dict) -> None:
    """Install what HRI_APT_PACKAGES asks for before Home Assistant starts: the system components pip cannot
    install (the ffmpeg binary, BlueZ), which the image does not carry for every user.  Nothing of this is
    fatal - a refused name, no network, an unknown package: it is logged, recorded in ha.json for the System
    page, and the boot goes on without them (the integration that needs them then fails, visibly)."""
    packages, refused = apt_packages_wanted()
    if not packages and not refused:
        state.pop("apt", None)  # the variable is gone: the record of an earlier boot no longer describes this one
        return
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "packages": packages, "refused": refused, "ok": True, "error": "", "note": ""}
    if refused:
        log(f"HRI_APT_PACKAGES: not a Debian package name, nothing installed for {', '.join(refused)}")
        record.update(ok=False, error=f"not a Debian package name: {', '.join(refused)}")
    if not packages:
        record["note"] = "nothing to install"
        state["apt"] = record
        return
    _phase(f"checking the system packages from HRI_APT_PACKAGES: {', '.join(packages)}")
    missing = [p for p in packages if not _dpkg_installed(p)]
    if not missing:
        # a restart must not download hundreds of MB again
        log(f"system packages already installed: {', '.join(packages)}")
        record["note"] = "already installed"
        state["apt"] = record
        return
    _phase(f"installing the system packages {', '.join(missing)} (apt-get, a few minutes)")
    log(f"apt-get install {' '.join(missing)} (output in {os.path.relpath(APT_LOG_FILE, CONFIG_DIR)})")
    try:
        _apt_install(missing)
    except Exception as err:  # noqa: BLE001
        log(f"installing the system packages FAILED ({err}); booting without them, see {os.path.relpath(APT_LOG_FILE, CONFIG_DIR)}")
        record.update(ok=False, note=f"not installed: {', '.join(missing)}", error=f"{type(err).__name__}: {err}")
        state["apt"] = record
        return
    log(f"installed the system packages {', '.join(missing)}")
    record["note"] = f"installed {', '.join(missing)}"
    state["apt"] = record


_install_log_started = False  # the first install of this boot started the file afresh


def install(version: str) -> bool:
    global _install_log_started
    d = venv_dir(version)
    _status.update(phase="preparing venv", version=version, kind="install", title=None)
    # one boot's installs per file: it used to grow forever (every pip run appended).  Only the first install of
    # the boot starts it afresh: pip's output goes nowhere else, and a second install (the image's own version
    # after the wanted one failed) truncating it took the reason of that failure with it
    try:
        with open(LOG_FILE, "a" if _install_log_started else "w", encoding="utf-8") as fh:
            fh.write(f"# install of Home Assistant {version}, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        _install_log_started = True
    except OSError:
        pass
    shutil.rmtree(d, ignore_errors=True)
    try:
        log(f"creating venv {d}")
        subprocess.run([sys.executable, "-m", "venv", d], check=True)
        # not -q: pip's per-package lines are the only progress this install has - the status page tails them
        # and _run_pip's idle budget counts them as "still working"
        pip = [os.path.join(d, "bin", "python"), "-m", "pip", "install", "--no-cache-dir", "--progress-bar", "off"]
        _status["phase"] = "downloading HA constraints"
        constraints = os.path.join(d, "package_constraints.txt")
        with urllib.request.urlopen(CONSTRAINTS_URL.format(version=version), timeout=60) as resp, open(constraints, "wb") as out:
            out.write(resp.read())
        _status["phase"] = f"pip install homeassistant=={version} (a few minutes)"
        log(f"pip install homeassistant=={version} -r {EXTRA_REQUIREMENTS}")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            # a hung download must not keep the boot on the status page forever: fails like any failed install
            # (a slow one runs on: the budget is on silence, see _run_pip)
            _run_pip([*pip, f"homeassistant=={version}", "-r", EXTRA_REQUIREMENTS, "-c", constraints], fh)
        # .ok makes the venv count as good (boot, fallback, prune): durable only after everything pip wrote is
        os.sync()
        with open(os.path.join(d, ".ok"), "w", encoding="utf-8") as fh:
            fh.write(version)
            fh.flush()
            os.fsync(fh.fileno())
        fsync_dir(d)
        _write_requirements_stamp(d)
        log(f"installed homeassistant=={version}")
        return True
    except Exception as err:  # noqa: BLE001
        log(f"install of {version} FAILED: {err}")
        shutil.rmtree(d, ignore_errors=True)
        return False


def _requirements_stamp() -> str:
    try:
        with open(EXTRA_REQUIREMENTS, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _write_requirements_stamp(venv: str) -> None:
    try:
        with open(os.path.join(venv, ".hri-requirements"), "w", encoding="utf-8") as fh:
            fh.write(_requirements_stamp())
    except OSError:
        pass


def ensure_extra_requirements(version: str) -> None:
    """A venv installed by an older image may lack what requirements.txt asks
    for now (the manager imports those packages): install them into it once
    per requirements.txt content.  A failure is logged, the boot goes on."""
    d = venv_dir(version)
    stamp = _requirements_stamp()
    if not stamp or not venv_ok(version):
        return
    try:
        with open(os.path.join(d, ".hri-requirements"), encoding="utf-8") as fh:
            if fh.read().strip() == stamp:
                return
    except OSError:
        pass
    cmd = [os.path.join(d, "bin", "python"), "-m", "pip", "install", "--no-cache-dir", "--progress-bar", "off", "-r", EXTRA_REQUIREMENTS]
    constraints = os.path.join(d, "package_constraints.txt")
    if os.path.isfile(constraints):
        cmd += ["-c", constraints]
    log(f"installing the manager's requirements into the venv of Home Assistant {version}")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            _run_pip(cmd, fh)  # killed half-way: the stamp is not written, the next boot runs it again
    except Exception as err:  # noqa: BLE001
        log(f"requirements install FAILED ({err}); booting with the venv as it is")
        return
    _write_requirements_stamp(d)


def prune(keep: set[str]) -> None:
    for name in os.listdir(CONFIG_DIR):
        if not name.startswith("venv-") or name == "venv-current":
            continue
        v = name[5:]
        if v in keep and venv_ok(v):
            continue
        # unused, or a half-installed leftover (no .ok / other Python)
        log(f"removing venv-{v}")
        shutil.rmtree(venv_dir(v), ignore_errors=True)


def clean_import_leftovers() -> None:
    """An uploaded HA backup / its extracted .storage (another instance's
    secrets) must not survive a restart that interrupted an import.  Nor an
    original an import set aside as .storage/<store>.pre-import (the import
    runs inside Home Assistant, so none is in progress at boot): put back, as
    the import undoes a failure.  One an import completed is renamed to
    .pre-import.done before its delete: removed, never put back."""
    storage = os.path.join(CONFIG_DIR, ".storage")
    for done in glob.glob(os.path.join(glob.escape(storage), "*.pre-import.done")):
        if os.path.isfile(done) and not os.path.islink(done):
            try:
                os.remove(done)
                log(f"removed leftover .storage/{os.path.basename(done)}")
            except OSError as err:
                log(f"could not remove .storage/{os.path.basename(done)} ({err})")
    for aside in glob.glob(os.path.join(glob.escape(storage), "*.pre-import")):
        if os.path.islink(aside) or not os.path.isfile(aside):
            continue
        try:
            os.replace(aside, aside[:-len(".pre-import")])
            log(f"put back .storage/{os.path.basename(aside)[:-len('.pre-import')]}: an interrupted import had set it aside")
        except OSError as err:
            log(f"could not put back .storage/{os.path.basename(aside)} ({err})")
    rebuild = os.path.isfile(REBUILD_FILE)
    for rel in ("import.tar", "import.tar.tmp", "import-extracted"):
        if rel == "import-extracted" and rebuild:
            continue  # the source of a scheduled clean start (this volume's own backup)
        p = os.path.join(STATE_DIR, rel)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            log(f"removed leftover {rel}")
        elif os.path.isfile(p):
            os.remove(p)
            log(f"removed leftover {rel}")


def reset_storage_for_rebuild(wanted: str, restored: bool, storage_restored: bool = False, restore_failed: bool = False) -> bool:
    """A downgrade with a clean start (scheduled from the manager): empty
    .storage before the older Home Assistant boots; the manager rebuilds
    the integration's configuration after the start.  Only for the version
    it was scheduled for, with its extracted source and a readable
    pre-change backup.  True when .storage was emptied.  ``restored``: a restore
    was applied at this boot; ``storage_restored``: it replaced .storage;
    ``restore_failed``: one ran and failed (nothing changed, or it was put back)."""
    try:
        with open(REBUILD_FILE, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(plan, dict):
        return False
    stage = plan.get("stage")
    aside_name = str(plan.get("aside") or "")
    aside = os.path.join(CONFIG_DIR, aside_name) if re.fullmatch(r"\.storage\.pre-rebuild-[0-9-]+", aside_name) else ""
    storage = os.path.join(CONFIG_DIR, ".storage")
    # "renaming" with the set-aside copy present: the boot that renamed .storage was killed before it
    # recorded the switch; .storage is (nearly) empty and the real configuration is in the copy
    switched = stage == "renaming" and bool(aside) and os.path.isdir(aside)
    why = ""
    if restored:
        why = "a restore was applied at this boot"
    elif restore_failed and wanted != plan.get("to"):
        why = f"Home Assistant {wanted} boots, not {plan.get('to')}"  # also once .storage was emptied for it ("import")
    elif stage not in ("reset", "renaming"):
        return False
    elif wanted != plan.get("to"):
        why = f"Home Assistant {wanted} boots, not {plan.get('to')}"
    else:
        try:
            with open(os.path.join(STATE_DIR, "import-extracted", "summary.json"), encoding="utf-8") as fh:
                summary = json.load(fh)
        except (OSError, ValueError):
            summary = None
        if not isinstance(summary, dict) or summary.get("type") != "ha-downgrade-rebuild":
            why = "its source (the extracted pre-change backup) is gone"
        else:
            try:
                backupkit.validate(os.path.join(CONFIG_DIR, backupkit.BACKUP_DIR, str(plan.get("backup"))))
            except Exception as err:  # noqa: BLE001 - a missing or unreadable backup must never stop the boot
                why = f"backup {plan.get('backup')} is not usable ({err})"
    if why:
        log(f"clean start dropped, the configuration is kept: {why}")
        if switched and not storage_restored:
            _put_aside_back(storage, aside)
        try:
            os.remove(REBUILD_FILE)
        except OSError:
            pass
        _drop_set_aside(aside, storage_restored, plan)
        return False
    if not switched:
        # the pre-change backup and the import source were taken when the switch was scheduled: anything
        # configured since exists only on the volume, so this boot backs it up before .storage goes (once:
        # a retry after an interrupted boot keeps that backup, never one of an already emptied .storage)
        if not plan.get("boot_backup"):
            try:
                plan["boot_backup"] = backupkit.create(CONFIG_DIR, f"pre-clean-start-{plan.get('to')}")["name"]
                write_json(REBUILD_FILE, plan)
            except Exception as err:  # noqa: BLE001 - no backup of the current state: the clean start does not happen
                log(f"clean start for Home Assistant {plan.get('to')} cancelled: the backup of the current configuration failed ({err}); the configuration is kept")
                return False
        # set aside in one rename: a delete that fails half-way would boot the older version
        # on part of the newer configuration; a failed rename leaves everything as it was.
        # The name is recorded first: a kill right after the rename must find the copy again
        aside_name = f".storage.pre-rebuild-{time.strftime('%Y%m%d-%H%M%S')}"
        aside = os.path.join(CONFIG_DIR, aside_name)
        try:
            write_json(REBUILD_FILE, {**plan, "stage": "renaming", "aside": aside_name})
        except OSError as err:
            log(f"clean start for Home Assistant {plan.get('to')} cancelled: its plan could not be written ({err}); the configuration is kept")
            return False
        try:
            if os.path.isdir(storage):
                os.rename(storage, aside)
            os.makedirs(storage, exist_ok=False)
        except OSError as err:
            log(f"clean start for Home Assistant {plan.get('to')} failed: .storage could not be set aside ({err}); the configuration is kept")
            if os.path.isdir(aside):
                _put_aside_back(storage, aside)
            try:
                write_json(REBUILD_FILE, {**plan, "stage": "reset"})
            except OSError:
                pass  # "renaming" without its copy is retried from the start
            return False
    else:
        os.makedirs(storage, exist_ok=True)
        log(f"clean start for Home Assistant {plan.get('to')}: finishing the switch an interrupted boot began ({aside_name})")
    # the set-aside copy stays until the manager finished the rebuild (ha_import.drop_rebuild removes it)
    plan.update(stage="import", aside=aside_name)
    write_json(REBUILD_FILE, plan)
    for old in glob.glob(f"{glob.escape(storage)}.pre-rebuild-*"):
        if old != aside:
            shutil.rmtree(old, ignore_errors=True)  # an earlier clean start's copy, only once this one is recorded
    log(f"clean start for Home Assistant {plan.get('to')}: .storage emptied, the integration is rebuilt after the boot (backup {plan.get('backup')})")
    return True


def _drop_set_aside(aside: str, storage_restored: bool, plan: dict) -> None:
    """The copy of .storage a dropped clean start leaves (emptied at an earlier boot, or not put back).  Nothing
    removes it later: ha_import.drop_rebuild, which does once a rebuild finished, runs only while a plan exists."""
    if not aside or not os.path.isdir(aside):
        return
    name = os.path.basename(aside)
    also = f"backup {plan.get('boot_backup') or plan.get('backup')}"
    if storage_restored:
        # the restore replaced what it held; a stale copy of every integration's credentials and the auth tokens
        # must not stay on the volume, the way a finished rebuild does not leave one
        shutil.rmtree(aside, ignore_errors=True)
        log(f"removed {name}: the restore replaced the configuration it held (that configuration is in {also})")
    else:
        log(f"{name} is kept: it holds the configuration from before the clean start, which this boot does not use "
            f"(it is also in {also}); delete it once it is not needed")


def _put_aside_back(storage: str, aside: str) -> None:
    """Undo the set-aside of .storage when the clean start does not go ahead (only over an empty .storage)."""
    try:
        if os.path.isdir(storage):
            os.rmdir(storage)  # fails when not empty: then nothing is overwritten
        os.rename(aside, storage)
    except OSError as err:
        log(f"putting .storage back failed ({err}): the configuration from before the clean start is in {aside}")


def _rebuild_stage(wanted: str) -> str | None:
    """Stage of the clean start planned for ``wanted`` ("import": .storage was already emptied for it)."""
    try:
        with open(REBUILD_FILE, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError):
        return None
    return plan.get("stage") if isinstance(plan, dict) and plan.get("to") == wanted else None


def restore_after_failed_change(state: dict, failed: str, fallback: str) -> bool:
    """The container wants to fall back from ``failed`` to ``fallback``.  If
    the switch to ``failed`` got as far as booting it (a restore, a clean
    start, or its own storage migrations in keep mode), the configuration
    from before the switch must come back from its pre-change backup first.
    That need is kept in ``state["recovery"]`` until a restore succeeded.
    False: the restore could not even be scheduled, so falling back would
    boot ``fallback`` on storage it may not read."""
    change = state.pop("change", None)
    recovery = state.get("recovery")
    if isinstance(change, dict) and change.get("to") == failed and change.get("applied") and change.get("backup"):
        # bring back what the switch replaced: .storage, or every part a restore with the switch put in place
        recovery = {"backup": change["backup"], "from": failed, "parts": change.get("parts") or ["storage"]}
    elif not (isinstance(recovery, dict) and recovery.get("from") == failed and recovery.get("backup")):
        return True  # nothing of a switch reached the storage: a plain fallback
    # "at": when this fallback's restore was scheduled.  A restore recorded before it belongs to something
    # else, so apply_config_changes cannot read it as this one having applied at an earlier boot.
    recovery = {**recovery, "for": fallback, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    state["recovery"] = recovery
    try:
        backupkit.schedule_restore(CONFIG_DIR, str(recovery["backup"]), recovery.get("parts") or ["storage"], for_version=fallback, force=True)
    except Exception as err:  # noqa: BLE001
        log(f"could not schedule the configuration from {recovery['backup']} for {fallback}: {err}")
        return False
    log(f"the configuration from before the switch to {failed} comes back from {recovery['backup']}")
    return True


RESTORE_RETRY_S = 300


def hold_after_failed_rollback(result: dict | None, apply) -> dict | None:
    """A restore whose rollback failed left the configuration half wiped: Home Assistant is not started on
    it (it would write fresh stores, which the retry then wipes again).  The status page explains it on the
    manager port and the restore is retried every RESTORE_RETRY_S until it applies or is put back.  Deleting
    integration_manager/restore-pending.json by hand ends the wait and boots the configuration as it is."""
    srv = None
    try:
        while isinstance(result, dict) and result.get("recovery_source") and not result.get("ok"):
            if not backupkit.pending(CONFIG_DIR):
                log("the restore schedule was removed: starting Home Assistant on the configuration as it is")
                break
            source = result["recovery_source"]
            _status.update(title="Home Assistant is not started: a restore failed and could not be put back", version=None, started=time.time(), kind="restore_hold",
                           phase=f"the configuration from before the restore is in backup {source}; retrying every {RESTORE_RETRY_S} s")
            if srv is None and _boot_server is None:  # the boot's own server shows this status already
                srv = start_status_server()
            log(f"Home Assistant NOT started: the configuration is half restored and the way back is backup {source}. "
                f"Free space or fix the error above; the restore is retried every {RESTORE_RETRY_S} s. "
                f"To start anyway on the configuration as it is, delete {backupkit.PENDING_META} on the volume")
            time.sleep(RESTORE_RETRY_S)
            if not backupkit.pending(CONFIG_DIR):
                continue
            result = apply()
    finally:
        if srv:
            stop_status_server(srv)
    return result


def apply_config_changes(state: dict, wanted: str, current: str | None) -> str:
    """After the install, before the boot: a restore or a clean start that
    belongs to a version change applies only when that version is the one
    booting (a failed install or a fallback boots another one).  Returns the
    version to boot: if a downgrade's restore or clean start did not happen,
    the version the configuration still belongs to."""
    def record_dropped(result: dict) -> bool:
        state["last_restore"] = result
        return save_state(state)

    backupkit.drop_orphan_schedule(CONFIG_DIR, log, record=record_dropped)  # a schedule whose archive is gone restores nothing
    for_version = backupkit.pending_for_version(CONFIG_DIR)
    if backupkit.pending(CONFIG_DIR) and for_version and for_version != wanted:
        backupkit.cancel_restore(CONFIG_DIR)
        log(f"restore scheduled for Home Assistant {for_version} dropped: {wanted} boots instead")
    made_on = backupkit.pending_ha_version(CONFIG_DIR)
    restore_parts = backupkit.pending_parts(CONFIG_DIR)
    if backupkit.pending(CONFIG_DIR) and not made_on and "storage" in restore_parts and not backupkit.pending_forced(CONFIG_DIR):
        # schedule_restore refuses these unless forced; a schedule that got here another way (an edited file) is not applied
        backupkit.cancel_restore(CONFIG_DIR)
        log("restore of a backup without a recorded Home Assistant version dropped: it was not confirmed (force)")
        state["last_error"] = "the scheduled restore was dropped: its backup does not record the Home Assistant version it was made on and the restore was not forced"
    if backupkit.pending(CONFIG_DIR) and made_on and "storage" in restore_parts and ha_vkey(made_on) > ha_vkey(wanted):
        # checked when it was scheduled, against the version wanted then; a cancelled switch or a failed
        # install boots another one, which cannot read a configuration made on a newer version
        backupkit.cancel_restore(CONFIG_DIR)
        log(f"restore of a backup made on Home Assistant {made_on} dropped: {wanted} boots and cannot read it")
        state["last_error"] = f"the scheduled restore (a backup made on Home Assistant {made_on}) was dropped: {wanted} boots, which cannot read it"
    own_restore = backupkit.pending(CONFIG_DIR) and for_version == wanted
    restored = False
    if backupkit.pending(CONFIG_DIR):
        # HA is not running here, so registries can be replaced safely.
        def record(result: dict) -> bool:
            result["for_version"] = for_version if own_restore else None
            state["last_restore"] = result
            return save_state(state)  # before the schedule is removed: see backupkit.apply_pending

        # after a crash fallback ha.json still names the crashed version as current, but the storage
        # was written by the newer of the two: a backup labelled with the older one would be offered
        # for a downgrade to exactly the version that cannot read it
        owner = current
        if state.get("fallback_from") and current and wanted:
            owner = max(current, wanted, key=backupkit.ha_vkey)
        def apply() -> dict | None:
            return backupkit.apply_pending(CONFIG_DIR, log, record=record, storage_version=owner)

        state["last_restore"] = hold_after_failed_rollback(apply(), apply)
        restored = True
    change = state.get("change")
    recovery = state.get("recovery")
    last = state.get("last_restore") if isinstance(state.get("last_restore"), dict) else {}
    if restored and last.get("ok") and "storage" in (last.get("parts") or []):
        # .storage is now a backup's, made on a version that boots here (a newer one's restore is dropped above):
        # _prepare's guard against booting an older version on a newer configuration does not apply (never saved)
        state["_config_for"] = wanted
    # a fallback's recovery; a switch the user scheduled to this version is not one (and a leftover must not stop it)
    if isinstance(recovery, dict) and recovery.get("for") == wanted and not (isinstance(change, dict) and change.get("to") == wanted):
        # the restore applied at an earlier boot that was killed after its outcome was recorded (which happens
        # before the schedule is removed) and before the state without "recovery" was saved: this boot finds
        # nothing scheduled any more, but the configuration on the volume is already the one that came back.
        # Starting "from" on it would migrate exactly that configuration forward, and start the version the
        # fallback ran away from.  Its backup and its timestamp tell that restore from one that belongs to
        # something else; a recovery written before "at" existed carries none and is judged by the backup.
        applied_before = (not restored and bool(last.get("ok")) and last.get("for_version") == wanted
                          and last.get("backup") == recovery.get("backup")
                          and str(last.get("at") or "") >= str(recovery.get("at") or ""))
        if (restored and bool(last.get("ok"))) or applied_before:
            state.pop("recovery", None)
            if applied_before:
                state["_config_for"] = wanted
        else:
            back = recovery.get("from")
            if back and back != wanted and venv_ok(back):
                log(f"restoring the configuration for the fallback to {wanted} failed: staying on {back}, whose storage this is; retried at the next fallback")
                state["last_error"] = (f"fallback to Home Assistant {wanted} stopped: the configuration from before the switch to {back} "
                                       f"could not be restored (see the container log); still on {back}")
                state["desired"] = back
                return back
            log(f"restoring the configuration for the fallback to {wanted} failed and {back} is not installed; booting {wanted}")
            state.pop("recovery", None)
    # a restore that failed and was put back changed nothing: it does not stand in for the clean start
    reset = reset_storage_for_rebuild(wanted, restored and bool(last.get("ok")), restored and bool(last.get("ok")) and "storage" in (last.get("parts") or []),
                                      restore_failed=restored and not last.get("ok"))
    if reset or _rebuild_stage(wanted) == "import":
        state["_config_for"] = wanted  # emptied for this version's clean start, now or at an earlier boot
    if not isinstance(change, dict):
        return wanted
    if change.get("to") != wanted:
        state.pop("change", None)  # that switch did not happen, nothing of it was applied
        return wanted
    mode = change.get("mode")
    # the change's own restore, with .storage (a restore scheduled by hand does not make a downgrade readable),
    # also when it was applied at a boot a power loss interrupted before ha.json recorded the change as applied
    own_now = restored and own_restore and "storage" in restore_parts and last.get("ok")
    own_before = (not restored and last.get("ok") and last.get("for_version") == wanted and "storage" in (last.get("parts") or [])
                  and str(last.get("at") or "") >= str(change.get("at") or ""))
    done = (mode == "restore" and bool(own_now or own_before)) or (mode == "rebuild" and (reset or _rebuild_stage(wanted) == "import"))
    if own_before:
        state["_config_for"] = wanted
    if mode in ("restore", "rebuild") and not done:
        what = "configuration restore" if mode == "restore" else "clean start"
        if current and current != wanted and venv_ok(current):
            log(f"the {what} for Home Assistant {wanted} did not happen: staying on {current}, whose configuration this still is")
            state["last_error"] = f"switch to Home Assistant {wanted} cancelled: its {what} failed (see the container log); still on {current}"
            state["desired"] = current
            state.pop("change", None)
            return current
        log(f"the {what} for Home Assistant {wanted} did not happen and there is no other version to stay on; booting {wanted}")
    # booting the target can migrate .storage in any mode (keep included):
    # from here on a crash loop must bring back the pre-change backup
    change["applied"] = True
    return wanted


def config_written_by(state: dict) -> str | None:
    """The Home Assistant version that last wrote this configuration: .HA_VERSION, which Home Assistant itself
    rewrites whenever another version boots here and which travels with the folder (a Supervisor backup of the app
    too, unlike ha.json's venvs); the version recorded as proven when that file says nothing."""
    try:
        with open(os.path.join(CONFIG_DIR, ".HA_VERSION"), encoding="utf-8") as fh:
            written = backupkit.known_ha_version(fh.readline().strip())
    except (OSError, ValueError):
        written = None
    return written or backupkit.known_ha_version(state.get("proven"))


def merge_applied_restore(state: dict) -> None:
    """A restore applied, or failed and put back, at an earlier boot whose outcome could not be written (a full disk)."""
    for rel, ok in ((backupkit.APPLIED_META, True), (backupkit.FAILED_META, False)):
        marker = os.path.join(CONFIG_DIR, rel)
        if not os.path.isfile(marker):
            continue
        try:
            with open(marker, encoding="utf-8") as fh:
                meta = json.load(fh)
            at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(marker)))
        except (OSError, ValueError):
            meta, at = {}, time.strftime("%Y-%m-%dT%H:%M:%S")
        meta = meta if isinstance(meta, dict) else {}
        state["last_restore"] = {"at": at, "ok": ok, "parts": meta.get("parts"), "backup": meta.get("name"),
                                 "error": "" if ok else "failed at an earlier boot and the previous configuration was put back (the error is in the container log)",
                                 "pre_restore": meta.get("pre_restore"), "for_version": meta.get("for_version"),
                                 "note": f"{'applied' if ok else 'failed'} at an earlier boot; recorded late (the volume was full)"}
        if save_state(state):
            try:
                os.remove(marker)
            except OSError:
                pass


def restrict_umask() -> int:
    """Files this process and the Home Assistant it execs create (restored and imported .storage with
    tokens, backups, uploads, venvs) are private to the container user: 0600 / 0700."""
    return os.umask(0o077)


def _phase(phase: str, version: str | None = None, title: str | None = None) -> None:
    _status.update(phase=phase, version=version, kind="install", title=title or f"Starting Home Assistant{' ' + version if version else ''} …")


def apply_app_options(path: str | None = None) -> list[str] | None:
    """As a Home Assistant app (SUPERVISOR_TOKEN set and the options file there), set or unset the variable of every
    option in APP_OPTIONS from the file and return the names set; an empty option is an unset variable.  Then
    APP_MARKER is set and the token variables are removed.  None when not an app, and so when run again in an
    environment this already cleaned: the environment is left as it is, the variables set the first time included.
    ValueError for a file that cannot be read: starting without the password it may hold would open the UI to the
    network."""
    path = path or APP_OPTIONS_FILE
    if not os.environ.get("SUPERVISOR_TOKEN") or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            options = json.load(fh)
    except (OSError, ValueError) as err:
        raise ValueError(f"{path} is not readable JSON ({type(err).__name__})") from None
    if not isinstance(options, dict):
        raise ValueError(f"{path} does not hold an object")
    applied = []
    for name, (var, flag) in APP_OPTIONS.items():
        value = options.get(name)
        if flag is not None:
            value = flag[0] if value is True else flag[1] if value is False else None
        elif isinstance(value, list):
            value = ",".join(str(x).strip() for x in value if str(x).strip())
        elif value is not None and not isinstance(value, bool):
            value = str(value)
        else:
            value = None
        if value:
            os.environ[var] = value
            applied.append(var)
        else:
            os.environ.pop(var, None)
    os.environ[APP_MARKER] = "1"
    for var in SUPERVISOR_TOKEN_VARS:
        os.environ.pop(var, None)
    return applied


def enable_app_watchdog(token: str) -> None:
    """Turn the app's Watchdog on, once per volume; fail-soft: a failure is logged (never the token) and the next
    start tries again.  docs/app.md says what it covers and how to do it by hand."""
    if not token or os.path.exists(APP_WATCHDOG_MARKER):
        return
    request = urllib.request.Request(SUPERVISOR_OPTIONS_URL, data=json.dumps({"watchdog": True}).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            resp.read()
    except Exception as err:  # noqa: BLE001 - the boot goes on either way
        log(f"the app's Watchdog could not be turned on ({type(err).__name__}: {err}); turn it on on the app's Info tab")
        return
    try:
        with open(APP_WATCHDOG_MARKER, "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
    except OSError as err:
        log(f"app watchdog marker not written ({err})")
    log("turned the app's Watchdog on (once for this volume: turning it off on the Info tab is respected)")


def read_app_watchdog(token: str) -> bool | None:
    """The app's Watchdog toggle from the Supervisor, None when it cannot be read; fail-soft, never logs the token."""
    if not token:
        return None
    request = urllib.request.Request(SUPERVISOR_INFO_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            watchdog = json.loads(resp.read(1 << 20))["data"]["watchdog"]
    except Exception as err:  # noqa: BLE001 - unknown: run.py restarts in place
        log(f"the app's Watchdog setting could not be read ({type(err).__name__}); a restart from HRI restarts in place")
        return None
    if not isinstance(watchdog, bool):
        log("the app's Watchdog setting could not be read (not a bool); a restart from HRI restarts in place")
        return None
    return watchdog


def main() -> None:
    global _boot_server
    restrict_umask()  # first: inherited by everything created from here on, and by the exec'd Home Assistant
    os.makedirs(STATE_DIR, exist_ok=True)  # before the first log() call
    token = os.environ.get("SUPERVISOR_TOKEN", "")  # apply_app_options removes it from the environment
    try:
        applied = apply_app_options()
    except ValueError as err:
        log(f"app options: {err}; not starting")
        sys.exit(2)
    if applied is not None:  # names only: an option can be the password
        log(f"running as a Home Assistant app; from its options: {', '.join(applied) or 'nothing set'}")
        enable_app_watchdog(token)
        watchdog = read_app_watchdog(token)
        if watchdog is None:
            os.environ.pop(APP_WATCHDOG_VAR, None)
        else:
            os.environ[APP_WATCHDOG_VAR] = "1" if watchdog else "0"
    token = ""
    if PORT is None:
        log(f"HRI_PORT={os.environ.get('HRI_PORT')!r} is not a TCP port (1-65535): fix the container's environment; not starting")
        sys.exit(2)
    if not DEFAULT_VERSION.strip():
        log("HA_VERSION_DEFAULT is empty: the image sets it to the Home Assistant version it was built with, so this "
            "container's environment overrides it with nothing; remove that override. Not starting")
        sys.exit(2)
    _phase("checking the volume")
    _boot_server = start_status_server()
    try:
        python = _prepare()
    except BaseException:
        _stop_boot_server()
        raise
    _stop_boot_server()  # run.py binds the same port
    os.execv(python, [python, "/app/run.py"])


def _stop_boot_server() -> None:
    global _boot_server
    if _boot_server is not None:
        stop_status_server(_boot_server)
        _boot_server = None


def nothing_has_run_here(state: dict) -> bool:
    """True only for a volume that has never had a Home Assistant on it: no version recorded in ha.json and
    no .storage for one to have written.  "No usable venv" is NOT that test - a Python bump in the image
    makes venv_ok fail for every venv on the volume - and Home Assistant migrates .storage forward only, so
    installing the image's own older version there would downgrade a running instance onto newer storage."""
    if any(state.get(key) for key in ("current", "proven", "previous")):
        return False
    return not os.path.isdir(os.path.join(CONFIG_DIR, ".storage"))


def _prepare() -> str:
    """Everything before the exec; returns the venv's python."""
    sweep_json_tmp_files()
    clean_import_leftovers()
    state = load_state()
    merge_applied_restore(state)
    # before anything pip does: a wheel that links against a system library, and Home Assistant itself
    # (the ffmpeg binary), find what the operator asked for already there
    ensure_apt_packages(state)
    wanted = state.get("desired") or state.get("current")
    fresh = not wanted and nothing_has_run_here(state)
    if not wanted and not fresh:
        # ha.json is missing (deleted, a volume restored without it) or was dropped as unreadable, and
        # .storage says a Home Assistant has run here.  The venvs on the volume say which one; only if
        # none of them can run on this image's Python is a version chosen below - and never the image's
        # own default, which is older than what the operator runs, on a .storage HA migrates forward only.
        wanted = _recovered_current()
        if wanted:
            log(f"no version recorded, but this volume has run: continuing with {wanted} from the volume, pruning disabled this boot")
            state["_corrupt"] = True  # what the volume held is a guess: no venv is removed on this boot
    if not wanted:
        # No version to run and none on the volume: start from the newest stable HA, not the version the
        # image happened to be built with (HA_VERSION_LATEST=0 disables that).  The image's own default
        # is the last resort of a FRESH volume only - it is older than what an operator runs, and Home
        # Assistant migrates .storage forward only.
        _phase("asking PyPI for the newest Home Assistant")
        wanted = latest_stable() if os.environ.get("HA_VERSION_LATEST", "1") != "0" else None
        if not wanted and fresh:
            wanted = DEFAULT_VERSION
        if not wanted:
            # A used volume, nothing on it this Python can run, and PyPI could not be asked (unreachable,
            # or HA_VERSION_LATEST=0): the image's default is the one answer that must not be given here.
            log("no Home Assistant version is recorded for this volume, none of its venvs runs on this image's "
                "Python, and the newest release could not be looked up: not installing this image's own "
                f"{DEFAULT_VERSION} over an existing configuration; exiting")
            state["last_error"] = ("no Home Assistant version is recorded for this volume and the newest release could not "
                                   "be looked up; this image's own older version is not installed over the existing "
                                   "configuration (see the container log)")
            save_state(state)
            sys.exit(1)
        if MIN_VERSION and ha_vkey(wanted) < ha_vkey(MIN_VERSION):
            log(f"Home Assistant {wanted} is older than this image's floor {MIN_VERSION}: installing {MIN_VERSION} instead")
            wanted = MIN_VERSION
        log(f"{'fresh volume' if fresh else 'no version recorded for this volume'}: installing Home Assistant {wanted}")
    current = state.get("current")
    # last_error stays until a new version change is asked for (ha_updater.set_desired clears it): a
    # fallback or a failed install must still be visible after the next ordinary restart

    # A venv can install fine and still fail to boot (a package HA dropped,
    # an incompatible integration).  run.py resets boot_failures once HA is
    # ready and records the version as "proven"; after MAX_BOOT_FAILURES
    # consecutive crashes on a version that never booted, go back to the
    # previous venv and say why.  A version that booted before is not the
    # cause (a changed setting, the port, memory): it is never left automatically.
    failures = _count(state.get("boot_failures"))
    previous = state.get("previous")
    fallback_from = state.get("fallback_from")
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    fallback_to = previous or recovery.get("for")  # a stopped fallback keeps its target for the retry
    change = state.get("change") if isinstance(state.get("change"), dict) else {}
    if "proven" not in state and current and change.get("to") != current:
        # a volume from before "proven" was recorded: the version it ran booted, unless a change to it is still pending
        state["proven"] = current
    fell_back = False
    if failures >= MAX_BOOT_FAILURES and fallback_from and fallback_from != wanted:
        # We already fell back once and the previous version fails too:
        # the problem is not the HA version (port clash, broken manager,
        # corrupt .storage).  Stop flipping, keep the message honest.
        log(f"{wanted} fails to boot as well as {fallback_from}: not a Home Assistant version problem; retrying")
        state["last_error"] = f"both {fallback_from} and {wanted} crash at boot: not a HA version problem (see container log)"
        save_state(state)
    elif failures >= MAX_BOOT_FAILURES and wanted == state.get("proven"):
        # counting goes on (the UI and the log show it), but no downgrade: an older version on this version's
        # storage, for a cause that is not the version, would only crash too
        log(f"{wanted} failed to boot {failures} times, but it booted before: not a Home Assistant version problem "
            "(a setting, the port, memory, the configuration); retrying it, no fallback")
        state["last_error"] = (f"Home Assistant {wanted} crashed at boot {failures} times in a row; it booted fine before, "
                               "so it is not rolled back automatically (see the container log)")
        save_state(state)
    elif failures >= MAX_BOOT_FAILURES and fallback_to and fallback_to != wanted and venv_ok(fallback_to):
        if restore_after_failed_change(state, wanted, fallback_to):
            log(f"{wanted} failed to boot {failures} times; falling back to {fallback_to}")
            state["last_error"] = f"{wanted} crashed at boot {failures} times; rolled back to {fallback_to} (see container log)"
            state["fallback_from"] = wanted
            state["desired"] = wanted = fallback_to
            fell_back = True
        else:
            log(f"{wanted} failed to boot {failures} times, but its configuration cannot be brought back for {fallback_to}: staying on {wanted}")
            state["last_error"] = (f"{wanted} crashed at boot {failures} times; no fallback to {fallback_to}, because the configuration "
                                   "from before the switch could not be restored (see container log)")
        state["boot_failures"] = 0
        save_state(state)
    elif failures >= MAX_BOOT_FAILURES:
        # a fresh volume whose first version cannot boot: nothing to go back to, but say what is wrong
        # rather than leaving the last error of a boot that never happened
        log(f"{wanted} failed to boot {failures} times and there is nothing to fall back to; retrying")
        state["last_error"] = (f"Home Assistant {wanted} crashed at boot {failures} times in a row and this volume has no "
                               "other version to go back to (see the container log)")
        save_state(state)

    if not venv_ok(wanted):
        _phase(f"checking that Home Assistant {wanted} supports this Python", wanted)
        if not fits_this_python(wanted):
            _phase("asking PyPI for the newest Home Assistant that supports this Python")
            other = latest_stable()
            py = ".".join(str(x) for x in sys.version_info[:3])
            if other and other != wanted:
                log(f"Home Assistant {wanted} does not support Python {py} (a newer image?): installing {other} instead")
                state["last_error"] = f"Home Assistant {wanted} does not support this image's Python {py}; {other} is installed instead"
                state["desired"] = wanted = other
    if not venv_ok(wanted):
        ok = install(wanted)
        if not ok:
            fallback = current if current and venv_ok(current) else next(iter(reversed(installed_versions())), None)
            if fallback is None and nothing_has_run_here(state):
                # A fresh volume whose very first install fails (a release published today with no wheel for
                # this image's Python) has no other venv to go back to - except the version the image was
                # built and tested with.  One extra attempt, here: exiting instead left a new user in a
                # restart loop that asked PyPI for the same broken version at every boot.  Only here: on a
                # volume that has run, the image default is usually OLDER than what the operator runs.
                baked = MIN_VERSION if MIN_VERSION and ha_vkey(DEFAULT_VERSION) < ha_vkey(MIN_VERSION) else DEFAULT_VERSION
                if baked != wanted and install(baked):
                    log(f"install of {wanted} failed; installed this image's own {baked} instead")
                    state["last_error"] = f"Home Assistant {wanted} could not be installed; this image's {baked} is running instead"
                    state["desired"] = wanted = baked  # recorded, so the next boot does not try the broken one again
                    save_state(state)
                else:
                    state["last_error"] = f"install of Home Assistant {wanted} failed and this volume has no other version to run"
                    save_state(state)
                    log("no working Home Assistant venv; exiting")
                    sys.exit(1)
            elif fallback is None:
                # Something has run here: no venv on the volume works on this image's Python (a Python bump),
                # and the version that does is the one that just failed to install - a passing network error
                # is enough.  Say so and let the boot fail: installing an older Home Assistant over a .storage
                # a newer one has migrated is not something this manager does anywhere else without asking.
                # desired stays as it is, so a retry installs what the operator actually runs.
                log(f"install of {wanted} failed and no venv on this volume runs on this image's Python; "
                    "not installing an older Home Assistant over the existing configuration: exiting")
                state["last_error"] = (f"install of Home Assistant {wanted} failed, and no version already on this volume can "
                                       "run on this image's Python; no older version is installed over the existing "
                                       "configuration (see the container log)")
                save_state(state)
                sys.exit(1)
            else:
                state["last_error"] = f"install of {wanted} failed; running {fallback}"
                state["desired"] = fallback
                wanted = fallback

    _phase("installing the manager's requirements into the venv if they changed", wanted)
    ensure_extra_requirements(wanted)
    _phase("applying a scheduled restore or clean start, if any", wanted)
    wanted = apply_config_changes(state, wanted, current)
    # Home Assistant migrates its configuration forward only.  Whatever chose ``wanted`` above (a switch whose restore
    # or clean start did not happen - a Supervisor restore of a backup taken while one was scheduled -, the newest
    # release for an older image's Python, the fallback after a failed install), it is not started on a configuration
    # a newer version wrote, unless that configuration was just put there for it or the operator chose to keep it.
    writer = config_written_by(state)
    change = state.get("change") if isinstance(state.get("change"), dict) else {}
    kept = change.get("to") == wanted and change.get("mode") == "keep"  # a downgrade asked for as it is, with its warning
    if writer and ha_vkey(wanted) < ha_vkey(writer) and state.get("_config_for") != wanted and not kept:
        reason = (f"Home Assistant {wanted} was not started: the configuration on this volume was last written by Home "
                  f"Assistant {writer}, which an older version cannot read. Run {writer} or newer (the image that ran it, "
                  f"or \"desired\": \"{writer}\" in integration_manager/ha.json), or restore a backup made on Home "
                  f"Assistant {wanted} or older")
        log(f"{reason}; exiting")
        # only the reason is recorded: what this boot chose instead (a substitute version, a switch marked applied)
        # is not, so the next boot decides again from what the operator asked for
        refused = load_state()
        refused["last_error"] = reason
        save_state(refused)
        sys.exit(1)

    if current and current != wanted and venv_ok(current) and not fell_back and failures < MAX_BOOT_FAILURES:
        # (a version that just crashed its way into a fallback is not a rollback target)
        state["previous"] = current
    if fell_back:
        # the venv that crashed goes once the fallback booted: do not offer it as a rollback target
        state.pop("previous", None)
    # fallback_from is cleared by run.py once HA actually reaches STARTED; until then its venv is kept
    state["current"] = wanted
    state.setdefault("desired", wanted)
    # This version has not booted yet: run.py records "proven" when it does.  Seeding the key keeps the
    # compatibility rule above (a volume from before "proven" existed ran a version that booted) from
    # claiming a fresh volume's first install, where "current" is a version nothing has ever started -
    # which turned a version that cannot boot at all into a loop no fallback would break.
    state.setdefault("proven", "")
    save_state(state)
    _phase("removing unused venvs", wanted)
    if not state.get("_corrupt"):
        keep = {wanted, state.get("previous") or wanted, state.get("fallback_from") or wanted}
        prune(keep | ({state["recovery"]["for"]} if isinstance(state.get("recovery"), dict) and state["recovery"].get("for") else set()))

    # Stable path for humans and scripts (docker exec ... /config/venv-current/bin/python); replaced in one
    # rename, never missing (the temporary name does not start with venv-: prune would take it for a version)
    link = os.path.join(CONFIG_DIR, "venv-current")
    tmp_link = os.path.join(CONFIG_DIR, ".venv-current.tmp")
    try:
        if os.path.lexists(tmp_link):
            os.remove(tmp_link)
        os.symlink(venv_dir(wanted), tmp_link)
        os.replace(tmp_link, link)
    except OSError as err:
        log(f"venv-current symlink not updated: {err}")
    python = os.path.join(venv_dir(wanted), "bin", "python")
    os.environ["SETUP_PORT"] = str(PORT)  # HA http default port, read at import time
    # counted only now: a stop during the slow work above (pruning venvs) is not a failed boot
    state["boot_failures"] = _count(state.get("boot_failures")) + 1  # run.py zeroes it when ready
    save_state(state)
    log(f"starting Home Assistant {wanted} via {python}")
    return python


def _on_sigterm(signum: int, _frame) -> None:
    """PID 1 without an init ignores SIGTERM unless it handles it: `docker stop` during an install waited the whole
    grace period.  SystemExit unwinds like Ctrl-C does: _run_pip kills pip's process group, a restore being applied
    keeps its schedule for the next boot, the status server is stopped.  The exec resets the handler for run.py."""
    log(f"signal {signum}: stopping before Home Assistant starts")
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_sigterm)
    main()
