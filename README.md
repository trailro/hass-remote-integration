# hass-remote-integration

[![CI](https://github.com/trailro/hass-remote-integration/actions/workflows/ci.yml/badge.svg)](https://github.com/trailro/hass-remote-integration/actions/workflows/ci.yml)
[![Image](https://img.shields.io/badge/ghcr.io-hass--remote--integration-2496ED?logo=docker&logoColor=white)](https://github.com/trailro/hass-remote-integration/pkgs/container/hass-remote-integration)
[![Docker Hub](https://img.shields.io/docker/v/trailro26/hass-remote-integration?sort=semver&label=docker%20hub&logo=docker&logoColor=white)](https://hub.docker.com/r/trailro26/hass-remote-integration)
<a href="https://www.buymeacoffee.com/trailro"><img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=%E2%98%95&slug=trailro&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy me a coffee" height="36"></a>

Run **one Home Assistant custom integration in its own small container**,
outside your main Home Assistant, and bring what it produces back to your main
HA over **MQTT**: entities (with MQTT discovery), services and a health signal.

A web UI does the rest: install versions from GitHub, configure, start and
roll back, back up and restore, move settings over from an existing Home
Assistant, and cut over. No HACS, no HA frontend, no shell.

> Nothing in the manager is specific to one integration: any custom
> integration published as GitHub releases, or sitting in a local directory,
> can run this way.

---

## Why would I want this?

Custom integrations that talk to hardware are often the fragile part of a Home
Assistant install: an HA upgrade breaks them or they pin a conflicting library,
an update migrates their config and going back is painful, and a bug in one (a
stuck event loop, a leaking serial port) slows down or restarts the whole house.
A container of its own decouples the integration:

- **Independent versions.** The container pins its own Home Assistant Core and
  libraries. Your main HA upgrades freely: it only sees MQTT.
- **Reversible updates.** Versions live side by side; every switch takes a
  backup first, is smoke-tested, and can roll back automatically.
- **Isolation.** A crash or a hang stays in its container.
- **Safe migration.** Run it in *shadow mode* next to your main HA, compare
  entity by entity, then switch over with one click, and undo with one click.

It is **not** a replacement for Home Assistant: no frontend, no automations, no
recorder. It runs exactly one integration and publishes it.

---

## How it works

```
+--------------------------- container -----------------------------+
|  entrypoint.py   installs Home Assistant Core into a venv on the  |
|                  volume, applies scheduled restores, falls back   |
|                  to the previous HA after repeated failed boots   |
|                                                                   |
|  run.py          headless Home Assistant Core: loader, registries,|
|                  http on :8087, the manager, the integration      |
|                                                                   |
|  integration_manager  web UI + API: versions, config, patches,    |
|                  MQTT translator + discovery, health, backups,    |
|                  import, parity & cutover, diagnostics            |
+-------------------------------+-----------------------------------+
                                | MQTT: hass_<domain>/...
                                v
                     +----------+----------+       +---------------------+
                     |     MQTT broker     | <---> |  your main Home     |
                     |    (e.g. mosquitto) |       |  Assistant (MQTT    |
                     +---------------------+       |  integration)       |
                                                   +---------------------+
```

- Home Assistant is **not baked into the image**. A fresh volume installs the
  newest stable release from PyPI into `/config/venv-<version>`; later versions
  are picked in the UI, and the previous one is kept for rollback.
- The container holds **one integration**, with any number of its versions in
  a version store. For a second integration, run a second container.
- Everything it publishes is named after the integration: MQTT base topic and
  client id `hass_<domain>`, discovery ids `hass_<domain>_...`. Containers of
  different integrations share one broker and one main HA without clashing.
  Two containers of the *same* integration cannot share a broker (the names
  are not a setting): they would take each other's connection and clear each
  other's retained data. Put both config entries in one container, or give
  each its own broker.

---

## Requirements

- Docker with Compose, or Home Assistant OS / Supervised (as an app, see
  [below](#home-assistant-os--supervised)).
- An MQTT broker reachable from the container (mosquitto or any other).
- For the entities to appear in your main Home Assistant: its MQTT
  integration, on **2025.10 or newer** (2026.5 for `date`, `time` and
  `datetime` entities; see [what it needs](docs/mqtt.md#what-the-main-home-assistant-needs)).
  The container installs its own, separate Home Assistant.
- Access to the hardware your integration needs: a USB/serial device passed
  into the container, or a network bridge (see [Hardware access](#hardware-access)).

Footprint: roughly 170–210 MB of RAM with a typical integration running, and
about 800 MB of disk per installed Home Assistant version.

---

## Quick start

The image is built for `amd64` and `arm64` (a Raspberry Pi with a 64-bit OS,
Apple silicon, most NAS boxes). Both registries get every release with the same
tags (`<version>`, `<major>.<minor>`, `latest`) and the same digest:

| Registry | Image |
|---|---|
| GitHub Container Registry (the default; since 0.9.0) | [`ghcr.io/trailro/hass-remote-integration`](https://github.com/trailro/hass-remote-integration/pkgs/container/hass-remote-integration) |
| Docker Hub (since 0.16.0) | [`trailro26/hass-remote-integration`](https://hub.docker.com/r/trailro26/hass-remote-integration) |

Docker Hub limits pulls from hosts that are not logged in; either registry is
fine for one container.

Get the compose file of the latest release, in a directory of its own:

```bash
mkdir hass-remote-integration && cd hass-remote-integration
curl -fsSLO https://github.com/trailro/hass-remote-integration/releases/latest/download/docker-compose.yml
```

Put your settings in a `.env` file next to `docker-compose.yml`:

```bash
TZ=Europe/Berlin          # your time zone
HRI_PORT=8087             # port of the UI
# HRI_VERSION=0.24.0      # optional: pin a release (default: latest)
# HRI_REGISTRY=docker.io/trailro26  # optional: pull from Docker Hub (default: ghcr.io/trailro)
# HRI_PASSWORD=...        # optional: require a password for the UI and API
# HRI_APT_PACKAGES=ffmpeg  # optional: Debian packages installed at boot (what pip cannot install)
```

If your MQTT broker runs in Docker, the container must reach it. Put what is
specific to your machine in a `docker-compose.override.yml`, which Compose
loads automatically. For a broker on an existing Docker network called
`my-mqtt-network`:

```yaml
services:
  hass-remote-integration:
    networks:
      - my-mqtt-network

networks:
  my-mqtt-network:
    external: true
```

A broker elsewhere on your LAN needs nothing: use its IP address on the MQTT
page. Then start it (to build the image yourself instead, see
[Development](#development)):

```bash
docker compose up -d
```

Open `http://<docker-host>:8087`. On the very first start the page shows the
Home Assistant installation progress; it takes a few minutes.

### Home Assistant OS / Supervised

This repository is also an app repository:

1. **Settings > Apps > App Store**, menu **⋮ > Repositories**, add
   `https://github.com/trailro/hass-remote-integration`.
2. Install **hass-remote-integration**, set a password on its
   **Configuration** tab, and start it.
3. **Open Web UI**. The first start installs Home Assistant inside the app,
   which takes a few minutes and needs internet access.

The Mosquitto broker app is reachable as `core-mosquitto`, the broker host a
fresh app offers. Options, backups, serial devices and updates:
[docs/app.md](docs/app.md).

---

## Your first integration, step by step

The UI has one page per task:

| Page | What you do there |
|---|---|
| **Overview** | What runs and its health, start/stop, restart, notifications, the timeline |
| **Integration** | Versions and releases, preflight, patches, YAML config, config flow, config entries |
| **Install** | Install from the registry or any GitHub repo, the environment builder, dev mode |
| **MQTT** | Broker connection, translator status, discovery, recent commands, health rules |
| **Cutover** | Compare with your main HA, enable discovery, undo |
| **Entities / Devices / Services** | Inspect, rename, disable, call services |
| **Logs / Log files** | The integration's logs and the log files it writes |
| **System** | Home Assistant version, backups and restore, import from a HA backup, settings, diagnostics |

### 1. Install the integration

On **Install**, pick an integration from the registry and click *Install latest
stable*. For one that is not in the registry, *Find an integration* searches
the default HACS list of custom integrations by name, domain, repository,
description and topics; *Use* adds the hit to the registry and selects it in
the environment builder. Or open *add a repo to the registry* and give a domain
and a GitHub `owner/repo` whose releases contain `custom_components/<domain>/`.
Nothing runs yet: the version sits in the version store.

For more control, the **environment builder** on the same page takes the
integration, any release, branch or commit, and a Home Assistant version.
*Check* resolves the ref to a commit, downloads it into a scratch directory,
resolves its requirements with `pip --dry-run`, evaluates patches, dependencies
and the minimum HA version, and reports blockers and warnings. It installs
nothing and leaves nothing on the volume, but building a source-only package
runs its build code (`setup.py`, PEP 517 hooks), as an install would.
*Prepare* installs exactly the commit Check saw, and adds a repository you
typed in to the registry; if a branch moved since, Check again, and if GitHub
cannot say which commit a ref points at, Prepare refuses. A release whose
`hacs.json` declares a newer minimum Home Assistant cannot start on an older
one: prepare it together with that Home Assistant version.

### 2. Configure it

Choose whichever fits the integration, on the **Integration** page:

- **Config flow** runs the integration's own setup dialog, as HA's frontend
  would; start the integration first, since the flow is its code. Texts come
  from its `translations/en.json` (else raw schema keys), selectors render as
  their control (an unknown one as a JSON textarea; see
  [API](docs/api.md#configuration)), and a value the page cannot convert is
  refused under its field, with nothing sent. HA refuses a
  second flow for a device with `already_in_progress`: every flow it still
  holds, one abandoned by a page reload included, is listed with **Continue**
  and **Abort**, and *Start config flow* ends this page's flow first.
- **YAML config**, for integrations configured in `configuration.yaml`: paste
  what would go under `<domain>:`. It is validated on save and applied at boot.
  When a later release imports that YAML into a config entry, a notification
  says so: remove the YAML then, because it is still applied at every boot.
- **Import from your existing Home Assistant** (on **System**): upload a
  standard HA backup (the uncompressed `.tar` Home Assistant writes, encrypted
  or not). The installed integration's config entries come over with data
  *and* options, and entity ids, names, icons and disabled flags are aligned
  both ways, so entities keep their ids from your main HA. An import never goes
  next to an entry the integration already has: *Import* is refused with
  "already has a config entry here" (delete that entry; the upload stays), and
  *Import all* skips such integrations, lists them under `skipped` and deletes
  the upload once nothing failed (upload again to import a skipped one). An
  entry imported from the same backup before is skipped as "already imported";
  the integration's other entries still come over. An import is refused while
  another import, a start, stop or install runs; a restart in the middle
  leaves either the imported entry with its store, or no entry and the
  volume's own store
  ([details](docs/backups.md#import-from-a-home-assistant-backup)).

### 3. Start it

Click **Start** on **Overview** or **Integration**. The version is deployed,
its requirements installed and patches applied, and the config entries the
manager disabled (on a stop or a switch) are enabled again; an entry you
disabled yourself stays disabled. A backup is taken first when something
changes. A requirement that takes over 30 minutes to install is stopped with
everything it started and reported as `pip failed for: <requirement>`.
Starting another version than the deployed one runs its preflight first (see
[Updating the integration](#updating-the-integration)); blockers ask whether to
start anyway. If a start fails after the new files went out, the previous files
are put back. When a start needs a process restart (another version of a loaded
integration, or YAML), the page says so: use *Restart process*.

About five minutes after a start, a **smoke test** checks the health verdict.
If the version switched to does not set up (a config entry in `setup_error` or
`migration_error`, the integration not loaded, a setup that never finishes),
the manager rolls back to the previous version and its backup automatically
(configurable on **System**); an entry still in `setup_retry` gets one more
interval first. A `degraded` version did set up: it is kept and reported. For
an integration that breaks later, see the [health
watchdog](docs/health.md#the-health-watchdog) (off by default).

### 4. Connect MQTT

On **MQTT**, enter the broker host, port and credentials, tick *enabled*, save.
The container publishes one retained JSON document per entity, the service
catalog and a health document under `hass_<domain>/`.

Leave **discovery off** for now if your main HA still runs the same
integration, or you get every entity twice. Turning it off later removes the
announced entities from your main HA, like *Undo* on **Cutover** (the manager
device stays while `manager_discovery` is on).

### 5. Move over from your main Home Assistant

The recommended path is **shadow mode**: run the container next to your main
HA for a while, with MQTT enabled and discovery off, and compare what the
container announces with what your main HA has made of it. [Shadow
mode](docs/shadow-mode.md) describes a complete setup, including a serial
device shared by both instances through a TCP bridge.

1. Make sure only one side talks to the hardware in a way that conflicts
   (for example, only one side sends commands to the devices).
2. On **Cutover**, give your main HA's URL (no `user:password@`) and a
   long-lived access token (optional). The page matches the entities this
   container announces over MQTT with the MQTT entities your main HA created
   from that discovery, by unique id, and lists what is missing on either side
   and what differs in state, names and flags. The integration's own entities
   on your main HA are not part of the comparison: compare those by hand. The
   page only reads, and needs no administrator, but a token carries every right
   of its user: create it under a dedicated user without admin rights.
3. When you are happy: **remove the integration from your main HA** (delete
   its config entries; disabling keeps its entity ids registered, and the
   Cutover page refuses then), then click *Enable discovery* on **Cutover**.
   With the main HA configured, it refuses while the integration has config
   entries there, or while an entity id about to be announced is held by
   anything but this container's own mirror of that very entity (it would
   arrive as `_2`; a mirror of another entity renamed onto the id blocks too),
   and names what blocks. It also refuses while a scheduled restore,
   rollback, version switch, import or running action would replace the
   configuration at the next restart, or a smoke test is pending: restart or
   wait first. Your main HA then creates the entities, and the page watches
   until all exist. Entity ids stay the same, but the unique ids are new
   (`hass_<domain>_<entity id>`), so areas, labels and custom names have to be
   set again.
4. Changed your mind? *Undo* removes every discovery config again, so your
   main HA drops the entities. The manager device stays while
   `manager_discovery` is on, and the Undo answer says `manager_device_kept`.

---

## Everyday operation

### Updating hass-remote-integration

```bash
docker compose pull && docker compose up -d
```

With `HRI_VERSION` pinned, change it first. Everything lives on the volume
(Home Assistant, the integration, its configuration, backups), so replacing the
container keeps it. The new manager is copied onto the volume at boot and takes
the old one's place only once the copy is complete: on a full volume the copy
fails, the old manager starts and the log says so, so the UI you need to free
the disk stays up.

Home Assistant writes its registries last when it stops, so the compose file
gives the container 240 s to stop (`stop_grace_period`; a hanging stop ends the
process on its own after about 235 s) and runs an init process. With plain
`docker run`, add `--init --stop-timeout 240 --restart unless-stopped`: the
restart policy is required, because a restart from the UI or MQTT exits the
process and relies on Docker to start it again.

An old `docker-compose.yml` lacks settings: before 0.14.0 it gives 120 s to
stop, and before 0.18.0 it does not pass `HRI_APT_PACKAGES` on, so setting it
in `.env` does nothing. Download the file again (the command in [Quick
start](#quick-start)) when you update, or fix `stop_grace_period` by hand.

The image carries a healthcheck, so `docker ps` says `healthy` once the manager
API answers and `unhealthy` when it stops, and `depends_on: condition:
service_healthy` works. It needs no new compose file: a service without its own
`healthcheck:` inherits the image's. It asks `GET /api/status` on `HRI_PORT`
every 30 s with the image's Python, and holds off for the first 20 minutes, the
time a first Home Assistant install takes (see
[Troubleshooting](#troubleshooting)).

The top bar shows the running version and the commit its image was built from
(`v0.24.0 · 1a2b3c4`), linking to that release. When GitHub has newer releases
(checked with the other update checks), a banner links the release notes of
each, newest first; hiding it lasts in that browser until a newer release.

Updating from before 0.11.0 with a password set: log in once, as the session
cookie format changed. Going back to such an image works, but it honours
logouts only by time, so a session you logged out of since can work there again
until it expires or you log out once more.

### Updating the integration

Install the new release (Install or Integration page), then *Switch to* it. The
manager backs up, switches, restarts if needed, smoke-tests, and rolls back on
its own if the new version does not set up; a degraded version is kept and
reported.

The switch first runs the **Preflight** on the copy in the version store (what
it deploys, not what GitHub has now), or reuses its own check of that copy from
the last 30 minutes, kept in memory per copy and running Home Assistant
version; the *Preflight* button checks the release on GitHub and is never
reused. If the version is installed again while its check runs, the start is
refused: start it again. Blockers stop the switch, with a choice to start
anyway; warnings do not. A switch started over MQTT refuses on blockers and
says why in its result; for the API's `force`, see [API](docs/api.md).

No preflight runs for the deployed version, a release of an integration without
a GitHub repository, a rollback or a restore. A dev build is always checked,
even for a domain with no repository, from its copy on the volume, and again
after each upload. A preflight that cannot run (GitHub unreachable) does not
stop the start, but a stored copy with no `manifest.json` for the domain
blocks.

**Full rollback** on the Integration page brings back the previous version with
the configuration from before the update; its restart is smoke-tested, without
a further automatic rollback. The previous version is the one that actually
ran: after two switches with no restart between them, the target and its backup
are the version the process still runs. After an automatic rollback there is no
target. One full rollback runs at a time. It is refused while a Home Assistant
version change, an install or a restore is being prepared, or a switch with a
configuration restore or a clean start is scheduled; the automatic rollback
waits for those three instead.

Until its restart finishes it, a full rollback holds the manager: a restore by
hand, *Cancel restore*, *Stop*, *Uninstall*, a second full rollback, an install,
removing the version it goes back to or the one it left, and every start are
refused, each saying to restart or to undo. Starting the version it left undoes
it: that drops the rollback's restore, and the Full rollback target stays as it
was. The version it goes back to is recorded before the restore is scheduled,
so a rollback cut off halfway (`docker stop`, a power loss) finishes at the next
boot. If the restore did not happen (dropped or failed), the rollback is given
up, the integration stays on the version it ran, and the timeline says why. The
backup it restores is kept from pruning and deletion until the restore is over:
applied, failed, dropped at the boot, or undone.

A boot that finds an integration's config entries enabled and its copy deployed
while nothing is recorded as running (a restore of a backup taken while it ran,
after a stop) adopts it as running, as after a damaged `state.json`: *Stop*,
the health verdict and the MQTT identity then describe what runs. The version
comes from the marker next to the code, and the timeline and log say it was
adopted. With more than one integration in that state, none is adopted, and
the log says so.

A downgrade after the config entries migrated to a newer format usually fails
(`migration_error`); the preflight warns about it. Use Full rollback right
after the upgrade instead.

Once the new version has run (after the smoke test, also when degraded), *What
changed between versions* on the Integration page compares it with the version
before: entities added, removed or renamed; entities whose unit, device class,
state class or category changed; services and service fields added or removed.
State attributes are not compared, an entity with no state counts as removed,
and one that only gains or loses its unique id under the same entity id is the
same entity. Removed, renamed or changed entities and removed services or
fields break automations in your main HA, so they also raise a notification.
The last ten reports are kept.

### Updating Home Assistant inside the container

On **System**, choose a version and install it: after a backup, the process
restarts, installs it into a new venv and reinstalls the integration's
requirements. A new version that fails to boot three times before it ever booted
falls back to the previous one. The list offers the ten newest stable releases
plus every version the box has (*Show all versions* for the rest). Versions
below the image's floor (`HA_VERSION_MIN`, 2026.5.0) or whose pins have no wheel
for the image's Python (`python:3.14`) are refused. A downgrade asks whether to
restore from a backup, start clean and rebuild, or keep the configuration. See
[Home Assistant and Python versions](docs/home-assistant-versions.md).

### Backups

Backups are taken before every start that changes something, every Home
Assistant version change, every restore and before replacing the integration,
and optionally daily. On **System** you create, download, upload, delete and
restore them. A restore is applied at the next restart, can be partial, and
puts the previous configuration back if it fails. A backup holds secrets (the
tokens, the broker password, `secrets.yaml`, the credentials in `.storage`):
keep downloads as private as the volume. See [Backups and
restore](docs/backups.md).

### Health

`hass_<domain>/health` carries a verdict: `ok`, `degraded`, `error` or
`stopped`. Set the thresholds on **MQTT**; mark an integration that only writes
on events as `event`, and use the `updated` stale basis for one that keeps
re-writing the same states on a dead source. The **health watchdog** (on
**System**, off by default) reloads, then restarts, an integration stuck in
`error`, or in `degraded` if you tick it (15 minutes by default). The Overview keeps a resource history. See
[Health and the health watchdog](docs/health.md).

### Logs and log files

**Logs** shows the process log with filters and a live follow (the newest 200
lines); raise a logger's level at runtime while you investigate. **Log files**
lists the `*.log` files the integration writes, with tail, search and *Download
file*, all masked. The **Formatting** box splits lines into columns with a
regular expression. See [Logs and log files](docs/logs.md).

### Replacing the integration

Installing a different integration in a container **replaces** the current
one, after a backup and a confirmation: its config entries, versions, patches,
YAML and retained MQTT documents are removed (a cleanup the broker does not take
is retried as for an uninstall; the install's answer does not report it). To
run two integrations, run two containers, each with its own name, port and
volume:

```bash
HRI_NAME=hri-other HRI_PORT=8088 docker compose -p hri-other up -d
```

### Documentation

| File | What is in it |
|---|---|
| [docs/home-assistant-versions.md](docs/home-assistant-versions.md) | Changing the Home Assistant inside the container, downgrades, the image's floor, what the preflight checks |
| [docs/app.md](docs/app.md) | Running it as a Home Assistant OS / Supervised app: options, backups, serial devices, updates |
| [docs/backups.md](docs/backups.md) | What a backup holds and leaves out, restore, pruning, versions of backups |
| [docs/health.md](docs/health.md) | The health verdict and its document, stale basis, the health watchdog, resource history |
| [docs/logs.md](docs/logs.md) | The Logs and Log files pages, downloads, the line format |
| [docs/mqtt.md](docs/mqtt.md) | Topics, the entity document, discovery, commands, service calls, the manager device, MQTT rules, TLS, what the main HA needs |
| [docs/shadow-mode.md](docs/shadow-mode.md) | Running beside your main HA before the cutover, and the cutover checklist |
| [docs/security.md](docs/security.md) | Password and sessions, reverse proxies, what is masked, limits on downloads and uploads |
| [docs/files.md](docs/files.md) | Every file on the volume, hand edits, the registry format |
| [docs/api.md](docs/api.md) | The JSON API behind every page |
| [SECURITY.md](SECURITY.md) | Reporting a security problem |

---

## Hardware access

The integration runs in a Linux container, so the hardware must be visible
there:

- **USB/serial devices** on a Linux host: add the device to the service in your
  `docker-compose.override.yml`, for example
  `devices: ["/dev/serial/by-id/usb-...:/dev/ttyUSB0"]`.
- **Hosts where USB passthrough is awkward** (macOS, some NAS systems): run a
  serial-to-TCP bridge on the host and point the integration at
  `socket://host.docker.internal:<port>`. The compose file already maps
  `host.docker.internal` to the host.
- **Network devices** need nothing special, but in bridge networking the
  container does not see mDNS/multicast from your LAN: configure devices by IP.
- **Bluetooth** is not a package problem: `bleak` and the rest install and
  import, then find no adapter. The container needs the host's Bluetooth stack:
  a running `bluetoothd`, the host's D-Bus system socket
  (`volumes: ["/run/dbus:/run/dbus:ro"]`), host networking and
  `NET_ADMIN`/`NET_RAW`. Even then the adapter is shared with the host. The
  preflight warns about a requirement that needs it while the socket is not
  mounted.

---

## Patches

Patches fix an integration or one of its libraries before upstream does. They
are applied after the requirements, at every start of the integration and at
every boot:

- `patches/<domain>/` in this repository ships with the image (empty here; use
  it in your own image builds, with `docker-compose.build.yml`);
- your own go to `integration_manager/patches/<domain>/` on the volume (upload
  on the Integration page); a file of the same name overrides a bundled one.

Two formats:

- a `*.py` module with `apply(ctx)` and `status(ctx)`, robust because it can
  find code by pattern;
- a unified diff `*.patch`, applied only when its context matches, so it never
  leaves broken Python behind. A hunk with no context line (as `diff -U0`
  makes) is refused, since it cannot be located. Each hunk is looked for from
  its own line shifted by the hunks before it, like GNU patch; a hunk whose
  lines occur in more than one place about as near is refused as ambiguous. A
  patched block that also exists as a twin still counts as applied, unless an
  unpatched copy is about as near too. File paths may carry one `a/`, `b/` or
  `./` prefix and then name the file relative to the integration's directory
  (`a/const.py`), as `custom_components/<domain>/…`, or relative to
  site-packages for a library (`a/some_lib/module.py`); the domain on its own
  (`a/<domain>/const.py`) is none of these and reports the file absent.

Two optional headers retire a patch on its own:

```python
# integration-version: 1.2.0, 1.2.1   only for these versions
# applies-to: some-lib<2.0            only while this requirement matches
```

The Integration page has an editor: *New .py patch* and *New .patch diff* start
from a template, *Edit* opens a patch (a bundled one is saved as your copy under
the same name). *Check* changes nothing: for a diff it shows where each hunk
lands, `ambiguous` for one that matches several places about as near, or, when
its context is gone, the closest lines in the file; a module runs its
`status(ctx)`.

When a patch stops fitting (upstream changed the code, a file is gone, the
module fails), the integration still starts without it and a notification on
the Overview says which patch and why, until every patch applies again. That is
worked out when patches are applied (a start, a boot, *Apply*) and when one is
deleted; uploading or saving a patch changes it only at the next of those.

A patch retired by its headers is listed as `skipped`, with nothing to do,
unless it patched an installed library: a version change does not redeploy
those files, so the change is still in them. The row then says `skipped, still
applied` and what to do (reinstall that distribution, or switch back and delete
the patch). The integration's own files come back with every version change.

---

## For integration authors: dev mode

Test an integration from your working copy without publishing a release (list
the override file only if you have one: with `-f`, Compose no longer loads it
on its own):

```bash
HRI_DEV_SRC=/path/to/your/checkout docker compose \
  -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.dev.yml up -d
```

The directory is mounted read-only at `/dev-src`; the `dev_source_dir` setting
(`POST /api/settings`) points at another absolute path in the container when
your own mount puts it elsewhere. **Install** lists every `manifest.json` found
there (except the manager's own `integration_manager`); *Install as local*
copies it into the version store as version `local`, which you start like any
other, and *Reinstall + restart* refreshes the running copy after you edit the
code. Symbolic links are skipped, never followed, the limits of a release
archive apply (300 MB, 20000 files), and `__pycache__`, `.git`, `.mypy_cache`
and `.pytest_cache` are left out.

`HRI_DEBUGPY=5678` (set by the dev overlay) makes the process listen for a
debugger: attach VS Code to `localhost:5678`. Exceptions show up on **Logs**.
debugpy binds `127.0.0.1` in the container unless `HRI_DEBUGPY_HOST` says
otherwise; the overlay sets `0.0.0.0`, since a published port cannot reach the
container's loopback, and publishes the port on the host's `127.0.0.1` only.
Every container on the same Docker network can still reach it, and debugpy has
no authentication: whoever connects runs code in the container. Use the dev
overlay only on a Docker network you trust.

`HRI_DEBUG=1` also turns on Home Assistant's blocking-call detection (off
otherwise): file, directory and import calls on the event loop are logged with
the line that made them, and `time.sleep` or a blocking HTTP request on the loop
raises, as in a regular Home Assistant.

---

## MQTT reference

Everything goes under `hass_<domain>/`: `status` (online/offline, retained),
`health`, one retained document per entity, the service catalog under
`services/<domain>`, commands under `cmd/...`, service calls on
`call/<domain>/<service>` with results on `result/...`, and the manager device
(`manager`, and `manager/cmd/<action>` with `manager_commands`). Discovery (off
by default) publishes one device-based config per device under
`<prefix>/device/hass_<domain>_<device>/config`. Per-entity MQTT rules rename,
exclude or adjust what is published; `tls` encrypts the broker connection. The
full topic list, payloads and limits, and what your main HA needs, are in [MQTT
reference](docs/mqtt.md).

## Security

There is **no login by default**, and anyone who can reach the port controls the
container: it installs code from GitHub, runs patches and calls services. Treat
the port like SSH access to the container and **do not expose it to the
internet**. Set `HRI_PASSWORD` or `HRI_PASSWORD_FILE` to require a password;
that also refuses Home Assistant's webhooks on the port. Over plain HTTP the
password travels unencrypted: use a reverse proxy with TLS (and
`HRI_COOKIE_SECURE=1`), or bind the port to `127.0.0.1` and tunnel. Secrets are
write-only in the UI and masked on the log pages and in diagnostics, but
`docker logs` holds what the integration logged. See [Security](docs/security.md);
to report a problem, see [SECURITY.md](SECURITY.md).

## Configuration reference

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `HRI_PORT` | `8087` | Port of the UI, the API and the image's healthcheck; a change applies at the next boot, and one pinned in `.storage/http` by an older setup or a restored backup is dropped. A value that is not a port number (1-65535) stops the container at boot, with a line in the log |
| `HRI_NAME` | `hass-remote-integration` | Container and volume name |
| `HRI_VERSION` | `latest` | Image tag Compose pulls, for example `0.24.0` |
| `HRI_REGISTRY` | `ghcr.io/trailro` | Registry Compose pulls from: `ghcr.io/trailro` or `docker.io/trailro26` (Docker Hub), the same image. A compose file from 0.16.0 or older ignores it: download it again |
| `TZ` | `UTC` | Time zone; an unknown zone falls back to UTC, with an error in the log |
| `HA_VERSION_LATEST` | `1` | `0` installs the image's default Home Assistant (`HA_VERSION_DEFAULT`, the version the image was built with) on a fresh volume instead of the newest |
| `HRI_APT_PACKAGES` | unset | Debian packages installed at boot, before Home Assistant, for what pip cannot install (the `ffmpeg` binary, BlueZ): package names separated by spaces or commas (`ffmpeg libpcap0.8t64`, `libc6:arm64`). Each is taken as an exact package name, never as a pattern, so a typo such as `python3.1.` fails like any unknown package. An option, a URL, a path, a shell metacharacter or a trailing `-` (apt's remove operator) is refused with a line in the log; the value never reaches a shell. A trailing `+` is fine where it is part of the name (`g++`), but one that is not (`ffmpeg+`) makes apt run at every boot. Installed packages are skipped, so a restart costs nothing; recreating the container or updating the image installs them again. A failure shows on **System** and does not stop the boot. Output: `integration_manager/apt-install.log`. A compose file from 0.17.2 or older does not pass it: download it again |
| `HRI_DEV_SRC` | `./dev-src` | Dev mode: directory mounted at `/dev-src` |
| `HRI_DEBUGPY` | unset | Dev mode: debugger port |
| `HRI_DEBUGPY_HOST` | `127.0.0.1` | Dev mode: address debugpy binds inside the container (the dev overlay sets `0.0.0.0`) |
| `HRI_CALL_TIMEOUT` | `60` | Seconds a service call (over MQTT, from the Services page or `POST /api/services/call`) or a command may take before it is reported as a timeout; a whole number, else a warning and 60; below 1 uses 1 |
| `HRI_TRACEMALLOC` | unset | Diagnostics: allocation tracing frames (costs memory); a value that is not a number traces 25 |
| `HRI_TRACE_IMPORT` | unset | Diagnostics: log who imports the given packages |
| `HRI_DEBUG` | unset | `1` turns on debug logging for the manager and blocking-call detection on the event loop; unset, empty, `0`, `false`, `no` or `off` leaves them off |
| `HRI_PASSWORD` | unset | Password for the web UI and API; unset or empty means no login, only spaces or tabs keeps the UI closed until it is fixed |
| `HRI_PASSWORD_FILE` | unset | File holding the password, for example a Docker secret; wins over `HRI_PASSWORD`, and must not be empty |
| `HRI_COOKIE_SECURE` | unset | `1` marks the session cookie `Secure` (behind a reverse proxy with TLS) |

### Files on the volume

Everything is under `/config`: one `venv-<ha version>/` per installed Home
Assistant, the deployed integration in `custom_components/<domain>/`, the
manager's state, settings, MQTT configuration and rules, version store,
patches, YAML, timeline and logs in `integration_manager/`, and `backups/`.
Edit `settings.json`, `mqtt.json` or `mqtt_rules.json` by hand only while the
container is stopped; your own registry entries go in
`integration_manager/registry.json`. See [Files on the volume](docs/files.md).

### API

Every page is backed by a JSON API on the same port. With a password set, send
`Authorization: Bearer <password>`; POST bodies are JSON. Requests that reach
out to the internet, upload files, or return logs, patches or diagnostics also
need `X-Requested-With: fetch`, or they answer `400`. `GET /api/status` answers
without it too, from a copy at most 10 seconds old. See [API](docs/api.md).

## Troubleshooting

- **The page keeps showing the installation progress.** The first start
  downloads Home Assistant, which can take several minutes on a slow
  connection. Without a password the page shows pip's progress; it is also in
  `integration_manager/ha-install.log` on the volume, while the container log
  shows only the start and end. Until Home Assistant is started (the
  `HRI_APT_PACKAGES` packages, the PyPI lookup, the install, the manager's
  requirements, a scheduled restore, removing unused venvs), the page and every
  `/api/` path answer `503` with `Retry-After: 5`, under `/api/` with a JSON
  body naming the `phase` and the seconds since the container started
  (`elapsed`). `docker stop` during these steps stops pip and exits at once. An
  install runs as long as it makes progress; one that writes nothing for 15
  minutes is taken for hung and fails: the container starts the Home Assistant
  it already had, or, on a first start, exits and Docker starts it again. A
  version in `integration_manager/ha.json` that is not a version number (a hand
  edit) is ignored and logged.
- **`docker ps` says the container is unhealthy, or stays `starting`.**
  `starting` is the first 20 minutes, which covers the first install; the
  manager's first answer makes it healthy at once. `unhealthy` after that means
  three failed probes of `GET /api/status` in a row (about 90 s). `docker
  inspect --format '{{json .State.Health}}' <name>` shows what the probe got,
  and `docker logs <name>` why. A password changes nothing: a `401` is the
  manager answering. To see the probe's own error, run it by hand:
  `docker exec <name> python -c "import http.client, os;
  c = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('HRI_PORT') or 8087));
  c.request('GET', '/api/status'); print(c.getresponse().status)"`.
- **The page says Home Assistant is not started: a restore failed and could not
  be put back.** The configuration is half restored, and the page names the
  backup that holds it from before. Free space or fix the error in `docker logs
  <name>`; the restore is retried every 5 minutes. To start on the
  configuration as it is, delete `integration_manager/restore-pending.json`.
- **The integration needs `ffmpeg` or another system package.** Name the Debian
  packages in `HRI_APT_PACKAGES` (see [Environment
  variables](#environment-variables)) and restart the container. What apt
  printed is in `integration_manager/apt-install.log`, and **System** shows what
  this boot did: the packages, whether they were already installed, the names it
  refused and the error of a failed install. Nothing of this stops the boot: the
  integration that needs a missing package fails where you can see it.
  Bluetooth also needs the host's adapter and D-Bus (see [Hardware
  access](#hardware-access)).
- **A Home Assistant version is refused as "unlikely to install".** Its pinned
  requirements have no wheel for this image's Python (the message names them),
  and the image has no compiler, so the install would fail and fall back. Choose
  a newer version, or set `HRI_APT_PACKAGES=build-essential`, recreate the
  container and confirm the version anyway when the page offers to; building
  takes long and needs the memory. A version that says *could not check* is not
  refused: PyPI was unreachable or pip gave up, and the install may work.
- **A Home Assistant version is refused as "older than this image's floor".**
  The image refuses everything below `HA_VERSION_MIN` (2026.5.0 here), without
  *Schedule anyway*, even when a venv of that version is on the volume. The
  container keeps booting the version it runs, and a rollback to the previous
  version still works. Below the floor Home Assistant's own pins have no wheel
  for this Python: what is needed is an image with an older Python, not a
  different build argument.
- **"restart required" does not go away.** Click *Restart process* on the
  Overview; a new version of a loaded integration, or YAML, only takes effect at
  a restart. Preparing `main`, a branch or a dev build again counts as a new
  version (same name, different code): the start says a restart is required,
  and the smoke test waits for it.
- **Restart process does nothing.** A restart is refused, with the error on the
  page, while the manager is busy: an install, start, stop or uninstall, an
  import, a clean-start rebuild after a Home Assistant downgrade, a full
  rollback, a restore being scheduled or cancelled, a backup, a patch being
  applied, a Home Assistant version change, the self-check right after boot, or
  a restart already under way. Wait and restart again. A full volume does not
  refuse it: a state write that fails is logged and the restart goes ahead. A
  restart that fails for another reason before anything stops is refused with
  the reason. Once accepted, the process gives Home Assistant about 225 s to
  stop, the same total as `docker stop` (up to 20 s for Home Assistant's first
  stage, then 205 s), and then exits anyway, so a hanging stop still ends in a
  restart.
- **MQTT says the base topic is in use.** Something else left retained messages
  under `hass_<domain>/`. Remove them, or tick `force_base_topic` if they are
  yours from an earlier setup.
- **MQTT reconnects every few seconds, "closed the connection ... after
  accepting it".** Either a document is over the broker's maximum packet size
  (the log names it), or another client connects with the same client id
  `hass_<domain>`: usually a second container running the same integration on
  that broker, which cannot work (see [How it works](#how-it-works)).
- **Entities appear twice in my main HA.** Discovery is on while the main HA
  still runs the same integration. *Undo* on **Cutover**, remove the
  integration from the main HA, enable again.
- **Health says degraded although everything works.** An integration that only
  writes states on events looks silent; set its health mode to `event` on the
  **MQTT** page. With the `updated` stale basis, values that stay steady longer
  than *stale* read as silence (`no entity value change`): raise *stale*, or go
  back to `reported`.
- **Something went wrong and I need help.** *Diagnostics zip* on **System**
  collects versions, statuses, the timeline and recent logs, with secrets
  removed.

---

## Development

To run a build of your own instead of a published image, clone the repository
and add the build overlay. List your override file too, if you have one: once
files are given with `-f`, Compose stops loading it on its own.

```bash
git clone https://github.com/trailro/hass-remote-integration.git && cd hass-remote-integration
git checkout "$(git describe --tags --abbrev=0)"   # the latest release; main can be ahead of it
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

For work on the manager itself:

```bash
docker build -t hass-remote-integration:local . && sh verify.sh recreate   # rebuild, boot check, memory
sh verify.sh status
sh verify.sh test      # validates discovery payloads against the installed HA's MQTT schemas
sh verify.sh unit      # unit tests (tests/, stdlib unittest) in the container's HA venv
```

`verify.sh` reads `HRI_NAME`, `HRI_PORT`, `HRI_IMAGE`, `HRI_NETWORK`,
`HRI_PASSWORD` (or `HRI_PASSWORD_FILE`, which wins) and `TZ` from the
environment or `.env`. `HRI_NAME`, the container and its volume, defaults to
`hri-verify`: `recreate` removes that container, so set it only to a container
you mean to replace. `start` exits non-zero when the API does not come up (a
timeout or a restart loop). `HRI_ENV="K=V K2=V2"` passes more variables to the
container; CI boots a version other than the newest that way
(`HA_VERSION_LATEST=0 HA_VERSION_DEFAULT=<version>`). Inside the container the
Home Assistant venv is `/config/venv-current/bin/python` (the image's own
`python3` does not have Home Assistant).

`custom_components/integration_manager/ha_compat.json` gives, for every MQTT
platform and device class, the oldest Home Assistant that has it. It is
generated: `python3 tools/gen_ha_compat.py` downloads one Home Assistant wheel
per minor release from 2025.1 on (below 2024.11 no discovery arrives at all),
reads the platforms out of `mqtt/const.py` and the device classes out of each
domain's `const.py` or `__init__.py`, and writes the table with a stamp of what
it scanned and when. It needs network access and a few minutes. Re-run it when a
new Home Assistant is out; until then a main Home Assistant at or above the
newest release in the table is treated as knowing everything.

CI runs on every push to `main` and every pull request: syntax checks, then,
natively on `amd64` and `arm64`, an image build, a boot on a fresh volume, the
discovery schema test and the unit tests; on `amd64` also boots on the image's
default Home Assistant and on its floor, each checked to have installed the
version it was given. A weekly workflow repeats it against the newest stable
Home Assistant and the newest pre-release, opening an issue when one breaks the
manager and a pull request moving `HA_VERSION_DEFAULT` when a newer stable
passes. Publishing a release builds the `amd64` and `arm64` image and pushes it
to `ghcr.io/trailro/hass-remote-integration` and to Docker Hub as
`trailro26/hass-remote-integration` (`<version>`, `<major>.<minor>` and, for
the newest stable release, `latest`; the Docker Hub push needs the
`DOCKERHUB_TOKEN` repository secret). The newest stable release also updates
the Docker Hub overview from this README, up to *Everyday operation*. Docker
Hub keeps 25000 bytes of the overview, and a pull request or release whose
overview is longer fails CI.

A few things that shaped the code:

- Home Assistant's loader imports the `custom_components` namespace once, so
  the manager's source ships under `/app/manager_src` and is copied to the
  volume at boot.
- `SETUP_PORT` must be set before anything imports `homeassistant`, or the
  http server binds to 8123.
- The boot skips `homeassistant.bootstrap` and the recorder/logbook preloads to
  keep memory low; the rest is Home Assistant's own import graph.

## Limitations

- With a password set, Home Assistant's webhooks and other unauthenticated
  callbacks on the port are refused too (`401`); there is no allowlist.
- One integration per container; two versions of the same integration cannot
  run at the same time.
- No Home Assistant frontend: features that exist only as frontend panels are
  not available.
- Entities without a `unique_id` are discovered, but cannot be renamed or
  disabled in the registry.
- The container cannot detect the main Home Assistant's version: below 2025.10
  entities silently get generated ids, below 2024.11 nothing arrives, and
  `main_ha_version` helps with neither (see [What the main Home Assistant
  needs](docs/mqtt.md#what-the-main-home-assistant-needs)).
- The Python version is fixed by the image. Home Assistant versions or
  integrations that need another Python cannot run until a release moves the
  image to it. A new image is tested against Home Assistant and the manager, not
  against every integration: after updating hass-remote-integration, run the
  preflight on your integration. How far back Home Assistant can go is set by
  the image's floor (`HA_VERSION_MIN`, which no force lifts).
- Packages without a wheel for your architecture that need a compiler (C, Rust)
  cannot be installed. Wheels differ between amd64 and arm64, so an integration
  can install on one and not the other.
- Packages that load native system libraries (`libusb`, `bluez`, codecs) need
  them in the image. The image carries `libturbojpeg`, which Home Assistant's
  camera component wants; `HRI_APT_PACKAGES` adds what an integration needs. The
  preflight warns about the packages it knows (`_SYSTEM_DEPS` in
  `preflight.py`); any other missing library shows up when the integration
  loads.
- Renaming an entity here recreates it on the consuming Home Assistant: the
  discovery `unique_id` is derived from the entity id, so the old entity is
  deleted and a new one created there, without its area, custom name and hidden
  flag. Rename before cutover, or set the name the consumer sees with an MQTT
  rule.
- An entity hidden here stays visible on the consuming Home Assistant: MQTT
  discovery and the MQTT rules have no `hidden` option, and nothing on the other
  side reads the flag in the entity document. `enabled_by_default: false` (an
  MQTT rule) is the only way to keep an entity out of the way there.
- The code checks read the source only. Dynamic imports (`importlib`,
  `__import__`), code that behaves differently on this Python at run time, and
  incompatibilities inside requirements are caught by the smoke test, not the
  preflight: a version that does not set up is rolled back, a degraded one is
  kept and reported.

## License

[Apache License 2.0](LICENSE). Home Assistant and the integrations you run
with this tool keep their own licenses; they are downloaded at runtime and not
part of this repository.
