"""Preflight of an integration version before the switch: the release is
unpacked into a scratch directory, its requirements are resolved by pip in
dry-run mode against this venv (nothing is installed into it), every package
pip would take from a source archive is built into a temporary directory,
every patch is evaluated against the new code and against the requirement
versions the update would bring, a requirement known to be a wrapper over a
program or a shared library the image does not carry is reported as a warning,
so is one pip backtracked years behind what the requirement allows,
an import of a name Home Assistant has removed by the version this
container will run is reported as a warning too,
the manifest's dependencies are checked
against HA's loader, and the minimum Home Assistant version (hacs.json) is
compared with the target.  The report says what would change and whether
anything blocks the update.  Used by "Preflight" on the Config page and by
the environment builder.

Not a sandbox: resolving an sdist or a direct URL runs its build backend
(setup.py, PEP 517 metadata hooks), and the source build runs it in full.
That is third-party code, run by pip in this container with the manager's
rights, as the install would run it.  The integration's own code is only
parsed."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from homeassistant import loader
from homeassistant.const import __version__ as ha_version
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from jsonio import ha_vkey, tag_key

from . import patches
from .installer import _req_name, bad_requirement, read_capped

_LOGGER = logging.getLogger(__name__)
LOCK = asyncio.Lock()  # one pip resolution at a time (UI, builder, MQTT update)

PIP_TIMEOUT_S = 300
STDERR_TAIL_LINES = 12  # of a failed pip run, what the report carries for the UI to show verbatim
CACHE_S = 1800  # a preflight report stays good enough to gate a start for 30 min (same stored copy, same Home Assistant)
MAX_CHECK_BYTES = 5 * 1024 * 1024  # a .py file above this is a blocker, not parsed
MAX_REPORTS = 32  # a report is several kB and every gate key carries the copy's installed_at: within CACHE_S,
# reinstalls add keys faster than staleness retires them, so the sweep alone does not bound the dict
_REPORTS: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
GITHUB_API = "https://api.github.com/repos/{repo}"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"
PYPI_TIMEOUT_S = 20  # the lag check is an extra: it never makes the preflight wait longer than a GitHub call does
PYPI_MAX_BYTES = 6 * 1024 * 1024  # a project with thousands of files (aiohttp's index is 9 MB) is not read at all
MAX_PYPI_LOOKUPS = 12  # a manifest with forty requirements must not turn the preflight into forty round trips

# A resolution that fell far behind.  pip backtracks: when the newest release of a requirement needs
# something that cannot be installed here (a dependency with no wheel for this Python, so a compiler this
# image has not got), pip does not fail - it walks back through older releases until one resolves.  Where
# the requirement names no lower bound it can walk back years, to a release whose API the integration was
# never written against: the preflight is green and the integration breaks the first time it talks to a
# device.  (What pip walks back to depends on the day: the release that blocks the newest version may get a
# wheel, and then the same requirement resolves to the newest again.)  A silent, ancient resolution is worse than an
# honest refusal, so it is reported - as a warning, never a blocker: pip did produce an install that works,
# and an old release is occasionally what the requirement really wants.
#
# "Far behind" has to stay quiet about the many legitimate reasons a resolution is not the newest release,
# so two independent signals have to agree:
#
#   * an older release SERIES - a lower major, or the same major and a lower minor, than the newest release
#     that satisfies the requirement.  A patch-level lag (1.4.2 where 1.4.7 exists) is never reported: that
#     is ordinary, and being held on a patch release is normal.
#   * and at least RESOLUTION_LAG_DAYS between the two releases.  This is what a series gap alone cannot
#     do: a major published last week while the resolution is three months old says nothing about
#     backtracking, and a calendar-versioned package (2026.1.0) opens a new "major" every year without any
#     of them being stale.  Two years of lag is not a versioning scheme, it is a resolution that went
#     somewhere else.
#
# What is not compared at all:
#   * anything Home Assistant pins in package_constraints.txt, and the requirements that come from the
#     manifest's dependencies: HA's resolution is HA's decision, deliberate, and not this preflight's
#     business.  Only what the integration itself declares, and what that pulls in, is measured.
#   * releases this Python is excluded from by their own requires_python: when the newer releases dropped
#     this Python, pip taking an older one is the right answer, not a backtrack.
#   * pre-releases, unless the resolution is itself a pre-release - pip does not take them by default.
#   * a version PyPI does not list (a direct archive URL, a VCS checkout): there is nothing to compare it
#     with, and PyPI that cannot be reached says nothing rather than guessing.
# The newest that satisfies is measured against the requirement's own specifier, so an integration that
# caps a requirement on purpose ("foo<2") is compared with the newest foo 1.x, not with foo 3.0.
RESOLUTION_LAG_DAYS = 730


class StoredCopyUnusable(ValueError):
    """The stored copy start() would deploy is not an integration Home Assistant can load."""


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _run_pip(cmd: list[str]) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True, text=True, timeout=PIP_TIMEOUT_S), with pip in its own process group:
    a timeout kills the group, so also the build backends pip started (a compiler, meson, a setup.py that hangs),
    which a kill of pip alone left running and holding memory after the preflight had given up on them."""
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/tmp", start_new_session=True) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=PIP_TIMEOUT_S)
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()  # not communicate(): a backend that left the group could hold the pipes open
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _pip_dry_run(python: str, requirements: list[str], constraints: str | None) -> dict[str, Any]:
    """Blocking: what pip would install for ``requirements`` in this venv.
    ``--dry-run --report`` resolves everything and installs nothing, but it
    downloads the archives, and for a source archive or a direct URL it runs
    the package's build backend to read its metadata."""
    if not requirements:
        return {"ok": True, "install": [], "stderr": ""}
    if (why := next((w for w in map(bad_requirement, requirements) if w), None)):
        return {"ok": False, "install": [], "stderr": why}  # "--index-url ..." from a manifest is an option to pip, not a package
    cmd = [python, "-m", "pip", "install", "--dry-run", "--quiet", "--report", "-", *requirements]
    if constraints and os.path.isfile(constraints):
        cmd += ["-c", constraints]
    try:
        proc = _run_pip(cmd)
    except subprocess.TimeoutExpired:
        return {"ok": False, "install": [], "stderr": f"pip did not finish within {PIP_TIMEOUT_S}s"}
    if proc.returncode != 0:
        err = proc.stderr.strip()
        # The line that says why can sit far above pip's closing summary (scipy's meson prints the missing
        # compiler ~20 lines before "metadata-generation-failed"), so _pip_reason reads the whole output;
        # "stderr" stays the bounded tail because the UI renders it verbatim.
        return {"ok": False, "install": [], "stderr": "\n".join(err.splitlines()[-STDERR_TAIL_LINES:]), "stderr_full": err}
    try:
        report = json.loads(proc.stdout or "{}")
    except ValueError:
        return {"ok": False, "install": [], "stderr": "pip report was not JSON"}
    rows = []
    for item in report.get("install", []):
        meta = item.get("metadata", {})
        info = item.get("download_info") or {}
        url = str(info.get("url") or "")
        # a VCS checkout or a local directory is no archive to build from, and the project of that name on PyPI may be another one
        archive = bool(url) and "vcs_info" not in info and "dir_info" not in info  # dir_info is often {}
        rows.append({"name": meta.get("name"), "version": meta.get("version"), "url": url if archive else "",
                     "requested": bool(item.get("requested")), "requires_python": meta.get("requires_python"),
                     # no wheel for this Python / architecture: pip and uv build it at install time
                     "source_only": archive and not url.split("?", 1)[0].endswith(".whl")})
    return {"ok": True, "install": rows, "stderr": ""}


def _patch_after_update(text: str, new_versions: dict[str, str]) -> str:
    """What the '# applies-to:' header says once the update's requirement
    versions are installed: applies | skipped | n/a (no header) | unknown."""
    m = patches._APPLIES_RE.search(text)
    if not m:
        return "n/a"
    try:
        from packaging.requirements import Requirement

        r = Requirement(m.group(1))
    except Exception:  # noqa: BLE001
        return "unknown"
    ver = new_versions.get(r.name.lower().replace("_", "-"))
    if not ver:
        return "unknown"
    return "applies" if (not r.specifier or r.specifier.contains(ver, prereleases=True)) else "skipped"


def _build_reason(stderr: str) -> str:
    """Why a wheel did not build: the missing compiler or header when pip says so, not its closing summary."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.lower().startswith("error")]  # not the compiler invocation itself
    for needle in ("no such file or directory", "error: command", "compiler", "cargo", "rust"):
        hit = next((ln for ln in errors if needle in ln.lower()), None)
        if hit:
            return hit
    return next((ln for ln in reversed(lines) if "error" in ln.lower()), lines[-1] if lines else "build failed")


def _build_from_source(python: str, rows: list[dict[str, Any]], constraints: str | None) -> list[dict[str, Any]]:
    """Blocking: build every package pip would take from a source archive, the way the install will (its build
    backend runs in full; the wheel goes to a temporary directory, nothing is installed).  The image has no
    compiler: a pure-Python package builds, one with C code does not."""
    import tempfile

    out = []
    for row in rows:
        if not row.get("source_only") or not row.get("name"):
            continue
        with tempfile.TemporaryDirectory(prefix="hri-build-") as tmp:
            # the archive pip resolved (a direct URL requirement is not on the index under that name)
            cmd = [python, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", tmp, row.get("url") or f"{row['name']}=={row['version']}"]
            if constraints and os.path.isfile(constraints):
                cmd += ["-c", constraints]
            try:
                proc = _run_pip(cmd)
                ok, err = proc.returncode == 0, ""
                if not ok:
                    err = _build_reason(proc.stderr)
            except subprocess.TimeoutExpired:
                ok, err = False, f"not built within {PIP_TIMEOUT_S}s"
        out.append({"name": row["name"], "version": row["version"], "built": ok, "error": err[:300]})
    return out


def _pip_reason(stderr: str) -> str:
    """The line of a failed pip run that says why (a Python version guard, a missing compiler, a conflict),
    prefixed with the package when pip names it elsewhere; not the closing "see above" hint."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    pkg = None
    for ln in lines:
        m = re.search(r"Failed to build '([^']+)'", ln) or re.match(r"^╰─>\s*([A-Za-z0-9_.\-]+)\s*$", ln)
        if m:
            pkg = m.group(1)
    for needle in ("python version", "a different python", "unknown compiler", "no such file or directory: 'gcc'",
                   "no such file or directory: 'cc'", "cannot install", "conflict is caused by", "no matching distribution",
                   "could not find a version"):
        hit = next((ln for ln in lines if needle in ln.lower()), None)
        if hit:
            hit = re.sub(r"^(\S+:\d+:\d+:\s*)", "", hit)  # meson's file:line:col prefix
            return (f"{pkg}: {hit}" if pkg and pkg.lower() not in hit.lower() else hit)[:300]
    last = next((ln for ln in reversed(lines) if ln.lower().startswith("error") and "see above" not in ln.lower()), None)
    return ((f"{pkg}: " if pkg and last and pkg.lower() not in last.lower() else "") + (last or (lines[-1] if lines else "pip failed")))[:300]


# folders a Home Assistant integration ships but HA never imports: code there that does not compile is not a blocker
_NOT_LOADED_DIRS = frozenset({"tests", "test", "scripts", "tools", "docs", "examples"})


# modules the standard library dropped (PEP 594 in 3.13, distutils/imp/asyncore in 3.12, ...)
_REMOVED_STDLIB = frozenset({
    "aifc", "asynchat", "asyncore", "audioop", "cgi", "cgitb", "chunk", "crypt", "distutils", "imghdr", "imp", "lib2to3",
    "mailcap", "msilib", "nis", "nntplib", "ossaudiodev", "pipes", "smtpd", "sndhdr", "spwd", "sunau", "telnetlib", "tkinter.tix",
    "uu", "xdrlib",
})


# a package that puts a removed module back is either named like it (telnetlib) or belongs to one of the
# families that exist for exactly that: standard-imghdr and the rest of PEP 594, legacy-cgi
_SHIM_PREFIXES = ("standard-", "legacy-")


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _removed_import_module(entry: str) -> str:
    """The module out of a `_code_checks` row ("__init__.py:2 imports imp")."""
    return entry.rsplit(" imports ", 1)[-1]


def _may_provide(module: str, distributions: set[str]) -> bool:
    """Whether any of ``distributions`` could ship ``module``, judged by name alone (pip's report
    does not say which modules a package installs)."""
    want = _canon(module.split(".")[0])
    return any(d == want or (d.startswith(_SHIM_PREFIXES) and d.split("-", 1)[1] == want) for d in distributions)


# Names Home Assistant itself removed.  The stdlib table above catches what the interpreter dropped; this one
# catches what the core dropped, which is the far more common way a third-party integration stops loading after
# a core update: a hard ImportError at setup, found from the traceback rather than before the install.
#
# One line per name: module -> {symbol: (first Home Assistant version without it, its replacement or "")}.
# The version is the earliest release the name was verified missing from, read out of the published wheel
# (homeassistant-<ver>-py3-none-any.whl), never out of a release note.  The 2026.9 rows say 2026.9.3 because
# that is the wheel they were diffed against: a target of 2026.9.0 to 2026.9.2 stays quiet rather than the
# table guessing at a patch release nobody looked at.  A name is only reported when its version is at or below
# the Home Assistant the release is checked for, so the table may name removals no operator has reached yet.
# To extend it, add a line - and check the wheel, the way every line here was checked.
#
# Source: the 2026.5.0 -> 2026.9.3 wheel diff (F3 of the breaking-change survey), re-verified against the
# 2026.9.3 in this image, which is also where each replacement comes from: helpers.target really does carry
# async_extract_referenced_entity_ids and SelectedEntities, and really does not carry ServiceTargetSelector.
# Only imports are seen, so an attribute Home Assistant removed from a class that stayed
# (VacuumEntityFeature.BATTERY, 2026.9) cannot be listed here.
_REMOVED_HA_SYMBOLS: dict[str, dict[str, tuple[str, str]]] = {
    "homeassistant.const": {
        "CLOUD_NEVER_EXPOSED_ENTITIES": ("2026.6.0", ""),
    },
    "homeassistant.helpers.trigger": {
        "async_track_same_state": ("2026.7.0", ""),
        "TRIGGER_DISABLED_TRIGGERS": ("2026.7.0", ""),
    },
    "homeassistant.helpers.condition": {
        "CONDITION_DISABLED_CONDITIONS": ("2026.7.0", ""),
    },
    # the ATTR_* re-exports became a StrEnum in homeassistant.const; the wheel diff elided the tail of the
    # list, so only the three names it printed are here
    "homeassistant.helpers.entity": {
        "ATTR_ASSUMED_STATE": ("2026.7.0", "homeassistant.const.EntityStateAttribute.ASSUMED_STATE"),
        "ATTR_ATTRIBUTION": ("2026.7.0", "homeassistant.const.EntityStateAttribute.ATTRIBUTION"),
        "ATTR_DEVICE_CLASS": ("2026.7.0", "homeassistant.const.EntityStateAttribute.DEVICE_CLASS"),
    },
    "homeassistant.helpers.entity_registry": {
        "STATE_UNKNOWN": ("2026.7.0", "homeassistant.const.STATE_UNKNOWN"),
    },
    "homeassistant.helpers.service": {
        "async_extract_referenced_entity_ids": ("2026.8.0", "homeassistant.helpers.target.async_extract_referenced_entity_ids"),
        "SelectedEntities": ("2026.8.0", "homeassistant.helpers.target.SelectedEntities"),
        "ServiceTargetSelector": ("2026.8.0", ""),
    },
    # http kept the package and moved the names into http/server.py and http/config.py; three went for good
    "homeassistant.components.http": {
        "HomeAssistantApplication": ("2026.8.0", "homeassistant.components.http.server.HomeAssistantApplication"),
        "MAX_CLIENT_SIZE": ("2026.8.0", "homeassistant.components.http.server.MAX_CLIENT_SIZE"),
        "ConfData": ("2026.8.0", "homeassistant.components.http.config.ConfData"),
        "SERVER_PORT": ("2026.8.0", "homeassistant.const.SERVER_PORT"),
        "HomeAssistantTCPSite": ("2026.8.0", ""),
        "async_get_last_config": ("2026.8.0", ""),
        "start_http_server_and_save_config": ("2026.8.0", ""),
    },
    # the CONCENTRATION_* re-exports went at the same time; the wheel diff elided their names, so only the one
    # it printed is listed
    "homeassistant.components.sensor.const": {
        "PERCENTAGE": ("2026.8.0", "homeassistant.const.PERCENTAGE"),
    },
    "homeassistant.components.number.const": {
        "PERCENTAGE": ("2026.8.0", "homeassistant.const.PERCENTAGE"),
    },
    "homeassistant.helpers.device_registry": {
        "DEVICE_INFO_KEYS": ("2026.8.0", ""),
        "LOW_PRIO_CONFIG_ENTRY_DOMAINS": ("2026.8.0", ""),
        "DEVICE_INFO_TYPES": ("2026.9.3", ""),
    },
    "homeassistant.helpers.config_validation": {
        "voluptuous_serialize": ("2026.9.3", "the voluptuous-serialize package, or homeassistant.helpers.config_validation.to_field_list"),
    },
    "homeassistant.helpers.data_entry_flow": {
        "voluptuous_serialize": ("2026.9.3", "the voluptuous-serialize package"),
    },
    "homeassistant.runner": {
        "HassEventLoopPolicy": ("2026.9.3", "homeassistant.runner.create_event_loop"),
    },
    "homeassistant.components.vacuum": {
        "ATTR_BATTERY_LEVEL": ("2026.9.3", ""),
    },
}


# Packages that pip installs perfectly but that are only a wrapper over something the image must already
# carry: a program on PATH or a shared library.  pip cannot see this, so the integration starts and fails
# the moment it uses that part (PyTurboJPEG did, which is why the image carries libturbojpeg since 0.16.0).
# Not a blocker: an operator may use only the parts of the integration that do without it.
#
# One line per package: distribution name (normalised by _canon, so case, "-", "_" and "." do not matter)
# -> (programs on PATH, shared libraries).  Either side may be empty.  To extend it, add a line.
_SYSTEM_DEPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ha-ffmpeg": (("ffmpeg",), ()),
    "ffmpeg-python": (("ffmpeg",), ()),
    "pytesseract": (("tesseract",), ()),
    "speechrecognition": (("flac",), ()),
    "pydub": (("ffmpeg",), ()),  # shells out to ffmpeg/ffprobe (pydub.utils.get_encoder_name); imports fine, decodes nothing without it
    "pyturbojpeg": ((), ("libturbojpeg.so.0",)),
    "pyaudio": ((), ("libportaudio.so.2",)),
    "sounddevice": ((), ("libportaudio.so.2",)),
    "python-magic": ((), ("libmagic.so.1",)),
    "pyusb": ((), ("libusb-1.0.so.0",)),
    "libpcap": ((), ("libpcap.so.0.8",)),
    "pyudev": ((), ("libudev.so.1",)),
    "python-vlc": ((), ("libvlc.so.5",)),
    "python-mpv": ((), ("libmpv.so.2",)),
    "opencv-python": ((), ("libGL.so.1",)),
    "opencv-contrib-python": ((), ("libGL.so.1",)),
}

# What installs each of those programs and libraries on the image's Debian (trixie): the value the warning
# tells the operator to put in HRI_APT_PACKAGES.  Every name was looked up in the image's own apt index -
# libmagic1 and libpcap0.8 exist there only as virtual packages, so the real ones (…t64) are named.  A
# program or library whose package is not obvious simply has no line here and the warning says nothing
# about apt rather than naming a package that may not exist.
_DEBIAN_PACKAGE: dict[str, str] = {
    "ffmpeg": "ffmpeg",
    "tesseract": "tesseract-ocr",
    "flac": "flac",
    "libturbojpeg.so.0": "libturbojpeg0",
    "libportaudio.so.2": "libportaudio2",
    "libmagic.so.1": "libmagic1t64",
    "libusb-1.0.so.0": "libusb-1.0-0",
    "libpcap.so.0.8": "libpcap0.8t64",
    "libudev.so.1": "libudev1",
    "libvlc.so.5": "libvlc5",
    "libmpv.so.2": "libmpv2",
    "libGL.so.1": "libgl1",
}

# Requirements that install, import and even work, but only against the host's Bluetooth stack: they talk
# to BlueZ ("org.bluez") over the system D-Bus, and dbus-fast opens /run/dbus/system_bus_socket itself.
# Verified one by one in the published wheels.  This is not a missing package - no Debian package puts an
# adapter inside a container - so it is a different warning from _SYSTEM_DEPS: the container needs the
# host's D-Bus socket mounted, the host's network namespace and the capabilities, which is a compose-file
# matter and is documented under "Hardware access".  When the socket is there, the operator has already
# done that work and the warning would be noise.
_BLUETOOTH_DEPS = frozenset({"bleak", "bluetooth-adapters", "dbus-fast", "habluetooth"})
_DBUS_SOCKETS = ("/run/dbus/system_bus_socket", "/var/run/dbus/system_bus_socket")

# the image is what it is for the life of the process: each program and library is looked for once
_PRESENT: dict[str, bool] = {}
_LIB_DIRS = ("/usr/lib", "/lib", "/usr/local/lib", "/usr/lib64")


def _library_present(soname: str) -> bool:
    """Blocking: whether the shared library is in this image.  The plain and the multiarch library
    directories (/usr/lib/x86_64-linux-gnu, /usr/lib/aarch64-linux-gnu), then ctypes' own lookup,
    which asks ldconfig's cache and so also finds one installed somewhere else."""
    import ctypes.util

    for base in _LIB_DIRS:
        if os.path.isfile(os.path.join(base, soname)):
            return True
        try:
            subdirs = [d for d in os.listdir(base) if d.endswith("-linux-gnu")]
        except OSError:
            continue
        if any(os.path.isfile(os.path.join(base, d, soname)) for d in subdirs):
            return True
    stem = re.sub(r"^lib", "", soname).split(".so", 1)[0]  # libturbojpeg.so.0 -> turbojpeg
    return bool(stem) and bool(ctypes.util.find_library(stem))


def _host_dbus_present() -> bool:
    """Whether the host's D-Bus system socket is mounted into this container (cached like the rest)."""
    if (hit := _PRESENT.get("dbus:system")) is None:
        hit = _PRESENT["dbus:system"] = any(os.path.exists(p) for p in _DBUS_SOCKETS)
    return hit


def _present(kind: str, name: str) -> bool:
    if (hit := _PRESENT.get(f"{kind}:{name}")) is None:
        hit = _PRESENT[f"{kind}:{name}"] = (shutil.which(name) is not None) if kind == "bin" else _library_present(name)
    return hit


def _system_dep_warnings(requirements: list[str]) -> list[str]:
    """Blocking (it looks at the filesystem): one line per requirement of _SYSTEM_DEPS whose program or
    library this container does not have, and one per requirement of _BLUETOOTH_DEPS while the host's
    D-Bus socket is not mounted.  A package whose system dependency is there says nothing."""
    out: list[str] = []
    seen: dict[str, str] = {}  # the same package twice (a manifest requirement pip also resolved) warns once
    for req in requirements:
        name = _req_name(req) if req else ""
        seen.setdefault(_canon(name), name)
    for key, name in seen.items():
        if entry := _SYSTEM_DEPS.get(key):
            missing: list[str] = []
            packages: list[str] = []
            for kind, label, wanted in (("bin", "the program", entry[0]), ("lib", "the library", entry[1])):
                for dep in wanted:
                    if _present(kind, dep):
                        continue
                    missing.append(f"{label} {dep}")
                    if (pkg := _DEBIAN_PACKAGE.get(dep)) and pkg not in packages:
                        packages.append(pkg)
            if missing:
                text = (f"{name} is a wrapper over {' and '.join(missing)}, which this image does not have: "
                        "it installs, but whatever the integration does with it fails at runtime")
                if packages:
                    text += (f". Set HRI_APT_PACKAGES={' '.join(packages)} (next to what it already names) "
                             "and recreate the container")
                out.append(text)
        if key in _BLUETOOTH_DEPS and not _host_dbus_present():
            out.append(f"{name} needs the host's Bluetooth stack, which a container cannot provide by itself: "
                       "a running bluetoothd reached over the host's D-Bus system socket, the host's network "
                       "namespace and the capabilities that go with it. No package installs that - the adapter "
                       "is on the host. It installs and imports, and finds no adapter. See the README, "
                       "\"Hardware access\"")
    return out


def _pypi_releases(raw: bytes) -> dict[str, tuple[str, str]]:
    """Blocking: {version: (release day, requires_python)} out of PyPI's JSON for one project.  A version
    whose files are all yanked, or that has no file left at all, is not a version pip can take."""
    out: dict[str, tuple[str, str]] = {}
    for ver, files in ((json.loads(raw) or {}).get("releases") or {}).items():
        live = [f for f in (files or []) if isinstance(f, dict) and not f.get("yanked")]
        if not live:
            continue
        day = min(str(f.get("upload_time_iso_8601") or f.get("upload_time") or "") for f in live)[:10]
        rpy = next((str(f.get("requires_python") or "") for f in live if f.get("requires_python")), "")
        out[str(ver)] = (day, rpy)
    return out


async def _pypi_index(hass: HomeAssistant, name: str) -> dict[str, tuple[str, str]] | None:
    """What PyPI lists for ``name``, or None when it cannot be read.  Nothing here is a blocker and nothing
    here is a guess: an unreachable, oversized or unreadable index simply says nothing."""
    import urllib.parse

    try:
        import aiohttp

        url = PYPI_JSON.format(name=urllib.parse.quote(name, safe=""))
        async with async_get_clientsession(hass).get(url, timeout=aiohttp.ClientTimeout(total=PYPI_TIMEOUT_S)) as resp:
            if resp.status != 200:
                return None
            raw = await read_capped(resp, f"the PyPI index of {name}", PYPI_MAX_BYTES)
        return await hass.async_add_executor_job(_pypi_releases, raw)
    except Exception as err:  # noqa: BLE001 - offline, PyPI down, an index too big to read: no warning, no failure
        _LOGGER.debug("PyPI index of %s not read: %s", name, err)
        return None


def _days_between(older: str, newer: str) -> int | None:
    from datetime import date

    try:
        return (date.fromisoformat(newer) - date.fromisoformat(older)).days
    except ValueError:
        return None


def _resolution_lag(name: str, resolved: str, req_text: str | None, index: dict[str, tuple[str, str]],
                    python: str) -> str | None:
    """The warning for one resolved package, or None (see RESOLUTION_LAG_DAYS for the rule).  Pure:
    ``index`` is what PyPI lists, ``req_text`` the integration's own requirement when it has one (a
    package pip only pulled in has none), ``python`` the image's version ("3.14.0")."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    def _ver(text: str) -> Any:
        try:
            return Version(text)
        except InvalidVersion:
            return None

    got = _ver(resolved)
    if got is None:
        return None
    want = SpecifierSet("")
    if req_text:
        try:
            from packaging.requirements import Requirement

            want = Requirement(req_text).specifier
        except Exception:  # noqa: BLE001
            return None
    here = _ver(python)
    got_day = ""
    releases: list[tuple[Any, str, str]] = []
    for text, (day, rpy) in index.items():
        ver = _ver(text)
        if ver is None:
            continue
        if ver == got:
            got_day = day
        if ver.is_prerelease and not got.is_prerelease:
            continue
        if not want.contains(ver, prereleases=got.is_prerelease):
            continue
        if rpy and here is not None:
            try:
                if not SpecifierSet(rpy).contains(here, prereleases=True):
                    continue
            except InvalidSpecifier:
                pass
        releases.append((ver, text, day))
    if not got_day or not releases:
        return None  # PyPI does not list what pip resolved (a direct URL), or nothing there satisfies
    newest, newest_text, newest_day = max(releases, key=lambda row: row[0])
    if (newest.major, newest.minor) <= (got.major, got.minor):
        return None
    days = _days_between(got_day, newest_day)
    if days is None or days < RESOLUTION_LAG_DAYS:
        return None
    behind = sum(1 for ver, _, _ in releases if ver > got)
    asked = f"the requirement {req_text!r}" if req_text else f"this Python ({python})"
    return (f"pip resolved {name} {resolved}, released {got_day}: {behind} release{'' if behind == 1 else 's'} "
            f"and {days / 365.25:.1f} years behind {newest_text} ({newest_day}), the newest that satisfies "
            f"{asked}. Nothing asks for the old release, so something else in the resolved set forced it down "
            "- the usual cause is a dependency of the newer releases that has no wheel for this Python. It "
            f"installs and resolves cleanly, and the integration then runs against {name} as it was in "
            f"{got_day[:4]}")


def _constraint_names(path: str) -> set[str]:
    """Blocking: the distributions Home Assistant pins in its package_constraints.txt.  Where pip lands for
    one of them is HA's decision and deliberate, never a backtrack this preflight should report."""
    out: set[str] = set()
    for line in _read_text(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            out.add(_canon(_req_name(line)))
    return out


async def _resolution_warnings(hass: HomeAssistant, installer, own_reqs: list[str], pip_rows: list[dict[str, Any]],
                               dep_reqs: list[str]) -> list[str]:
    """One line per package of the resolved set pip settled far behind on.  The manifest's own requirements
    are looked up first, because the number of PyPI round trips is capped."""
    own: dict[str, str] = {}
    for req in own_reqs:
        if req and (name := _req_name(req)):
            own.setdefault(_canon(name), req)
    if not own:
        return []  # a version that declares no requirements of its own has nothing here that is its doing
    pinned = await hass.async_add_executor_job(_constraint_names, installer.constraints)
    from_ha = {_canon(_req_name(req)) for req in dep_reqs if req} - set(own)
    rows = [row for row in pip_rows if row.get("name") and row.get("version")
            and _canon(str(row["name"])) not in pinned and _canon(str(row["name"])) not in from_ha]
    rows.sort(key=lambda row: _canon(str(row["name"])) not in own)  # stable: the manifest's own first, pip's order after
    python = ".".join(str(x) for x in sys.version_info[:3])
    out: list[str] = []
    for row in rows[:MAX_PYPI_LOOKUPS]:
        name = str(row["name"])
        index = await _pypi_index(hass, name)
        if not index:
            continue
        if (hit := _resolution_lag(name, str(row["version"]), own.get(_canon(name)), index, python)):
            out.append(hit)
    return out


def _catches_import_error(handler: Any) -> bool:
    import ast

    names = []
    if handler.type is None:
        return True
    for node in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]):
        if isinstance(node, ast.Name):
            names.append(node.id)
    return any(n in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException") for n in names)


def _guarded_imports(tree: Any) -> set[int]:
    """The ids of the nodes under a `try:` whose `except` catches an ImportError: an import there is a
    fallback the integration already handles, not something to report."""
    import ast

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_catches_import_error(h) for h in node.handlers):
            for stmt in node.body:
                guarded.update(id(n) for n in ast.walk(stmt))
    return guarded


def _source_files(component_dir: str):
    """(absolute path, path relative to the integration) of every .py file Home Assistant would load, in a
    stable order: the folders it never imports are skipped at the top level, a package named like one deeper
    in the tree is checked like any other."""
    for root, dirs, files in os.walk(component_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not (root == component_dir and d in _NOT_LOADED_DIRS))
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(root, name)
                yield path, os.path.relpath(path, component_dir)


def _code_checks(component_dir: str) -> tuple[list[str], list[str]]:
    """Blocking: (syntax errors, imports of removed standard modules) of the integration's code, with this
    interpreter (the image's Python).  Nothing is imported or run."""
    import ast
    import importlib.util

    errors: list[str] = []
    removed: list[str] = []
    for path, rel in _source_files(component_dir):
        try:
            size = os.path.getsize(path)
            if size > MAX_CHECK_BYTES:
                errors.append(f"{rel}: too large to check ({size} bytes)")
                continue
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=rel)
            compile(source, rel, "exec", dont_inherit=True)  # what ast accepts but the compiler refuses
        except SyntaxError as err:
            errors.append(f"{rel}:{err.lineno}: {err.msg}")
            continue
        except (MemoryError, RecursionError) as err:  # "Parser stack overflowed": Python refuses to load it too
            errors.append(f"{rel}: too complex to parse ({type(err).__name__})")
            continue
        except (OSError, UnicodeDecodeError, ValueError) as err:
            errors.append(f"{rel}: {err}")
            continue
        guarded = _guarded_imports(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                hit = module if module in _REMOVED_STDLIB else top if top in _REMOVED_STDLIB else None
                if hit and id(node) not in guarded and importlib.util.find_spec(top) is None:
                    removed.append(f"{rel}:{node.lineno} imports {hit}")
    return errors, removed


def _ha_symbol_checks(component_dir: str, target: str) -> list[str]:
    """Blocking: one line per import of a name Home Assistant ``target`` no longer has ("sensor.py:4 imports
    homeassistant.helpers.service.async_extract_referenced_entity_ids, removed in 2026.8.0, now
    homeassistant.helpers.target.async_extract_referenced_entity_ids").  A name a newer Home Assistant than the
    target removed is not reported: on the version this container will run, the import still works.  Nothing is
    imported or run, and a file that does not parse says nothing here - _code_checks already reports it."""
    import ast

    hits: list[str] = []
    for path, rel in _source_files(component_dir):
        try:
            if os.path.getsize(path) > MAX_CHECK_BYTES:
                continue
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError, MemoryError, RecursionError):
            continue
        guarded = _guarded_imports(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 0 or not node.module or id(node) in guarded:
                continue
            for alias in node.names:
                removed = _REMOVED_HA_SYMBOLS.get(node.module, {}).get(alias.name)
                if not removed or ha_vkey(removed[0]) > ha_vkey(target):
                    continue
                hits.append(f"{rel}:{node.lineno} imports {node.module}.{alias.name}, removed in {removed[0]}"
                            + (f", now {removed[1]}" if removed[1] else ""))
    return hits


def _config_flow_version(component_dir: str) -> int | None:
    """Blocking: VERSION of the ConfigFlow class in <component>/config_flow.py, read with ast (never imported)."""
    import ast

    path = os.path.join(component_dir, "config_flow.py")
    try:
        if os.path.getsize(path) > MAX_CHECK_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not any(k.arg == "domain" for k in node.keywords):
            continue
        for item in node.body:
            if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in item.targets):
                if isinstance(item.value, ast.Constant) and isinstance(item.value.value, int):
                    return item.value.value
        return 1  # a config flow without VERSION is version 1
    return None


def remember(domain: str, ref: str, report: dict[str, Any], target_ha: str | None = None) -> None:
    now = time.monotonic()
    for key in [k for k, (at, _) in _REPORTS.items() if now - at >= CACHE_S]:
        del _REPORTS[key]
    _REPORTS[(domain, ref, target_ha or ha_version)] = (now, report)
    while len(_REPORTS) > MAX_REPORTS:
        del _REPORTS[min(_REPORTS, key=lambda k: _REPORTS[k][0])]


def recent(domain: str, ref: str) -> dict[str, Any] | None:
    hit = _REPORTS.get((domain, ref, ha_version))
    return hit[1] if hit and time.monotonic() - hit[0] < CACHE_S else None


async def gate(hass: HomeAssistant, installer, domain: str, tag: str | None) -> dict[str, Any]:
    """Whether a start of (domain, tag) from the UI or the API should wait for a confirmation:
    {"blocked", "report", "skipped"}.  Starting the version that already runs (or ran last) or a release
    without a GitHub repository is not gated; a preflight that cannot run (GitHub is unreachable, for
    example) does not block either: the smoke test still guards the start.  A dev build is gated like any
    other version: it is checked against the stored copy, never against GitHub, so there is nothing about
    an uploaded tree the check cannot read - and a syntax error or a requirement with no wheel in one costs
    two restarts and a rollback to find out otherwise.  Its report is keyed on the copy's installed_at, so
    the next upload is checked again and starting the same copy twice runs one check.
    A stored copy that is no integration at all is the exception, and blocks; a version that is not in the
    store (or whose directory is gone) is not gated at all: start() refuses it plainly, forced or not.
    The check reads the stored copy start() deploys, not what the ref names on GitHub now (a moved tag or
    branch, a commit installed under a name).  No busy flag while it runs (pip can take minutes, and an
    install or a backup must not be refused for that): LOCK queues concurrent gates, start() refuses on its
    own while another action runs, and a reinstall of the copy during the check blocks the start."""
    rec = installer.state.installed.get(domain) or {}
    versions = rec.get("versions") or {}
    target = tag or rec.get("running_tag") or (max(versions, key=tag_key) if versions else None)
    if not target or target == rec.get("running_tag"):
        return {"blocked": False, "report": None, "skipped": "same version as the one deployed"}
    if target not in versions:
        # nothing to check and nothing to confirm: start() refuses a version that is not in the store, forced or not
        return {"blocked": False, "report": None, "skipped": None}
    spec = installer.spec(domain) or {}
    if not spec.get("repo"):
        return {"blocked": False, "report": None, "skipped": "no GitHub repository known"}
    def stamp() -> str:
        versions_now = (installer.state.installed.get(domain) or {}).get("versions") or {}
        return str((versions_now.get(target) or {}).get("installed_at") or "")

    before = stamp()
    key = f"stored:{target}\n{before}"  # a reinstalled copy (same tag, new installed_at) is checked again
    report = recent(domain, key)
    if report is None:
        try:
            async with LOCK:
                report = await run(hass, installer, domain, target, source_dir=installer._version_dir(domain, target))
        except StoredCopyUnusable as err:
            if not await hass.async_add_executor_job(os.path.isdir, installer._version_dir(domain, target)):
                return {"blocked": False, "report": None, "skipped": None}  # recorded, but its directory is gone: start() refuses it
            # not a transient failure: deploying this copy replaces the live integration with a tree Home Assistant
            # cannot load ("Integration not found"), and only the smoke test undoes it, two restarts later
            return {"blocked": True, "report": {"domain": domain, "ref": target, "ok": False, "blockers": [str(err)],
                                                "warnings": []}, "skipped": None}
        except Exception as err:  # noqa: BLE001
            return {"blocked": False, "report": None, "skipped": f"preflight could not run: {err}"}
        if stamp() != before:
            why = f"{domain} {target} was installed again while its preflight ran: start it again to check the new copy"
            return {"blocked": True, "report": {**report, "ok": False, "blockers": [why]}, "skipped": None}
        remember(domain, key, report)
    return {"blocked": not report.get("ok", True), "report": report, "skipped": None}


async def run(hass: HomeAssistant, installer, domain: str, ref: str, target_ha: str | None = None,
              archive_ref: str | None = None, source_dir: str | None = None, repo: str | None = None) -> dict[str, Any]:
    """``archive_ref``: the commit to download (``ref`` names it in the report); ``source_dir``: check that
    stored copy instead of downloading anything (the start gate); ``repo``: the repository to read, for a
    check of one that is not in the registry (the environment builder registers nothing before Prepare)."""
    t0 = time.monotonic()
    repo = repo or (installer.spec(domain) or {}).get("repo")
    if not repo:
        raise ValueError(f"{domain}: no GitHub repository known (registry)")
    blockers: list[str] = []
    warnings: list[str] = []

    # 1. the release: the stored copy, or the archive (by its commit when known) into a scratch directory
    if source_dir:
        manifest = await hass.async_add_executor_job(installer._manifest_at, source_dir)
        if not manifest or manifest.get("domain") != domain:
            raise StoredCopyUnusable(f"{domain} {ref}: the stored copy has no manifest.json for {domain}")
        scratch, blob, min_ha = source_dir, b"", installer.min_ha_of(domain, ref)
    else:
        fetch = archive_ref or ref
        async with async_get_clientsession(hass).get(GITHUB_API.format(repo=repo) + f"/zipball/{fetch}", headers=installer.settings.github_headers()) as resp:
            if resp.status != 200:
                raise ValueError(f"{repo}@{fetch}: GitHub answered {resp.status}")
            blob = await read_capped(resp, f"{repo}@{fetch}")
        scratch, manifest = os.path.join(installer.versions_dir, domain, f".preflight-{int(time.time())}"), {}
        min_ha = await hass.async_add_executor_job(installer._hacs_min_ha, blob)  # hacs.json of the same commit as the code
    try:
        if not source_dir:
            manifest = await hass.async_add_executor_job(installer._unpack, blob, domain, scratch)
        new_reqs = list(manifest.get("requirements", []))

        # 2. minimum Home Assistant version (hacs.json) vs the target
        target = target_ha or ha_version
        if min_ha and ha_vkey(str(min_ha)) > ha_vkey(target):
            blockers.append(f"needs Home Assistant >= {min_ha}, target is {target}")

        # 3. dependencies: every domain the manifest names must be loadable here
        deps = list(manifest.get("dependencies", [])) + list(manifest.get("after_dependencies", []))
        dep_rows = []
        dep_reqs: list[str] = []
        for dep in deps:
            try:
                integ = await loader.async_get_integration(hass, dep)
                dep_rows.append({"domain": dep, "found": True, "requirements": list(integ.requirements or [])})
                dep_reqs.extend(integ.requirements or [])
            except loader.IntegrationNotFound:
                dep_rows.append({"domain": dep, "found": False, "requirements": []})
                if dep in manifest.get("dependencies", []):
                    blockers.append(f"dependency '{dep}' is not available in this Home Assistant")
                else:
                    warnings.append(f"after_dependency '{dep}' is not available here")

        # 4. requirements: pip dry-run against this venv
        all_reqs = list(dict.fromkeys(new_reqs + dep_reqs))
        refused = [r for r in all_reqs if bad_requirement(r)]
        blockers += [str(bad_requirement(r)) for r in refused]
        all_reqs = [r for r in all_reqs if r not in refused]
        installed_now = {_req_name(req): ver for req, ver in (await hass.async_add_executor_job(installer._requirement_versions, all_reqs)).items()}
        pip = await hass.async_add_executor_job(_pip_dry_run, sys.executable, all_reqs, installer.constraints)
        if not pip["ok"]:
            blockers.append("requirements cannot be resolved: " + _pip_reason(pip.get("stderr_full") or pip["stderr"]))
        py = ".".join(str(x) for x in sys.version_info[:3])
        import platform

        source_builds = await hass.async_add_executor_job(_build_from_source, sys.executable, pip["install"], installer.constraints) if pip["ok"] else []
        for b in source_builds:
            if not b["built"]:
                blockers.append(f"{b['name']} {b['version']} has no wheel for Python {py} on {platform.machine()} and cannot be built here "
                                f"(the image has no compiler): {b['error']}")
        # 4b. a requirement that installs but only wraps a program or library the image does not carry
        # (the resolved set too: the package that needs it is often pulled in by another one)
        warnings += await hass.async_add_executor_job(
            _system_dep_warnings, [*all_reqs, *(str(r.get("name") or "") for r in pip["install"])])
        # 4c. where pip's backtracking landed: a resolution years behind installs cleanly and breaks at runtime
        if pip["ok"]:
            warnings += await _resolution_warnings(hass, installer, [r for r in new_reqs if r not in refused],
                                                   pip["install"], dep_reqs)

        new_versions = {str(r["name"]).lower().replace("_", "-"): str(r["version"]) for r in pip["install"] if r.get("name")}
        for name, ver in installed_now.items():
            key = name.lower().replace("_", "-")
            new_versions.setdefault(key, ver or "")
        req_rows = []
        for req in all_reqs:
            name = _req_name(req)
            key = name.lower().replace("_", "-")
            change = next((r for r in pip["install"] if str(r.get("name", "")).lower().replace("_", "-") == key), None)
            req_rows.append({"requirement": req, "installed": installed_now.get(name), "after": change["version"] if change else installed_now.get(name),
                             "action": ("install" if change and not installed_now.get(name) else "upgrade" if change else "unchanged"),
                             "from_dependency": req in dep_reqs and req not in new_reqs})
        extra = [r for r in pip["install"] if str(r.get("name", "")).lower().replace("_", "-") not in
                 {_req_name(q).lower().replace("_", "-") for q in all_reqs}]
        if target != ha_version:
            warnings.append(f"requirements were resolved against Home Assistant {ha_version} (this venv); the {target} venv is built at the restart and the requirements reinstalled there")

        # 5. patches against the new code and the new requirement versions
        rows = await hass.async_add_executor_job(
            patches.status, installer.config_dir, domain, installer.site_packages_for(domain), scratch, ref)
        patch_rows = []
        for row in rows:
            path = patches.patch_path(installer.config_dir, domain, row["name"])
            text = await hass.async_add_executor_job(_read_text, path)
            after = _patch_after_update(text, new_versions)
            patch_rows.append({**row, "after_update": after})
            st = str(row.get("status", ""))
            if st.startswith(("error", "failed")):
                blockers.append(f"patch {row['name']}: {st}")
            elif st == "not applicable" and after != "skipped":
                warnings.append(f"patch {row['name']} does not fit the new code (its context changed); it will be reported, not applied")

        # 5b. the integration's own code on this Python
        code_errors, removed_imports = await hass.async_add_executor_job(_code_checks, scratch)
        if code_errors:
            blockers.append(f"the integration's code does not compile on Python {py}: " + "; ".join(code_errors[:3])
                            + (f" (+{len(code_errors) - 3} more)" if len(code_errors) > 3 else ""))
        if removed_imports:
            # the escape hatch is only real when something could ship the module: with the manifest's requirements
            # and pip's resolved set both known here, an import nothing provides fails at load, so it blocks
            provided = {_canon(_req_name(req)) for req in all_reqs} | {_canon(str(r.get("name") or "")) for r in pip["install"]}
            orphans = [e for e in removed_imports if not _may_provide(_removed_import_module(e), provided)]
            shimmed = [e for e in removed_imports if e not in orphans]
            if orphans:
                blockers.append(f"the integration imports modules Python {py} no longer has ({'; '.join(orphans[:3])}) "
                                "and none of its requirements provides them: it fails when loaded")
            if shimmed:
                warnings.append(f"the integration imports modules Python {py} no longer has ({'; '.join(shimmed[:3])}): "
                                "it fails when loaded, unless one of its requirements provides them")

        # 5c. names Home Assistant removed: an ImportError at setup, and never a blocker - the table is a
        # help, not a verdict, and a release may import one of them from a place the table does not know
        ha_symbols = await hass.async_add_executor_job(_ha_symbol_checks, scratch, target)
        if ha_symbols:
            warnings.append(f"the integration imports names Home Assistant {target} no longer has ({'; '.join(ha_symbols[:3])}): "
                            "it fails when loaded, unless it catches the ImportError somewhere the check cannot see"
                            + (f" (+{len(ha_symbols) - 3} more)" if len(ha_symbols) > 3 else ""))

        # 6. configuration surface
        yaml_present = os.path.isfile(installer.yaml_path(domain))
        old = installer.installed_manifest(domain) or {}
        cfg = {"config_flow": bool(manifest.get("config_flow")), "yaml_config_present": yaml_present,
               "integration_type": manifest.get("integration_type"), "iot_class": manifest.get("iot_class"),
               "entries_here": len(installer._entries_of(domain))}
        if not manifest.get("config_flow") and not yaml_present and not cfg["entries_here"]:
            warnings.append("no config flow and no YAML stored here: the integration would start unconfigured")
        entries = installer._entries_of(domain)
        if not manifest.get("config_flow") and entries:
            warnings.append(f"{ref} has no config flow, but {len(entries)} config entr{'y exists' if len(entries) == 1 else 'ies exist'} here: "
                            "Home Assistant cannot set them up with this version (they come back when a version with a config flow runs)")
        flow_version = await hass.async_add_executor_job(_config_flow_version, scratch)
        cfg["config_flow_version"] = flow_version
        newest_entry = max((e.version for e in entries), default=None)
        if flow_version is not None and newest_entry is not None and newest_entry > flow_version:
            warnings.append(f"config entries here are at version {newest_entry}, {ref}'s config flow is version {flow_version}: "
                            "Home Assistant cannot migrate an entry back (migration_error). Full rollback right after an upgrade "
                            "brings the entry back as it was; otherwise delete the entry and set it up again")
        if manifest.get("config_flow") and yaml_present and not entries:
            warnings.append("YAML is stored here and this version has a config flow: if it imports the YAML into a config entry, "
                            "remove the YAML afterwards (it is still applied at every boot)")

        return {
            "domain": domain, "ref": ref, "repo": repo, "target_ha": target, "current_ha": ha_version,
            "ok": not blockers, "blockers": blockers, "warnings": warnings,
            "versions": {"running": old.get("version"), "running_tag": (installer.state.installed.get(domain) or {}).get("running_tag"),
                         "new": manifest.get("version"), "min_ha": min_ha},
            "requirements": req_rows, "also_installed": extra, "pip_ok": pip["ok"], "pip_error": pip["stderr"],
            "source_builds": source_builds, "python": py, "code_errors": code_errors, "removed_imports": removed_imports,
            "removed_ha_symbols": ha_symbols,
            "dependencies": dep_rows, "patches": patch_rows, "config": cfg,
            "duration_s": round(time.monotonic() - t0, 1),
        }
    finally:
        def _cleanup() -> None:
            shutil.rmtree(scratch, ignore_errors=True)
            try:
                os.rmdir(os.path.dirname(scratch))  # a domain that is not installed leaves no empty dir behind
            except OSError:
                pass

        if not source_dir:  # the stored copy stays
            await hass.async_add_executor_job(_cleanup)


# ----- a Home Assistant version, before the restart installs it ------------
#
# The image carries one Python and no compiler; Home Assistant pins every
# requirement with "==", and an older release pins versions that were built
# before this Python existed.  ``requires_python`` does not say so (it is a
# lower bound only), so such a version is offered, the entrypoint starts the
# install and pip fails minutes later.  This resolves the same thing first.
#
# Not a full resolve of the dependency graph: with everything below the pins
# unconstrained, pip backtracks through thousands of candidates and gives up
# with "resolution-too-deep" - on good versions too (2026.9.2 does), so that
# answer says nothing about the version.  What decides the install is
# narrower and deterministic: every pin Home Assistant itself names must have
# a wheel for this interpreter.  ``--no-deps`` asks exactly that (no graph to
# walk), ``--only-binary=:all:`` makes pip refuse what it would otherwise try
# to compile, and ``--dry-run`` installs nothing.

HA_CACHE_S = 3600  # the answer for (version, Python) changes only when PyPI grows a wheel: an hour is plenty
MAX_HA_REPORTS = 32
MAX_HA_MISSING = 8  # pins named one per pip run: enough to describe a version, few enough to bound the check
_HA_REPORTS: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_HA_LOCK = asyncio.Lock()  # one check at a time; LOCK belongs to the integration preflight and can be held for minutes

_PIP_NO_DEPS = ("-m", "pip", "install", "--dry-run", "--quiet", "--report", "-", "--ignore-installed", "--no-deps", "--only-binary=:all:")
_NO_WHEEL_RE = re.compile(r"Could not find a version that satisfies the requirement (\S+)")
# Offline, pip says exactly what it says when a wheel is genuinely missing - "Could not find a version
# that satisfies the requirement X (from versions: none)" - and only the retry line above it tells the two
# apart.  Without this, a container with no route to PyPI reports the release as missing from PyPI, or its
# pins as having no wheel, and caches that answer for an hour.  Captured from pip itself, not from memory.
_PIP_UNREACHABLE_RE = re.compile(r"Retrying \(Retry\(|connection broken by|Failed to establish a new connection|"
                                 r"Temporary failure in name resolution|Name or service not known|ProxyError|"
                                 r"SSLError|Read timed out|Connection refused")
_HA_VERSION_RE = re.compile(r"\d{4}\.\d{1,2}\.\d+(b\d+)?\Z")


def _pip_no_deps(python: str, requirements: list[str]) -> subprocess.CompletedProcess:
    """Blocking: resolve ``requirements`` one by one (no dependency graph),
    wheels only, installing nothing."""
    return _run_pip([python, *_PIP_NO_DEPS, *requirements])


def _req_key(req: str) -> str:
    return re.split(r"[=<>!~ \[(]", req, maxsplit=1)[0].strip().lower().replace("_", "-").replace(".", "-")


def _ha_pins(python: str, version: str) -> tuple[list[str] | None, str]:
    """Blocking: the requirements ``homeassistant==version`` pins, read from
    pip's own resolution report (its metadata, not a second source)."""
    try:
        proc = _pip_no_deps(python, [f"homeassistant=={version}"])
    except subprocess.TimeoutExpired:
        return None, f"pip did not finish within {PIP_TIMEOUT_S}s"
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if _PIP_UNREACHABLE_RE.search(err):
            return None, f"PyPI could not be reached: {_pip_reason(err)}"
        if (m := _NO_WHEEL_RE.search(err)) and _req_key(m.group(1)) == "homeassistant":
            return [], f"no such Home Assistant release on PyPI: {m.group(1)}"
        return None, _pip_reason(err)
    try:
        report = json.loads(proc.stdout or "{}")
        meta = (report["install"][0]).get("metadata") or {}
    except (ValueError, LookupError):
        return None, "pip report was not the expected JSON"
    return [str(r) for r in (meta.get("requires_dist") or [])], ""


def _ha_wheel_check(python: str, version: str) -> dict[str, Any]:
    """Blocking, for the executor: what would stop ``pip install
    homeassistant==version`` in this image.  ``checked`` is False when pip
    answered something that is not about a missing wheel (a timeout, no
    index, a resolver artifact): that is "could not check", never a blocker.
    """
    import platform

    t0 = time.monotonic()
    py = ".".join(str(x) for x in sys.version_info[:3])
    machine = platform.machine()
    out: dict[str, Any] = {"version": version, "python": py, "machine": machine, "ok": True, "checked": True,
                           "blockers": [], "warnings": [], "notes": [], "missing": [], "requirements": 0}

    pins, err = _ha_pins(python, version)
    if pins is None:
        out.update(checked=False, notes=[f"could not check Home Assistant {version} against PyPI: {err}"],
                   duration_s=round(time.monotonic() - t0, 1))
        return out
    if not pins:
        # the release itself has no file pip can take (yanked between the version list and this check)
        out.update(ok=False, blockers=[f"Home Assistant {version} cannot be installed here: {err or 'no distribution on PyPI'}"],
                   duration_s=round(time.monotonic() - t0, 1))
        return out

    # a pin behind an environment marker (an extra, another platform) is not part of what the entrypoint installs
    base = [r for r in pins if ";" not in r]
    out["requirements"] = len(base)
    left, missing = list(base), []
    for _ in range(MAX_HA_MISSING):
        try:
            proc = _pip_no_deps(python, left)
        except subprocess.TimeoutExpired:
            out.update(checked=False, notes=[f"could not check Home Assistant {version}: pip did not finish within {PIP_TIMEOUT_S}s"])
            break
        if proc.returncode == 0:
            break
        err = proc.stderr.strip()
        if _PIP_UNREACHABLE_RE.search(err):
            out.update(checked=False, notes=[f"could not check Home Assistant {version}: PyPI could not be reached "
                                             f"({_pip_reason(err)})"])
            break
        m = _NO_WHEEL_RE.search(err)
        if not m:
            # "resolution-too-deep", a network failure, an index that answered 503: pip did not say a wheel is
            # missing, so nothing here knows whether the install would work.  Said plainly, not turned into a refusal.
            out.update(checked=False, notes=[f"could not check Home Assistant {version}: {_pip_reason(err)}"])
            break
        req = m.group(1)
        missing.append(req)
        key = _req_key(req)
        left = [r for r in left if _req_key(r) != key]
    else:
        # "at least", not "more than": the loop stops at the cap, so eight missing pins and eighty look the
        # same from here, and finding out which would cost another pip run for a verdict that is already
        # decided.
        out["warnings"].append(f"Home Assistant {version} pins at least {MAX_HA_MISSING} requirements without a wheel "
                               f"for Python {py}: only the first {MAX_HA_MISSING} are named")

    if missing:
        out["ok"] = False
        out["missing"] = missing
        one = len(missing) == 1
        out["blockers"].append(
            f"Home Assistant {version} needs {', '.join(missing)}, with no wheel for Python {py} on {machine}; "
            f"this image has no compiler to build {'it' if one else 'them'} "
            "(add one with HRI_APT_PACKAGES=build-essential and force, or choose a newer Home Assistant version)")
    elif out["checked"]:
        out["notes"].append(f"all {len(base)} pinned requirements of Home Assistant {version} have a wheel for Python {py} on {machine}")
    if len(base) != len(pins):
        out["notes"].append(f"{len(pins) - len(base)} conditional requirement(s) not checked (they depend on extras or another platform)")
    out["duration_s"] = round(time.monotonic() - t0, 1)
    return out


def ha_remember(version: str, report: dict[str, Any]) -> None:
    if not report.get("checked", True):
        # "could not check" is a statement about this moment - PyPI unreachable, pip gave up - not about the
        # version.  Keeping it for an hour would answer the next attempt with the same non-answer, and an
        # update scheduled in that hour would go out with no check behind it at all.
        return
    now = time.monotonic()
    for key in [k for k, (at, _) in _HA_REPORTS.items() if now - at >= HA_CACHE_S]:
        del _HA_REPORTS[key]
    _HA_REPORTS[(version, _ha_python_key())] = (now, report)
    while len(_HA_REPORTS) > MAX_HA_REPORTS:
        del _HA_REPORTS[min(_HA_REPORTS, key=lambda k: _HA_REPORTS[k][0])]


def ha_recent(version: str) -> dict[str, Any] | None:
    hit = _HA_REPORTS.get((version, _ha_python_key()))
    return hit[1] if hit and time.monotonic() - hit[0] < HA_CACHE_S else None


def _ha_python_key() -> str:
    """The second half of the cache key: the image's Python (and the
    architecture it was built for), which is what decides the answer."""
    import platform

    return f"{sys.version.split()[0]}-{platform.machine()}"


async def ha_version_report(hass: HomeAssistant, version: str) -> dict[str, Any]:
    """Whether ``homeassistant==version`` would install in this image, without
    installing it: ``ok`` False with ``blockers`` naming the pins that have no
    wheel, ``checked`` False when pip could not answer that question at all
    (then ``ok`` stays True - the version is not refused for a check that did
    not run).  Cached per (version, image Python) for HA_CACHE_S."""
    version = version.strip()
    if not _HA_VERSION_RE.fullmatch(version):
        raise ValueError(f"not a Home Assistant version: {version[:40]!r}")
    if (hit := ha_recent(version)) is not None:
        return hit
    async with _HA_LOCK:
        if (hit := ha_recent(version)) is not None:  # resolved while this call waited for the lock
            return hit
        report = await hass.async_add_executor_job(_ha_wheel_check, sys.executable, version)
    ha_remember(version, report)
    return report
