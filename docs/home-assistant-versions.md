# Home Assistant and Python versions inside the container

How the container picks, updates and downgrades the Home Assistant it runs,
which versions it refuses and why, what the integration preflight checks
against the image's Python, and what happens when a new version does not boot.

## What runs inside

The container has one Python interpreter, the image's (`python:3.14`). Home
Assistant, the integration and every package it requires run on it, so all
three have to support that Python. The image has no compiler.

The image is built with two Home Assistant versions, both build-time `ARG`s:

| Variable | Meaning |
|---|---|
| `HA_VERSION_MIN` | The floor: the oldest release the image installs at all (2026.5.0). Anything older is refused. |
| `HA_VERSION` → `HA_VERSION_DEFAULT` | The image's default: a recent release to start from, not a floor. |

**Which version a volume starts on.** A fresh volume (no version in `ha.json`
and no `.storage`) installs the newest stable release. It installs the image's
default (`HA_VERSION_DEFAULT`) instead when `HA_VERSION_LATEST=0` or when PyPI
cannot be reached. A default below the floor installs the floor instead, and
the log says so. An override that empties `HA_VERSION_DEFAULT` stops the
container at boot with the reason.

A volume that has run something but has no version recorded (`ha.json`
deleted, or restored without it) is not fresh: the container continues with
the newest version it finds a venv for on the volume and prunes none of them
on that boot. If none of them runs on this image's Python and the newest
release cannot be looked up (no network, or `HA_VERSION_LATEST=0`), the boot
ends with the reason rather than installing the image's older default over
your configuration.

CI boots the newest stable release, the default and the floor on a fresh
volume on every change, with the discovery schemas and unit tests. A weekly
job also boots the newest pre-release and proposes a passing newer release as
the default, so `HA_VERSION_DEFAULT` only names a version something booted.

## Updating

On **System**, choose a version and install it. The process restarts, the new
Home Assistant is installed into a new venv (the page shows progress) and the
integration's requirements are reinstalled there. Every version change, up or
down, first takes a backup of the current configuration. Before restarting,
the page warns when the target is older than the minimum Home Assistant the
running integration declares, and when keeping the configuration on a
downgrade could fail.

The list offers the **ten newest stable releases** plus everything this box
already has, however old: every venv on the volume, the running version, a
scheduled one and the one a rollback goes back to. *Show all versions* adds
every other stable release PyPI still offers; betas are never listed. Each
entry shows what is already known, with nothing resolved to find out:

| Mark | Meaning |
|---|---|
| ✓ | Installs here: checked within the hour, or its venv is on the volume. |
| ✗ | Refused before anything is scheduled: below the floor, a Python this image does not have, or a pin with no wheel. |
| none | Not checked yet, or the check could not answer. |

A release whose files were all yanked on PyPI is never offered, installed or
picked for a first start. A venv of it, or of a version PyPI no longer lists,
already installed for this Python can still be switched to. The API is in
[api.md](api.md) (`GET /api/ha`, `POST /api/ha/check`, `POST /api/ha/update`).

## Downgrading

Home Assistant migrates its configuration forward only: a newer version
rewrites `.storage` in its own format and never converts it back. A downgrade
therefore asks what the older version starts with:

- **Restore from a backup.** `.storage` comes back from the newest backup made
  on the target version or an older one while the integration that runs now
  was running. Changes made after that backup are lost. A backup made while
  another integration ran (or none) is not used, since only `.storage` comes
  back and the manager stays as it is. When no backup qualifies, the refusal
  says how many were skipped for that reason; choose rebuild or keep.
- **Start clean and rebuild the integration**, the default when there is no
  such backup. The older version starts like a fresh install. After it boots,
  the integration's config entries are created again with their data and
  options, its own store files are copied, and entity ids, names, icons,
  hidden and disabled flags and device names are applied again, all from the
  backup taken just before the switch. Areas, labels, other entity settings
  and the last known states are not carried over, nor are changes made after
  the switch was scheduled. The configuration from right before the clean
  start is kept in a backup of its own, which the notification names. While
  the rebuild runs, an install, start, stop, uninstall, full rollback,
  restore, Home Assistant version change or process restart is refused (try
  again). The rebuild waits for an action already running, then rebuilds only
  if the integration still runs.
- **Keep the current configuration.** This works when the older version can
  read the newer storage formats; otherwise the boot fails and the container
  falls back to the version you came from. Keep is refused while a restore
  scheduled on System, or the restore of a full rollback, brings back a backup
  made on a newer version than the target, which that version could not read.
  Cancel the restore (or, after a full rollback, restart to finish it) first.

The integration version and the manager state stay as they are in every case.

## The floor and the version check

How far back you can go is decided by the image. Three rules refuse a version
before anything is scheduled:

- **The floor.** Anything older than `HA_VERSION_MIN` is refused outright, and
  no force lifts it. A venv of that version already on the volume is refused
  too. The floor never blocks recovery: the container goes on booting the
  version it already runs (the entrypoint validates nothing), and a rollback
  to the recorded previous version goes through.
- **`requires_python`.** A version whose PyPI `requires_python` does not
  accept the image's Python is refused; the list still shows it, marked ✗
  with the reason. While PyPI cannot be reached, a version is refused too
  ("try again"), unless its venv is already installed for this Python.
- **Home Assistant's own pins.** `requires_python` is only a lower bound, so
  every requirement `homeassistant==<version>` pins must also have a wheel for
  this Python and architecture. A version with a pin that has none is refused,
  naming the package (at most eight per version). A pin in the release's
  metadata that is a pip option or a direct URL (`pkg @ https://...`) is never
  handed to pip; it blocks the version.

The pin check installs nothing. It can take minutes on an old version, only
one runs at a time, and its answer is cached for an hour, except *could not
check* (PyPI unreachable, pip gave up), which is asked again next time and
refuses nothing. *Check selected version* on **System**, or picking a version
in the list, shows the verdict. If you know better, for example after adding
a compiler with `HRI_APT_PACKAGES=build-essential`, the confirmation offers to
schedule the version anyway (`force`). That overrides the pin check only;
the manager device's install action has no override. A venv already
installed for this Python is not resolved again: the entrypoint boots it as
it is.

The floor is where the pin check starts failing, measured in September 2026
on this image's CPython 3.14.7 on `aarch64`: **2026.5.0 and newer resolve
entirely from wheels**; every release from 2026.4.4 back does not (2026.4.x on
`fnv-hash-fast` and `lru-dict`, 2026.1.0–2026.3.0 on `lru-dict` alone,
2025.10.0 and older on `aiohttp` and more). 2026.5.0, 2026.6.0 and 2026.7.0
were each run end to end (all 13 manager and 31 domain discovery components),
as was a downgrade from 2026.8.3 to 2026.6.0. Only `aarch64` was measured.
The wheel floor moves down as those projects publish wheels for this Python
and up when the image's Python moves. In this image the floor and the pin
check coincide, so the pin check only matters once the image moves to a newer
Python or a build lowers `HA_VERSION_MIN`.

## Python versions

The integration preflight checks the following against the image's Python and
the target Home Assistant. What it cannot see, the smoke test after the switch
catches: a version that does not set up is rolled back automatically.

- **Requirements.** pip resolves them against the running venv. A package
  whose `requires_python` excludes the image's Python, or that conflicts with
  Home Assistant's pins, is a blocker. A package with no wheel for this Python
  and architecture is built from source for real: pure Python builds, C code
  is a blocker. A requirement given as an archive URL is built from that URL;
  one from a VCS URL or a local directory is not built.
- **What a requirement needs from the image.** Some packages wrap a program or
  shared library the container must carry: `ha-ffmpeg` over `ffmpeg`,
  `PyTurboJPEG` over `libturbojpeg.so.0`, `pyaudio` over `libportaudio.so.2`.
  pip cannot see that, so the integration fails only when it uses that part.
  For the packages in `preflight.py` (`_SYSTEM_DEPS`; others are not checked),
  the preflight looks for a program on `PATH` or a library in the library
  directories or known to `ldconfig`. What is missing is a warning naming the
  package, what it wants and the Debian package that carries it
  (`_DEBIAN_PACKAGE`; none when it is not obvious), ending with `Set
  HRI_APT_PACKAGES=ffmpeg (next to what it already names) and recreate the
  container`. It covers the manifest's requirements, those of the Home
  Assistant components it names under `dependencies` (where `ha-ffmpeg` comes
  from) and everything pip resolves for them.
- **What a requirement needs from the host.** `bleak`, `bluetooth-adapters`,
  `dbus-fast` and `habluetooth` need BlueZ on the host's system D-Bus. The
  preflight warns about them while the host's D-Bus socket is not mounted
  into the container; see [Hardware access](../README.md#hardware-access).
- **Where pip's resolution landed.** When a requirement's newest release
  cannot install here, pip walks back through older ones, and a requirement
  with no lower bound can land years back and break at runtime. The preflight
  warns when the resolved version is both in an older release series and at
  least two years older than the newest release that satisfies the
  requirement, naming both versions and dates. It stays quiet about
  patch-level lag, a recent major, releases this Python is excluded from,
  pre-releases, packages Home Assistant pins in its constraints, and
  requirements from the manifest's `dependencies`. It looks up at most twelve
  packages per preflight (the manifest's own first), skips a version with no
  requirements of its own, and says nothing when PyPI cannot be reached.
- **Names Home Assistant has removed.** The preflight warns about imports of
  names in `preflight.py` (`_REMOVED_HA_SYMBOLS`, each read out of the
  published wheel), such as `CLOUD_NEVER_EXPOSED_ENTITIES` (2026.6) or the
  `helpers.service` names moved to `helpers.target` (2026.8). The 2026.9
  entries were diffed against the 2026.9.3 wheel, so a target of 2026.9.0 to
  2026.9.2 stays quiet. The warning names the file and line, the removing
  version and where the name moved (`removed in 2026.8.0, now
  homeassistant.helpers.target.async_extract_referenced_entity_ids`), and
  lists the first three hits and counts the rest. A name is reported only when
  its removing version is at or below the target. Only `from <module> import
  <name>` is matched, and an import in a `try` whose `except` catches
  `ImportError` is ignored. The table only knows the names in it.
- **The integration's own code.** Every `.py` file is compiled with the
  image's Python, except in the top-level folders `tests`, `test`, `scripts`,
  `tools`, `docs` and `examples`. A syntax error, a file over 5 MB or one too
  deeply nested for the parser is a blocker naming the file and line. An
  import of a standard module Python removed (`imp`, `distutils`, `asyncore`,
  `telnetlib` and the rest of PEP 594) is a blocker, or only a warning when a
  requirement (or what pip resolves for it) is named after the module or is a
  shim that puts it back (`standard-imghdr`, `legacy-cgi`). The import is
  ignored inside a `try` whose `except` catches `ImportError` or broader
  (`ModuleNotFoundError`, `Exception`, `BaseException`, a bare `except`), or
  when the module is installed here after all.
- **Configuration the release cannot take over.** The preflight warns about
  config entries here while the release has no config flow, entries at a newer
  version than its config flow (Home Assistant cannot migrate an entry back),
  and YAML stored here that a config-flow release will import.

What pip cannot install (the `ffmpeg` binary, BlueZ and D-Bus, system
libraries a wheel links against) goes in `HRI_APT_PACKAGES`: the entrypoint
installs those Debian packages at boot, before Home Assistant starts, and does
nothing when they are already there. **System** shows what it did, and a
failure does not stop the boot. The image carries only libjpeg-turbo, since
ffmpeg would add about 400 MB to every install. Bluetooth also needs the
host's hardware and bus.

## When a new version does not boot

A boot counts as good once the integration has set up, or 10 minutes after
Home Assistant started. Stopping or restarting the container during a boot,
also while Home Assistant is still being imported, does not count as a
failure; only that boot's own failure is taken back, so earlier crashes still
count.

- **A version that never booted** and fails three times in a row: the
  container falls back to the previous version. The new version's venv is
  removed only once the previous one has booted.
- **A version that has booted once** is never left automatically. When it
  later crashes three times in a row (a changed setting or port, too little
  memory), the container keeps retrying it, and **System** and the log say so.
- **A fresh volume** has nothing to fall back to: the first version keeps
  being retried, and **System** says it crashed and that the volume has no
  other version to go back to.
- **A first version that cannot be installed** on a fresh volume (a release
  published today, with no wheel for this image's Python yet): the container
  installs the image's default (`HA_VERSION_DEFAULT`, or `HA_VERSION_MIN` when
  the floor is higher) instead, records it as the desired version so the next
  boot does not ask PyPI for the broken one again, and **System** names the
  version that could not be installed. If that one fails too, the boot ends.
- **A failed install on a volume that has run something** ends the boot with
  the reason, and the version you had stays recorded: starting an older
  version on a newer configuration is the one thing the container must not do.
- **A version older than the configuration** is never started, whatever chose
  it: a switch whose restore or clean start did not happen (a Home Assistant
  backup of the app restored while one was scheduled), the newest release an
  older image's Python supports, the fallback after a failed install. The
  version that last wrote the configuration is the one in `/config/.HA_VERSION`,
  which Home Assistant writes itself. The boot ends with the reason in the log
  and in `integration_manager/ha.json` (`last_error`, shown on **System** once
  Home Assistant runs again), and the wanted version is left as it was. To go
  on, run that version or newer (the image that ran it, or
  `"desired": "<that version>"` in `ha.json`), or restore a backup made on the
  older version. A downgrade with **keep**, and one whose restore or clean
  start ran, boot as before.

What happened (a fallback, a failed install) stays on **System** until the
next version change and is announced once as a notification.
