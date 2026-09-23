# Home Assistant and Python versions inside the container

## Updating Home Assistant inside the container

On **System**, choose a version and install it. The process restarts, the new
Home Assistant is installed into a new venv (the page shows progress), and the
integration's requirements are reinstalled there. If the new version fails to
boot three times in a row before it ever booted, the container falls back to
the previous one (the new version's venv is removed only once the previous one
has booted). A version that has booted once is never left automatically: when
it later crashes three times in a row (a changed setting or port, too little
memory), the container keeps retrying it and **System** and the log say so. On
a fresh volume there is nothing to fall back to: the first version keeps being
retried, and **System** says that it crashed and that this volume has no other
version to go back to — rather than claiming it booted fine before, which it
never did. A first version that cannot even be installed — the newest release,
published today, with no wheel for this image's Python yet — is a different
case: the container installs the version the image was built and tested with
(`HA_VERSION_DEFAULT`, or `HA_VERSION_MIN` when the floor is higher) instead,
records it as the desired version so the next boot does not ask PyPI for the
broken one again, and says on **System** which version could not be installed.
Only when that one fails too does the boot end. This is for a volume nothing
has ever run on — no version in `ha.json` and no `.storage`. On a volume that
has run something, a failed install ends the boot with the reason instead:
Home Assistant migrates storage forward only, so quietly starting an older
version on a newer configuration is the one thing the container must not do,
and the version you had stays recorded. A volume that has run something but has
no version recorded — `ha.json` deleted, or restored without it — is not treated
as fresh either: the container continues with the newest version it finds a venv
for on the volume, and prunes none of them that boot. If none of them runs on
this image's Python and the newest release cannot be looked up (no network, or
`HA_VERSION_LATEST=0`), the boot ends with the reason rather than installing the
image's own older version over your configuration. A
boot counts as good once the integration has set up, or 10 minutes after Home
Assistant started; stopping or restarting the container during a boot, also
while Home Assistant is still being imported, does not count as a failure —
only that boot's own failure is taken back, so the crashes before it still
count and a version that never boots still reaches the fallback. What
happened (a fallback, a failed install) stays on **System** until the next
version change and is announced once as a notification. Before restarting,
the page warns when the target is older than the minimum Home Assistant the
running integration declares, and when keeping the configuration on a
downgrade could fail.

Every version change, up or down, takes a backup of the current configuration
first. This is what makes it practical to try an integration on several Home
Assistant versions.

**How far back you can go is decided by the image, not by preference.** Two
rules refuse an older version, both before anything is scheduled. The first is
the image's floor, `HA_VERSION_MIN` (2026.5.0 in this image): anything older is
refused outright, and no force lifts it. That is not the same number as the
version a fresh volume installs (`HA_VERSION_DEFAULT`, the version the image
was built with; an override that empties it stops the container at boot with
the reason, since there is no version to fall back to) — the floor is the oldest release the manager was measured on, the default is a recent one to
start from. The second rule applies above the floor: an older release pins
requirements published before this image's Python existed, PyPI has no wheel
for them and the image has no compiler, so the manager resolves the chosen
version's pins itself — nothing is installed, and the answer is cached for an
hour, except a *could not check*: that says PyPI could not be reached at that
moment, not anything about the version, so the next attempt asks again — and
refuses a version whose requirements
cannot install, naming the package. A pin in the release's own metadata that
is a pip option or a direct URL (`pkg @ https://...`) is never handed to pip;
it blocks the version, as it would in an integration's manifest. A check can
take minutes on an old version,
and only one runs at a time. On **System**, *Check selected version* (and
picking a version in the list) shows the same verdict for the selection. When
the resolution cannot answer the question at all (PyPI unreachable, pip gave
up), the page says *could not check* and nothing is refused: a check that did
not run is not a reason to block. If you know better — you added a compiler
with `HRI_APT_PACKAGES=build-essential`, say — the confirmation offers to
schedule the version anyway. That covers the pin check only; a version below
the floor stays refused. In this image the two coincide: the floor was set to
where the pin check starts failing, so the pin check only begins to matter once
the image moves to a newer Python, or a build lowers `HA_VERSION_MIN`. *Python
versions* has the details of both.

Because of those floors, the list offers the **ten newest stable releases**
plus everything this box already has — every venv on the volume, the running
version, a scheduled one, the one a rollback goes back to — however old those
are, so nothing you have can fall off it. *Show all versions* adds the rest,
every stable release PyPI still offers (betas are never listed). Each entry
carries what is already known about it, with nothing resolved to find out: ✓ it
installs here (checked within the hour, or its venv is on the volume), ✗ it is
refused before anything is scheduled (older than the image's floor, a Python
this image does not have, or a pin with no wheel), and no mark when nobody has
checked or the check could not answer.

Home Assistant migrates its configuration forward only: a newer version
rewrites `.storage` in its own format and never converts it back. A downgrade
therefore asks what the older version starts with:

- **Restore from a backup.** `.storage` comes back from the newest backup made
  on the target version or an older one while the integration that runs now
  was running, which is the configuration in a format that version
  understands. Changes made after that backup are lost. A backup made while
  another integration ran (or none) is not used, since only `.storage` comes
  back and the manager stays as it is; when no backup qualifies, the refusal
  says how many were skipped for that reason, so choose rebuild or keep.
- **Start clean and rebuild the integration**, the default when there is no
  such backup. The older version starts like a fresh install. After it boots,
  the integration's config entries are created again with their data and
  options, its own store files are copied, and entity ids, names, icons,
  hidden and disabled flags and device names are applied again, all from the
  backup taken just before the switch. Areas, labels, other entity settings
  and the last known states are not carried over. Changes made after the
  switch was scheduled are not rebuilt; the configuration from right before
  the clean start is kept in a backup of its own, which the notification names.
  The rebuild holds the manager like an import: an install, start, stop,
  uninstall, full rollback, restore, Home Assistant version change or process
  restart is refused while it runs (try again). It waits for an action that is
  already running
  and then rebuilds only if the integration still runs.
- **Keep the current configuration.** This works when the older version can
  read the newer storage formats; otherwise the boot fails and the container
  falls back to the version you came from. Keep is refused while a restore
  scheduled on System, or the restore of a full rollback, brings back a
  backup made on a newer version than the target: that version could not
  read it and the boot would drop it. Cancel the restore (or, after a full
  rollback, restart to finish it) first.

The integration version and the manager state stay as they are in every case.

## Python versions

The container has one Python interpreter, the image's (`python:3.14`). Home
Assistant, the integration and every package it requires run on it, so all
three have to support that Python:

- **The image's floor.** The image is built with two Home Assistant versions,
  both build-time `ARG`s: `HA_VERSION_MIN` (2026.5.0 here) is the oldest
  release it installs at all, and `HA_VERSION` → `HA_VERSION_DEFAULT` is what
  a fresh volume installs when it is not told to take the newest, and the
  fallback when PyPI cannot be reached. Anything older than the floor is
  refused outright, before any of the checks below and with no force path, and a venv of that version already sitting on the volume is
  not an exception: selecting it is refused too, because nothing has measured
  this manager below the floor and the list must not offer what the manager
  will not schedule. What the floor never blocks is recovery — the container
  goes on booting the version it already runs (the entrypoint validates
  nothing), and a rollback to the recorded previous version goes through. The
  floor is a limit on what gets installed, not a trap for a box that is
  already running. Both are booted in CI on every change — the newest stable Home
  Assistant, the default and the floor, each on a fresh volume with the
  discovery schemas and the unit tests run against it — so neither number is a
  claim nobody checks. A weekly job does the same against whatever Home
  Assistant is newest that week and against its newest pre-release, so a
  release that breaks the manager is found here rather than by you — and when
  a newer release passes, that job is what proposes making it the default, so
  `HA_VERSION_DEFAULT` only ever names a version something has booted. The floor
  is where this manager was measured, not a guess — in September 2026, on this
  image's CPython 3.14.7 on `aarch64`: 2026.5.0, 2026.6.0 and 2026.7.0 were
  each run end to end (the manager, its UI and API, MQTT discovery to a main
  Home Assistant, all 13 manager and 31 domain discovery components, a
  preflight, a backup and a command round trip), as was a downgrade from
  2026.8.3 to 2026.6.0 on the same volume. Below 2026.5.0 nothing was measured,
  because Home Assistant's own pins stop resolving there (see below). A build
  or an override that sets the default below the floor installs the floor
  instead, and says so in the log: a fresh volume must not start on a version
  the UI then refuses to return to.
- **Home Assistant.** A version whose PyPI `requires_python` does not accept
  the image's Python is refused before the restart; it is still shown in the
  list, marked ✗ with the reason. While PyPI cannot be reached a version is
  refused too ("try again"), unless its venv is already installed for this
  Python. A release whose files were all yanked on PyPI is
  never offered, installed or picked for a first start; a venv of it (or of a
  version PyPI no longer lists) already installed for this Python can still be
  switched to.
- **Home Assistant's own pins.** `requires_python` is a lower bound, so it does
  not refuse a version whose pinned requirements predate this Python. Those are
  resolved separately: every requirement `homeassistant==<version>` pins has to
  have a wheel for this Python and architecture, because the image has no
  compiler. A version with a pin that has none is refused before the change is
  scheduled, naming the package — at most eight of them per version, enough to
  describe it and few enough to bound the check. `force` overrides this one
  refusal and nothing else; the manager device's install action has no
  override. This is what sets the floor: measured against this image's CPython
  3.14.7 on `aarch64` in September 2026, **Home Assistant 2026.5.0 and newer
  resolve entirely from wheels**, and every release from 2026.4.4 back does
  not — 2026.4.x on `fnv-hash-fast` and `lru-dict`, 2026.1.0–2026.3.0 on
  `lru-dict` alone, and 2025.10.0 and older on `aiohttp` and several more.
  `HA_VERSION_MIN` is set to that measurement. It is not a rule in the code:
  the wheel floor was measured on `aarch64` only, it moves down by itself as
  those projects publish wheels for this Python, and it moves up when the
  image's Python does.
  A venv already installed for this Python is not resolved again: the
  entrypoint boots it as it is.
- **The integration's requirements.** The preflight resolves them with pip
  against the running venv. A package whose `requires_python` excludes the
  image's Python, or that conflicts with Home Assistant's pins, is a blocker.
  A package with no wheel for this Python and architecture has to be built from
  source during the install. The preflight builds it for real. The image has no
  compiler, so a pure-Python package builds and one with C code is a blocker.
  A requirement given as an archive URL is built from that URL; one from a VCS
  URL or a local directory is not built by the preflight.
- **What a requirement needs from the image.** Some packages install perfectly
  and are only a wrapper over a program or a shared library the container must
  already carry: `ha-ffmpeg` over `ffmpeg`, `PyTurboJPEG` over
  `libturbojpeg.so.0`, `pyaudio` over `libportaudio.so.2`. pip cannot see that,
  so the integration starts and fails the moment it uses that part. The
  preflight knows a list of such packages and looks for what they need in this
  container (a program on `PATH`, a library in the library directories or known
  to `ldconfig`). What is missing is a warning, not a blocker, and names the
  package, what it wants and the Debian package that carries it, ending with
  the setting to make: `Set HRI_APT_PACKAGES=ffmpeg (next to what it already
  names) and recreate the container`. An integration is often useful without
  the part that needs it. A package whose program or library is there says nothing, and
  one that is not on the list is not checked. The list lives in
  `preflight.py` (`_SYSTEM_DEPS`), one line per package, next to the Debian
  package each program and library comes from (`_DEBIAN_PACKAGE`); a program
  or library whose package is not obvious is warned about without one, rather
  than with a name that may not exist. What is checked is the
  manifest's own requirements, those of the Home Assistant components it names
  under `dependencies` (that is where `ha-ffmpeg` comes from) and everything pip
  resolves for them.
- **What a requirement needs from the host.** `bleak`,
  `bluetooth-adapters`, `dbus-fast` and `habluetooth` install and import
  perfectly and then look for BlueZ on the system D-Bus: they need the host's
  Bluetooth stack, which a container cannot provide by itself and which no
  package installs. The preflight warns about them while the host's D-Bus
  socket is not mounted into the container; see [Hardware
  access](../README.md#hardware-access) for what a compose file has to give them.
- **Where pip's resolution landed.** When the newest release of a requirement
  needs something that cannot be installed here, pip does not fail: it walks
  back through older releases until one resolves. A requirement with no lower
  bound (`some-lib` rather than `some-lib>=2`) can send it back years, to a
  release the integration was never written against: it installs cleanly and
  breaks at runtime. After the
  dry run the preflight asks PyPI what the newest release that satisfies the
  requirement is, and warns when the resolved one is both in an older release
  series and at least two years older than it, naming both versions and their
  dates. It is a warning, never a blocker, and it is deliberately quiet about
  everything that is not backtracking: patch-level lag, a major published
  recently, releases this Python is excluded from, pre-releases, packages Home
  Assistant pins in its own constraints, and the requirements that come from
  the manifest's `dependencies`. At most twelve packages are looked up per
  preflight (the manifest's own requirements first), and a version that declares
  no requirements of its own is not checked at all. When PyPI cannot be reached
  it says nothing rather than guessing.
- **Names Home Assistant has removed.** A second walk over the release's
  imports looks for names Home Assistant itself dropped, which is the more
  common way an integration stops loading after a core update:
  `CLOUD_NEVER_EXPOSED_ENTITIES` (gone in 2026.6),
  `helpers.trigger.async_track_same_state` (2026.7), two `helpers.service`
  names that moved to `helpers.target` and one (`ServiceTargetSelector`) that
  simply went (2026.8), `device_registry.DEVICE_INFO_TYPES` and vacuum's
  `ATTR_BATTERY_LEVEL` (2026.9.3 — the wheel they were diffed against, so a
  target of 2026.9.0 to 2026.9.2 stays quiet rather than guess), and the rest
  of the table in
  `preflight.py` (`_REMOVED_HA_SYMBOLS`, one line per name, each read out of
  the published wheel rather than a release note). It is a warning, never a
  blocker: it names the file and the line, the version that removed the name,
  and where the name moved when it moved, so `from
  homeassistant.helpers.service import async_extract_referenced_entity_ids`
  reports `removed in 2026.8.0, now
  homeassistant.helpers.target.async_extract_referenced_entity_ids`. A name is
  only reported when the version that removed it is at or below the Home
  Assistant this container will run — on an older target the import still
  works and the check says nothing — and an import inside a `try` whose
  `except` catches `ImportError` is ignored here too, the way it is for a
  removed standard module. The table only knows the names in it, so silence is
  not a promise: the smoke test after the switch is still what catches an
  integration that does not set up. Only `from <module> import <name>` is
  matched, so a module imported whole and used by attribute says nothing here,
  and the warning names the first three hits and counts the rest.
- **The integration's own code.** The preflight compiles every `.py` file with
  the image's Python (a syntax error is a blocker naming the file and line, and
  so is a file over 5 MB or one too deeply nested for the parser),
  except in the top-level folders `tests`, `test`, `scripts`, `tools`, `docs`
  and `examples`, which Home Assistant does not load. An import of a standard
  module that Python has removed (`imp`, `distutils`, `asyncore`, `telnetlib`
  and the rest of PEP 594) is a blocker when nothing the version brings can
  provide it: neither the manifest's requirements nor the packages pip resolves
  for them is named after the module or is one of the shims that put it back
  (`standard-imghdr`, `legacy-cgi`). When one of them is, it stays a warning,
  because a shim is a real pattern. Either way the import is ignored when it
  sits in a `try` whose `except` catches `ImportError` or something broader
  (`ModuleNotFoundError`, `Exception`, `BaseException`, a bare `except`), or
  when the module turns out to be installed here after all.

What the preflight cannot see is caught by the smoke test after the switch:
a version that does not set up is rolled back automatically.

Some of what an integration needs is not a Python package at all and pip
cannot install it: the `ffmpeg` binary that Home Assistant's `ffmpeg`
component and everything built on it calls, BlueZ and D-Bus for Bluetooth
(which also want the host's hardware and bus, so the package alone is not
enough), and system libraries a wheel links against. The image carries only
libjpeg-turbo: ffmpeg would add about 400 MB to every install for the few
integrations that use it. `HRI_APT_PACKAGES` declares what this container
needs instead — the entrypoint installs those Debian packages at boot, before
Home Assistant starts, and does nothing when they are already there. **System**
shows what it did, and a failure does not stop the boot.
