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
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


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


def save_state(state: dict) -> None:
    """Atomic: a torn ha.json would read as {} and silently reinstall the
    image default version (and prune the one that was running)."""
    write_json(HA_FILE, {k: v for k, v in state.items() if k != "_corrupt"})


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


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        try:
            with open(LOG_FILE, "rb") as fh:  # last 64 KB only, the file may be long
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 65536))
                tail = "\n".join(fh.read().decode("utf-8", errors="replace").splitlines()[-40:])
        except OSError:
            tail = ""
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
    storage = os.path.join(CONFIG_DIR, ".storage")
    shutil.rmtree(storage, ignore_errors=True)
    os.makedirs(storage, exist_ok=True)
    plan["stage"] = "import"
    write_json(REBUILD_FILE, plan)
    log(f"clean start for Home Assistant {plan.get('to')}: .storage emptied, the integration is rebuilt after the boot (backup {plan.get('backup')})")
    return True


def restore_after_failed_change(state: dict, failed: str, fallback: str) -> None:
    """The container falls back from ``failed`` to ``fallback``.  If the
    switch to ``failed`` got as far as booting it (with a restore, a clean
    start, or just its own storage migrations in keep mode), the
    configuration from before the switch comes back from its pre-change
    backup, which ``fallback`` can read."""
    change = state.pop("change", None)
    if not isinstance(change, dict) or change.get("to") != failed or not change.get("applied") or not change.get("backup"):
        return
    try:
        backupkit.schedule_restore(CONFIG_DIR, str(change["backup"]), ["storage"], for_version=fallback)
        log(f"the configuration from before the switch to {failed} comes back from {change['backup']}")
    except Exception as err:  # noqa: BLE001
        log(f"could not bring back the configuration from {change.get('backup')}: {err}")


def apply_config_changes(state: dict, wanted: str, current: str | None) -> str:
    """After the install, before the boot: a restore or a clean start that
    belongs to a version change applies only when that version is the one
    booting (a failed install or a fallback boots another one).  Returns the
    version to boot: if a downgrade's restore or clean start did not happen,
    the version the configuration still belongs to."""
    for_version = backupkit.pending_for_version(CONFIG_DIR)
    if backupkit.pending(CONFIG_DIR) and for_version and for_version != wanted:
        backupkit.cancel_restore(CONFIG_DIR)
        log(f"restore scheduled for Home Assistant {for_version} dropped: {wanted} boots instead")
    restored = False
    if backupkit.pending(CONFIG_DIR):
        # HA is not running here, so registries can be replaced safely.
        state["last_restore"] = backupkit.apply_pending(CONFIG_DIR, log)
        restored = True
    reset = reset_storage_for_rebuild(wanted, restored)
    change = state.get("change")
    if not isinstance(change, dict):
        return wanted
    if change.get("to") != wanted:
        state.pop("change", None)  # that switch did not happen, nothing of it was applied
        return wanted
    mode = change.get("mode")
    done = (mode == "restore" and restored and (state.get("last_restore") or {}).get("ok")) or (mode == "rebuild" and reset)
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


def main() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)  # before the first log() call
    clean_import_leftovers()
    state = load_state()
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
    if failures >= MAX_BOOT_FAILURES and fallback_from and fallback_from != wanted:
        # We already fell back once and the previous version fails too:
        # the problem is not the HA version (port clash, broken manager,
        # corrupt .storage).  Stop flipping, keep the message honest.
        log(f"{wanted} fails to boot as well as {fallback_from}: not a Home Assistant version problem; retrying")
        state["last_error"] = f"both {fallback_from} and {wanted} crash at boot: not a HA version problem (see container log)"
        save_state(state)
    elif failures >= MAX_BOOT_FAILURES and previous and previous != wanted and venv_ok(previous):
        log(f"{wanted} failed to boot {failures} times; falling back to {previous}")
        restore_after_failed_change(state, wanted, previous)
        state["last_error"] = f"{wanted} crashed at boot {failures} times; rolled back to {previous} (see container log)"
        state["fallback_from"] = wanted
        state["desired"] = wanted = previous
        state["boot_failures"] = 0
        save_state(state)
    elif failures >= MAX_BOOT_FAILURES:
        log(f"{wanted} failed to boot {failures} times and there is nothing to fall back to; retrying")

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
        prune({wanted, state.get("previous") or wanted})

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
