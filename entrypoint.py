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
import subprocess
import sys
import threading
import time
import urllib.request

import backupkit  # /app/backupkit.py: apply a restore scheduled from the UI
from jsonio import vkey, write_json

CONFIG_DIR = os.environ.get("HRI_CONFIG", "/config")
PORT = int(os.environ.get("HRI_PORT", "8087"))
DEFAULT_VERSION = os.environ.get("HA_VERSION_DEFAULT", "2026.8.3")
EXTRA_REQUIREMENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")  # installed next to homeassistant
MAX_BOOT_FAILURES = 3
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
        op, want = m.group(1), vkey(m.group(2))
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
        if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", v) or not files or vkey(v) < vkey(DEFAULT_VERSION):
            continue
        if not _python_fits(files[0].get("requires_python")):
            continue
        if best is None or vkey(v) > vkey(best):
            best = v
    return best

_status = {"phase": "starting", "version": None, "started": time.time()}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [entrypoint] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # a full disk must not stop the boot: the UI is where space gets freed


def load_state() -> dict:
    """Missing file = fresh volume.  Present but unparsable = torn write:
    never treat that as fresh (it would reinstall the image default and
    prune the venv that was running); the returned dict then carries
    ``_corrupt`` (never saved: save_state strips it)."""
    try:
        with open(HA_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        current = None
        link = os.path.join(CONFIG_DIR, "venv-current")
        if os.path.islink(link):
            current = os.path.basename(os.readlink(link))[5:]
        if not current or not venv_ok(current):
            current = next(iter(reversed(installed_versions())), None)
        log(f"ha.json is unreadable; recovered current={current} from the volume, pruning disabled this boot")
        return {"current": current, "desired": current, "last_error": "ha.json was corrupt and has been rebuilt", "_corrupt": True}


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
    return sorted(out, key=vkey)


SAFE_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")  # as hostguard.py


def status_host_ok(host: str) -> bool:
    """The manager's DNS-rebinding rule (hostguard._host_ok) for the page served while HA installs."""
    try:
        with open(os.path.join(STATE_DIR, "settings.json"), encoding="utf-8") as fh:
            extra_raw = str((json.load(fh) or {}).get("allowed_hosts") or "")
    except (OSError, ValueError, AttributeError):
        extra_raw = ""
    extra = {re.sub(r":\d+$", "", x.strip().lower()) for x in extra_raw.split(",") if x.strip()}
    h = (host or "").strip().lower()
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    if not h:
        return False
    if h in ("localhost", socket.gethostname().lower()) or h in extra:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return h.endswith(SAFE_HOST_SUFFIXES)


def password_configured() -> bool:
    return bool(os.environ.get("HRI_PASSWORD", "").strip() or os.environ.get("HRI_PASSWORD_FILE", "").strip())


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if not status_host_ok(self.headers.get("Host", "")):
            self.send_error(403, "Host not allowed (DNS rebinding guard)")
            return
        try:
            with open(LOG_FILE, "rb") as fh:  # last 64 KB only, the file may be long
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 65536))
                tail = "\n".join(fh.read().decode("utf-8", errors="replace").splitlines()[-40:])
        except OSError:
            tail = ""
        if password_configured():
            tail = "(the install log is shown after login, on the System page)"  # no login exists yet: show only the phase
        body = (
            "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5>"
            "<title>hass-remote-integration · installing HA</title>"
            "<body style='font:14px system-ui;background:#0f1418;color:#e6edf3;padding:24px'>"
            f"<h2>Installing Home Assistant {_status['version']} …</h2>"
            f"<p>phase: <b>{_status['phase']}</b> · {int(time.time() - _status['started'])} s so far · this page refreshes itself</p>"
            f"<pre style='font:12px ui-monospace;color:#8b98a5;white-space:pre-wrap'>{html.escape(tail)}</pre>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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


def install(version: str) -> bool:
    d = venv_dir(version)
    _status.update(phase="preparing venv", version=version)
    try:  # one install per file: it used to grow forever (every pip run appended)
        with open(LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"# install of Home Assistant {version}, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except OSError:
        pass
    shutil.rmtree(d, ignore_errors=True)
    try:
        log(f"creating venv {d}")
        subprocess.run([sys.executable, "-m", "venv", d], check=True)
        pip = [os.path.join(d, "bin", "python"), "-m", "pip", "install", "--no-cache-dir", "-q"]
        _status["phase"] = "downloading HA constraints"
        constraints = os.path.join(d, "package_constraints.txt")
        with urllib.request.urlopen(CONSTRAINTS_URL.format(version=version), timeout=60) as resp, open(constraints, "wb") as out:
            out.write(resp.read())
        _status["phase"] = f"pip install homeassistant=={version} (a few minutes)"
        log(f"pip install homeassistant=={version} -r {EXTRA_REQUIREMENTS}")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            subprocess.run(
                [*pip, f"homeassistant=={version}", "-r", EXTRA_REQUIREMENTS, "-c", constraints],
                check=True, stdout=fh, stderr=subprocess.STDOUT,
            )
        with open(os.path.join(d, ".ok"), "w", encoding="utf-8") as fh:
            fh.write(version)
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
    cmd = [os.path.join(d, "bin", "python"), "-m", "pip", "install", "--no-cache-dir", "-q", "-r", EXTRA_REQUIREMENTS]
    constraints = os.path.join(d, "package_constraints.txt")
    if os.path.isfile(constraints):
        cmd += ["-c", constraints]
    log(f"installing the manager's requirements into the venv of Home Assistant {version}")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            subprocess.run(cmd, check=True, stdout=fh, stderr=subprocess.STDOUT, timeout=900)
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
    secrets) must not survive a restart that interrupted an import."""
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


def reset_storage_for_rebuild(wanted: str, restored: bool) -> bool:
    """A downgrade with a clean start (scheduled from the manager): empty
    .storage before the older Home Assistant boots; the manager rebuilds
    the integration's configuration after the start.  Only for the version
    it was scheduled for, with its extracted source and a readable
    pre-change backup.  True when .storage was emptied."""
    try:
        with open(REBUILD_FILE, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(plan, dict):
        return False
    why = ""
    if restored:
        why = "a restore was applied at this boot"
    elif plan.get("stage") != "reset":
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
        try:
            os.remove(REBUILD_FILE)
        except OSError:
            pass
        return False
    # the pre-change backup and the import source were taken when the switch was scheduled: anything
    # configured since exists only on the volume, so this boot backs it up before .storage goes
    try:
        plan["boot_backup"] = backupkit.create(CONFIG_DIR, f"pre-clean-start-{plan.get('to')}")["name"]
        write_json(REBUILD_FILE, plan)
    except Exception as err:  # noqa: BLE001 - no backup of the current state: the clean start does not happen
        log(f"clean start for Home Assistant {plan.get('to')} cancelled: the backup of the current configuration failed ({err}); the configuration is kept")
        return False
    storage = os.path.join(CONFIG_DIR, ".storage")
    # set aside in one rename: a delete that fails half-way would boot the older version
    # on part of the newer configuration; a failed rename leaves everything as it was
    aside = f"{storage}.pre-rebuild-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        if os.path.isdir(storage):
            os.rename(storage, aside)
        os.makedirs(storage, exist_ok=False)
    except OSError as err:
        log(f"clean start for Home Assistant {plan.get('to')} failed: .storage could not be set aside ({err}); the configuration is kept")
        if os.path.isdir(aside) and not os.path.exists(storage):
            try:
                os.rename(aside, storage)
            except OSError as back:
                log(f"putting .storage back failed too ({back}): it is in {aside}")
        return False
    for old in glob.glob(f"{storage}.pre-rebuild-*"):
        if old != aside:
            shutil.rmtree(old, ignore_errors=True)  # an earlier clean start's copy
    # the set-aside copy stays until the manager finished the rebuild (ha_import.drop_rebuild removes it)
    plan["stage"] = "import"
    write_json(REBUILD_FILE, plan)
    log(f"clean start for Home Assistant {plan.get('to')}: .storage emptied, the integration is rebuilt after the boot (backup {plan.get('backup')})")
    return True


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
        backupkit.schedule_restore(CONFIG_DIR, str(recovery["backup"]), recovery.get("parts") or ["storage"], for_version=fallback)
    except Exception as err:  # noqa: BLE001
        log(f"could not schedule the configuration from {recovery['backup']} for {fallback}: {err}")
        return False
    log(f"the configuration from before the switch to {failed} comes back from {recovery['backup']}")
    return True


def apply_config_changes(state: dict, wanted: str, current: str | None) -> str:
    """After the install, before the boot: a restore or a clean start that
    belongs to a version change applies only when that version is the one
    booting (a failed install or a fallback boots another one).  Returns the
    version to boot: if a downgrade's restore or clean start did not happen,
    the version the configuration still belongs to."""
    from jsonio import ha_vkey

    for_version = backupkit.pending_for_version(CONFIG_DIR)
    if backupkit.pending(CONFIG_DIR) and for_version and for_version != wanted:
        backupkit.cancel_restore(CONFIG_DIR)
        log(f"restore scheduled for Home Assistant {for_version} dropped: {wanted} boots instead")
    made_on = backupkit.pending_ha_version(CONFIG_DIR)
    restore_parts = backupkit.pending_parts(CONFIG_DIR)
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

        state["last_restore"] = backupkit.apply_pending(CONFIG_DIR, log, record=record)
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
    reset = reset_storage_for_rebuild(wanted, restored)
    if not isinstance(change, dict):
        return wanted
    if change.get("to") != wanted:
        state.pop("change", None)  # that switch did not happen, nothing of it was applied
        return wanted
    mode = change.get("mode")
    # the change's own restore, with .storage (a restore scheduled by hand does not make a downgrade readable),
    # also when it was applied at a boot a power loss interrupted before ha.json recorded the change as applied
    last = state.get("last_restore") if isinstance(state.get("last_restore"), dict) else {}
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
    """A restore applied at an earlier boot whose outcome could not be written (a full disk)."""
    marker = os.path.join(CONFIG_DIR, backupkit.APPLIED_META)
    if not os.path.isfile(marker):
        return
    try:
        with open(marker, encoding="utf-8") as fh:
            meta = json.load(fh)
        at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(marker)))
    except (OSError, ValueError):
        meta, at = {}, time.strftime("%Y-%m-%dT%H:%M:%S")
    meta = meta if isinstance(meta, dict) else {}
    state["last_restore"] = {"at": at, "ok": True, "parts": meta.get("parts"), "error": "", "pre_restore": meta.get("pre_restore"),
                             "for_version": meta.get("for_version"), "note": "applied at an earlier boot; recorded late (the volume was full)"}
    if save_state(state):
        try:
            os.remove(marker)
        except OSError:
            pass


def main() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)  # before the first log() call
    clean_import_leftovers()
    state = load_state()
    merge_applied_restore(state)
    wanted = state.get("desired") or state.get("current")
    if not wanted:
        # Fresh volume: start from the newest stable HA, not the version the
        # image happened to be built with (HA_VERSION_LATEST=0 disables that).
        wanted = (latest_stable() if os.environ.get("HA_VERSION_LATEST", "1") != "0" else None) or DEFAULT_VERSION
        log(f"fresh volume: installing Home Assistant {wanted}")
    current = state.get("current")
    if not state.get("_corrupt"):
        state["last_error"] = ""

    # A venv can install fine and still fail to boot (a package HA dropped,
    # an incompatible integration).  run.py resets boot_failures once HA is
    # ready; after MAX_BOOT_FAILURES consecutive crashes on a version that is
    # not the previous one, go back to the previous venv and say why.
    failures = int(state.get("boot_failures") or 0)
    previous = state.get("previous")
    fallback_from = state.get("fallback_from")
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    fallback_to = previous or recovery.get("for")  # a stopped fallback keeps its target for the retry
    if failures >= MAX_BOOT_FAILURES and fallback_from and fallback_from != wanted:
        # We already fell back once and the previous version fails too:
        # the problem is not the HA version (port clash, broken manager,
        # corrupt .storage).  Stop flipping, keep the message honest.
        log(f"{wanted} fails to boot as well as {fallback_from}: not a Home Assistant version problem; retrying")
        state["last_error"] = f"both {fallback_from} and {wanted} crash at boot: not a HA version problem (see container log)"
        save_state(state)
    elif failures >= MAX_BOOT_FAILURES and fallback_to and fallback_to != wanted and venv_ok(fallback_to):
        if restore_after_failed_change(state, wanted, fallback_to):
            log(f"{wanted} failed to boot {failures} times; falling back to {fallback_to}")
            state["last_error"] = f"{wanted} crashed at boot {failures} times; rolled back to {fallback_to} (see container log)"
            state["fallback_from"] = wanted
            state["desired"] = wanted = fallback_to
        else:
            log(f"{wanted} failed to boot {failures} times, but its configuration cannot be brought back for {fallback_to}: staying on {wanted}")
            state["last_error"] = (f"{wanted} crashed at boot {failures} times; no fallback to {fallback_to}, because the configuration "
                                   "from before the switch could not be restored (see container log)")
        state["boot_failures"] = 0
        save_state(state)
    elif failures >= MAX_BOOT_FAILURES:
        log(f"{wanted} failed to boot {failures} times and there is nothing to fall back to; retrying")

    if not venv_ok(wanted) and not fits_this_python(wanted):
        other = latest_stable()
        py = ".".join(str(x) for x in sys.version_info[:3])
        if other and other != wanted:
            log(f"Home Assistant {wanted} does not support Python {py} (a newer image?): installing {other} instead")
            state["last_error"] = f"Home Assistant {wanted} does not support this image's Python {py}; {other} is installed instead"
            state["desired"] = wanted = other
    if not venv_ok(wanted):
        srv = start_status_server()
        ok = install(wanted)
        if srv:
            srv.shutdown()
        if not ok:
            fallback = current if current and venv_ok(current) else next(iter(reversed(installed_versions())), None)
            state["last_error"] = f"install of {wanted} failed; running {fallback}"
            state["desired"] = fallback
            if fallback is None:
                save_state(state)
                log("no working Home Assistant venv; exiting")
                sys.exit(1)
            wanted = fallback

    ensure_extra_requirements(wanted)
    wanted = apply_config_changes(state, wanted, current)

    if current and current != wanted and venv_ok(current) and failures < MAX_BOOT_FAILURES:
        # (a version that just crashed its way into a fallback is not a rollback target)
        state["previous"] = current
    if failures >= MAX_BOOT_FAILURES:
        # after an automatic fallback the venv that crashed is pruned below:
        # do not offer it as a rollback target
        state.pop("previous", None)
    # fallback_from is cleared by run.py once HA actually reaches STARTED
    state["current"] = wanted
    state.setdefault("desired", wanted)
    state["boot_failures"] = int(state.get("boot_failures") or 0) + 1  # run.py zeroes it when ready
    save_state(state)
    if not state.get("_corrupt"):
        prune({wanted, state.get("previous") or wanted} | ({state["recovery"]["for"]} if isinstance(state.get("recovery"), dict) and state["recovery"].get("for") else set()))

    # Stable path for humans and scripts (docker exec ... /config/venv-current/bin/python)
    link = os.path.join(CONFIG_DIR, "venv-current")
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(venv_dir(wanted), link)
    except OSError as err:
        log(f"venv-current symlink not updated: {err}")
    python = os.path.join(venv_dir(wanted), "bin", "python")
    os.environ["SETUP_PORT"] = str(PORT)  # HA http default port, read at import time
    log(f"starting Home Assistant {wanted} via {python}")
    os.execv(python, [python, "/app/run.py"])


if __name__ == "__main__":
    main()
