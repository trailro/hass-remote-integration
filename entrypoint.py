"""Container entrypoint: make sure the wanted Home Assistant version is
installed in a venv ON THE VOLUME, then hand over to run.py inside it.

Why: HA used to be baked into the image, which made "update HA" a rebuild.
Now the image is only Python + this bootstrap; HA lives in
``/config/venv-<version>`` and survives image rebuilds and container
recreation.  The manager UI changes the wanted version in
``/config/integration_manager/ha.json`` and restarts the process; this script
does the rest, keeps the previous venv for rollback, and falls back to the
last working venv if an install fails (the UI then shows the error).

While installing (a few minutes on first boot) a tiny status page answers
on the manager port so the browser is not left with a connection error.
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
DEFAULT_VERSION = os.environ.get("HA_VERSION_DEFAULT", "2026.8.3")
EXTRA_REQUIREMENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")  # installed next to homeassistant
MAX_BOOT_FAILURES = 3
PIP_IDLE_TIMEOUT_S = 15 * 60  # pip writes a line per package: nothing at all for this long is a hang, not a slow download
PIP_POLL_S = 5
STATUS_RETRY_AFTER_S = 5  # the install page refreshes itself this often; clients polling /api/ may do the same
STATE_DIR = os.path.join(CONFIG_DIR, "integration_manager")
HA_FILE = os.path.join(STATE_DIR, "ha.json")
LOG_FILE = os.path.join(STATE_DIR, "ha-install.log")
REBUILD_FILE = os.path.join(STATE_DIR, "rebuild-pending.json")  # custom_components/integration_manager/ha_import.py
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
    """boot_failures as written by hand or by an older version: anything that is not a number counts as 0."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


TMP_SWEEP_AGE_S = 600
_JSON_TMP = re.compile(r".+\.json\.[^.]+\.tmp")  # jsonio.write_json's mkstemp names


def sweep_json_tmp_files() -> None:
    """A kill between jsonio.write_json's mkstemp and its replace leaves the tmp file behind for good (nothing else
    ever matches its random name).  Only on the volume's top level and integration_manager/, only old ones."""
    now = time.time()
    for d in (CONFIG_DIR, STATE_DIR):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            try:
                if _JSON_TMP.fullmatch(name) and os.path.isfile(path) and not os.path.islink(path) and now - os.path.getmtime(path) > TMP_SWEEP_AGE_S:
                    os.remove(path)
                    log(f"removed leftover {os.path.relpath(path, CONFIG_DIR)}")
            except OSError:
                pass


def save_state(state: dict) -> bool:
    """Atomic: a torn ha.json would read as {} and silently reinstall the
    image default version (and prune the one that was running)."""
    try:
        write_json(HA_FILE, {k: v for k, v in state.items() if k != "_corrupt"})
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


def status_host_ok(host: str) -> bool:
    """The manager's DNS-rebinding rule (hostguard._host_ok) for the page served while HA installs."""
    try:
        with open(os.path.join(STATE_DIR, "settings.json"), encoding="utf-8") as fh:
            extra_raw = str((json.load(fh) or {}).get("allowed_hosts") or "")
    except (OSError, ValueError, AttributeError):
        extra_raw = ""
    # a fully qualified name may end in one dot (foo.local.): the same host, as hostguard treats it
    extra = {re.sub(r":\d+$", "", x.strip().lower()).removesuffix(".") for x in extra_raw.split(",") if x.strip()}
    h = (host or "").strip().lower()
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    h = h.removesuffix(".")
    if not h:
        return False
    if h in ("localhost", socket.gethostname().lower()) or h in extra:
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
    return bool(os.environ.get("HRI_PASSWORD", "").strip() or os.environ.get("HRI_PASSWORD_FILE", "").strip())


def _log_tail() -> str:
    try:
        with open(LOG_FILE, "rb") as fh:  # last 64 KB only, the file may be long
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            return "\n".join(fh.read().decode("utf-8", errors="replace").splitlines()[-40:])
    except OSError:
        return ""


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if not status_host_ok(self.headers.get("Host", "")):
            self.send_error(403, "Host not allowed (DNS rebinding guard)")
            return
        # Nothing here is the manager API: while Home Assistant installs, /api/status, /api/diag/health and
        # any healthcheck used to get this HTML page with a 200 and call the container healthy for the whole
        # install.  503 says what is true, and the page is served with it too - browsers render the body.
        if urllib.parse.urlsplit(self.path).path.startswith("/api/"):
            self._send(503, "application/json", json.dumps(install_status()).encode())
            return
        if _status.get("kind") != "install":
            tail = ""  # the log is of the last install, nothing to do with a restore that failed
        elif password_configured():
            tail = "(the install log is shown after login, on the System page)"  # no login exists yet: show only the phase
        else:
            # without a password the manager shows the same log to anyone once it runs; until then this is the
            # only place pip's progress appears (pip writes into the file, not into the container log)
            tail = _log_tail()
        heading = _status.get("title") or f"Installing Home Assistant {_status['version']} …"
        log_block = f"<pre style='font:12px ui-monospace;color:#8b98a5;white-space:pre-wrap'>{html.escape(tail)}</pre>" if tail else ""
        body = (
            "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5>"
            f"<title>hass-remote-integration · {html.escape(heading)}</title>"
            "<body style='font:14px system-ui;background:#0f1418;color:#e6edf3;padding:24px'>"
            f"<h2>{html.escape(heading)}</h2>"
            f"<p>phase: <b>{html.escape(str(_status['phase']))}</b> · {int(time.time() - _status['started'])} s so far · this page refreshes itself</p>"
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


def _run_pip(cmd: list[str], out, idle_timeout: float = PIP_IDLE_TIMEOUT_S) -> None:
    """subprocess.run(check=True), with pip in its own process group: a kill takes the whole group, also the
    build backends pip started (a kill of pip alone left those running).

    The budget is on silence, not on the whole run: the fixed wall clock it replaced killed an install that was
    still working (a small machine, a slow mirror, a big wheel) and then threw the venv away, so the retry
    started from zero and ran into the same wall.  pip writes a line per package into ``out``, so the file
    growing is progress; nothing written for ``idle_timeout`` is a hang and still ends the install."""
    with subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True) as proc:
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


def install(version: str) -> bool:
    d = venv_dir(version)
    _status.update(phase="preparing venv", version=version, kind="install", title=None)
    try:  # one install per file: it used to grow forever (every pip run appended)
        with open(LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"# install of Home Assistant {version}, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
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
    the import undoes a failure."""
    storage = os.path.join(CONFIG_DIR, ".storage")
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


def reset_storage_for_rebuild(wanted: str, restored: bool, storage_restored: bool = False) -> bool:
    """A downgrade with a clean start (scheduled from the manager): empty
    .storage before the older Home Assistant boots; the manager rebuilds
    the integration's configuration after the start.  Only for the version
    it was scheduled for, with its extracted source and a readable
    pre-change backup.  True when .storage was emptied.  ``storage_restored``:
    the restore applied at this boot replaced .storage."""
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
    for old in glob.glob(f"{storage}.pre-rebuild-*"):
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
    recovery = {**recovery, "for": fallback}
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
    # a fallback's recovery; a switch the user scheduled to this version is not one (and a leftover must not stop it)
    if isinstance(recovery, dict) and recovery.get("for") == wanted and not (isinstance(change, dict) and change.get("to") == wanted):
        if restored and (state.get("last_restore") or {}).get("ok"):
            state.pop("recovery", None)
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
    last = state.get("last_restore") if isinstance(state.get("last_restore"), dict) else {}
    reset = reset_storage_for_rebuild(wanted, restored, restored and bool(last.get("ok")) and "storage" in (last.get("parts") or []))
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


def main() -> None:
    global _boot_server
    restrict_umask()  # first: inherited by everything created from here on, and by the exec'd Home Assistant
    os.makedirs(STATE_DIR, exist_ok=True)  # before the first log() call
    if PORT is None:
        log(f"HRI_PORT={os.environ.get('HRI_PORT')!r} is not a TCP port (1-65535): fix the container's environment; not starting")
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


def _prepare() -> str:
    """Everything before the exec; returns the venv's python."""
    sweep_json_tmp_files()
    clean_import_leftovers()
    state = load_state()
    merge_applied_restore(state)
    wanted = state.get("desired") or state.get("current")
    if not wanted:
        # Fresh volume: start from the newest stable HA, not the version the
        # image happened to be built with (HA_VERSION_LATEST=0 disables that).
        _phase("asking PyPI for the newest Home Assistant")
        wanted = (latest_stable() if os.environ.get("HA_VERSION_LATEST", "1") != "0" else None) or DEFAULT_VERSION
        log(f"fresh volume: installing Home Assistant {wanted}")
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
        log(f"{wanted} failed to boot {failures} times and there is nothing to fall back to; retrying")

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
            state["last_error"] = f"install of {wanted} failed; running {fallback}"
            state["desired"] = fallback
            if fallback is None:
                save_state(state)
                log("no working Home Assistant venv; exiting")
                sys.exit(1)
            wanted = fallback

    _phase("installing the manager's requirements into the venv if they changed", wanted)
    ensure_extra_requirements(wanted)
    _phase("applying a scheduled restore or clean start, if any", wanted)
    wanted = apply_config_changes(state, wanted, current)

    if current and current != wanted and venv_ok(current) and not fell_back and failures < MAX_BOOT_FAILURES:
        # (a version that just crashed its way into a fallback is not a rollback target)
        state["previous"] = current
    if fell_back:
        # the venv that crashed goes once the fallback booted: do not offer it as a rollback target
        state.pop("previous", None)
    # fallback_from is cleared by run.py once HA actually reaches STARTED; until then its venv is kept
    state["current"] = wanted
    state.setdefault("desired", wanted)
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
