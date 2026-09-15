# hass-remote-integration

[![CI](https://github.com/trailro/hass-remote-integration/actions/workflows/ci.yml/badge.svg)](https://github.com/trailro/hass-remote-integration/actions/workflows/ci.yml)
[![Image](https://img.shields.io/badge/ghcr.io-hass--remote--integration-2496ED?logo=docker&logoColor=white)](https://github.com/trailro/hass-remote-integration/pkgs/container/hass-remote-integration)
<a href="https://www.buymeacoffee.com/trailro"><img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=%E2%98%95&slug=trailro&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy me a coffee" height="36"></a>

Run **one Home Assistant custom integration in its own small container**,
outside your main Home Assistant, and bring everything it produces back to
your main HA over **MQTT**: entities (with MQTT discovery), services and a
health signal.

Everything is managed from a web UI: install versions from GitHub, configure
the integration, start and roll back, back up and restore, move settings over
from an existing Home Assistant, and cut over when you are ready. No HACS, no
HA frontend and no shell needed.

> Nothing in the manager is specific to one integration: any custom
> integration published as GitHub releases, or sitting in a local directory,
> can be run this way.

---

## Why would I want this?

Custom integrations that talk to hardware are often the fragile part of a Home
Assistant install:

- a Home Assistant upgrade breaks them, or they pin a library that conflicts
  with something else;
- an integration update migrates its config, and going back is painful;
- a bug in one integration (a stuck event loop, a leaking serial port) slows
  down or restarts the whole house.

Running the integration in its own container decouples it:

- **Independent versions.** The container pins its own Home Assistant Core and
  its own library versions. Your main HA can upgrade freely: it only sees MQTT.
- **Reversible updates.** Several versions of the integration live side by
  side; every switch takes a backup first, is smoke-tested afterwards, and can
  be rolled back automatically.
- **Isolation.** A crash or a hang stays in its container.
- **Safe migration.** Run it in *shadow mode* next to your main HA, compare
  entity by entity, then switch over with one click (and undo with one click).

It is **not** a replacement for Home Assistant: there is no frontend, no
automations, no recorder. It runs exactly one integration and publishes it.

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
  newest stable Home Assistant from PyPI into `/config/venv-<version>`. Later
  versions are picked from the UI; the previous one is kept for rollback.
- The container holds **one integration**, with as many of its versions as you
  like in a version store. Want a second integration? Run a second container.
- Everything the container publishes is named after the integration: MQTT base
  topic `hass_<domain>`, discovery ids `hass_<domain>_...`. Several containers
  share one broker and one main HA without clashing.

---

## Requirements

- Docker with Compose.
- An MQTT broker reachable from the container (mosquitto or any other).
- Your main Home Assistant with the MQTT integration, if you want the entities
  to appear there.
- Access to the hardware your integration needs: a USB/serial device passed
  into the container, or a network bridge (see [Hardware access](#hardware-access)).

Footprint: roughly 170–210 MB of RAM with a typical integration running, and about
800 MB of disk per installed Home Assistant version.

---

## Quick start

The image, [`ghcr.io/trailro/hass-remote-integration`](https://github.com/trailro/hass-remote-integration/pkgs/container/hass-remote-integration),
is published on GitHub Container Registry for `amd64` and `arm64` (a Raspberry
Pi with a 64-bit OS, Apple silicon, most NAS boxes), from 0.9.0 on:

```bash
docker pull ghcr.io/trailro/hass-remote-integration:latest
```

All you need to run it is the compose file of the latest release, in a
directory of its own:

```bash
mkdir hass-remote-integration && cd hass-remote-integration
curl -fsSLO https://github.com/trailro/hass-remote-integration/releases/latest/download/docker-compose.yml
```

Put your settings in a `.env` file next to `docker-compose.yml`:

```bash
TZ=Europe/Berlin          # your time zone
HRI_PORT=8087             # port of the UI
# HRI_VERSION=0.13.0      # optional: pin a release (default: latest)
# HRI_PASSWORD=...        # optional: require a password for the UI and API
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
page. Then start it:

```bash
docker compose up -d
```

To build the image yourself instead, clone the repository and add the build
overlay (and your override file, if you have one, because Compose stops
loading it on its own once files are listed with `-f`):

```bash
git clone https://github.com/trailro/hass-remote-integration.git && cd hass-remote-integration
git checkout "$(git describe --tags --abbrev=0)"   # the latest release; main can be ahead of it
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Open `http://<docker-host>:8087`. On the very first start the page shows the
Home Assistant installation progress; it takes a few minutes.

---

## Your first integration, step by step

The UI has one page per task:

| Page | What you do there |
|---|---|
| **Overview** | See what runs and whether it is healthy, start/stop, restart, notifications, timeline of everything that happened |
| **Integration** | Versions and GitHub releases of the integration, preflight, patches, YAML config, config flow, config entries |
| **Install** | Install from the registry or any GitHub repo, the environment builder, dev mode |
| **MQTT** | Broker connection, translator status, discovery, recent commands, health rules |
| **Cutover** | Compare with your main HA, enable discovery, undo |
| **Entities / Devices / Services** | Inspect, rename, disable, call services |
| **Logs / Log files** | The integration's logs and the log files it writes |
| **System** | Home Assistant version, backups and restore, import from a HA backup, settings, diagnostics |

### 1. Install the integration

On **Install**, pick an integration from the registry and click *Install latest
stable*. To use one that is not in the registry, search for it under *Find an
integration*: it searches the default list of HACS custom integrations by name,
domain, repository, description and topics, and *Use* adds the hit to the
registry and selects it in the environment builder. You can also open *add a
repo to the registry* and give a domain and GitHub `owner/repo` yourself (the
repository must publish releases that contain `custom_components/<domain>/`).

Nothing runs yet: the version sits in the version store.

For more control use the **environment builder** on the same page: choose the
integration, any release, branch or commit, and a Home Assistant version, then
*Check*. Check downloads the release into a scratch directory, resolves its
Python requirements with `pip --dry-run`, evaluates patches and dependencies
and the minimum HA version, and tells you whether anything blocks the
combination, without touching the running environment. *Prepare* installs
exactly the combination that passed. Check also warns about configuration the
release cannot take over: config entries here while the release has no config
flow, entries at a newer version than its config flow (Home Assistant cannot
migrate an entry back), and YAML stored here that a config flow release will
import. A release that declares a newer minimum Home Assistant in `hacs.json`
cannot be started on an older one; prepare it together with that Home
Assistant version.

### 2. Configure it

Choose whichever fits the integration, on the **Integration** page:

- **Config flow**: runs the integration's own setup dialog, like HA's
  frontend would (the integration must be started first, because the flow is
  its code).
- **YAML config**: for integrations configured in `configuration.yaml`, paste
  what would go under `<domain>:`. It is validated on save and applied at boot.
  When a later release imports that YAML into a config entry, a notification
  says so: remove the YAML then, because it is still applied at every boot.
- **Import from your existing Home Assistant** (on **System**): upload a
  standard HA backup (`.tar`, encrypted or not). The config entries of the
  installed integration come over with their data *and* options, and entity
  ids, names, icons and disabled flags are aligned, so entities keep the same
  ids they had in your main HA.

### 3. Start it

Click **Start** on the **Overview** or **Integration** page. The version is
deployed, its requirements installed, patches applied, its config entries
enabled. A backup is taken first when something changes.

Some starts need a process restart (for example switching to a different
version of an integration that is already loaded, or applying YAML). The page
says so; use *Restart process*.

About five minutes after a start, a **smoke test** checks the health verdict.
If the new version does not set up right after a version switch (a config
entry in `setup_error` or `migration_error`, the integration not loaded, a
setup that never finishes), the manager rolls back to the previous version
and its backup automatically (configurable on **System**). A config entry
still in `setup_retry` (a device or broker not reachable yet) gets one more
smoke interval first, and is rolled back only if it has not loaded by then.
A `degraded` verdict (entities unavailable, silent, or without a state yet)
is never rolled back: the version did set up, so it is kept, and the
notification says it is degraded. A failed or degraded smoke test raises a
notification and stays in the last error until another version runs healthy,
also across the rollback's restart.

### 4. Connect MQTT

On **MQTT**, enter the broker host, port and credentials, tick *enabled*, save.
The container publishes one retained JSON document per entity, the service
catalog and a health document under `hass_<domain>/`.

Leave **discovery off** for now if your main HA still runs the same
integration: otherwise you would get every entity twice. Turning discovery
off after it was on removes the announced entities from your main HA again,
like *Undo* on **Cutover** (the manager device stays while `manager_discovery`
is on), so nothing changed in the container meanwhile lingers there.

### 5. Move over from your main Home Assistant

The recommended path is **shadow mode**: run the container next to your main
HA for a while, with MQTT enabled and discovery off, and compare.

1. Make sure only one side talks to the hardware in a way that conflicts
   (for example, only one side sends commands to the devices).
2. On **Cutover**, give your main HA's URL and a long-lived access token
   (optional; used only to compare). The page lists entities missing on either
   side and differences in state, names and flags.
3. When you are happy: **remove the integration from your main HA** (delete
   its config entries; disabling keeps its entity ids registered, and the
   Cutover page refuses then), then click *Enable discovery* on **Cutover**. Your main HA creates the entities from
   MQTT; the page watches until all of them exist.
4. Changed your mind? *Undo* removes every discovery config again, so your
   main HA drops the entities.

[docs/shadow-mode.md](docs/shadow-mode.md) describes a complete shadow-mode setup, including a
serial device shared by both instances through a TCP bridge.

---

## Everyday operation

### Updating hass-remote-integration

```bash
docker compose pull && docker compose up -d
```

Everything lives on the volume (Home Assistant, the integration, its
configuration, backups), so replacing the container keeps it; the new manager
is copied onto the volume at boot. With `HRI_VERSION` pinned, change it first.
Home Assistant writes its registries last when it stops, so the compose file
gives the container 120 s to stop (`stop_grace_period`) and runs an init
process; with plain `docker run`, add `--init --stop-timeout 120`.

The top bar shows the version that runs and the commit its image was built
from (`v0.13.0 · 1a2b3c4`), linking to that release. When GitHub has newer
releases than the one running (checked with the other update checks), a banner
under the top bar says so and links the release notes of each newer release,
newest first. Hiding it lasts until a newer release is published.

With a password set, 0.11.0 changes the session cookie format: log in once
after updating. Going back to an image older than 0.11.0 is possible (the
volume stays readable), but that older image honours logouts only by time, so
a session you logged out of since the update can work again there until it
expires or you log out once more.

### Updating the integration

Install the new release (Install or Integration page), then *Switch to* it.
The switch runs the **Preflight** of that release first, or reuses one run in
the last 30 minutes. Blockers stop the switch and are listed with a choice to
start anyway; warnings do not stop it. An update started from your main HA
over MQTT refuses on blockers, since nobody is there to confirm, and says why
in its result. Through the API, `POST /api/run/start` answers
`needs_force` with the report, and `force: true` starts anyway. Starting the
version that is already deployed, a dev build, a rollback and a restore skip
the preflight. The manager backs up, switches,
restarts if needed, smoke-tests, and rolls back on its own if the new version
is unhealthy. *Full rollback* on the Integration page brings back the previous
version together with the config as it was before the update; its restart is
smoke-tested too, without a further automatic rollback. After an automatic
rollback there is no Full rollback target: the version the smoke test rejected
is never offered again that way.

A downgrade of the integration after its config entries were migrated to a
newer format usually fails (`migration_error`), which the preflight warns
about. Full rollback right after the upgrade brings the entries back as they
were.

Once the new version has run (after the smoke test), *What changed between
versions* on the Integration page compares its entities and services with those
of the version before: entities added, removed or renamed, entities whose unit,
device class, state class or category changed, and services or service fields
added or removed. State attributes are not compared. Removed, renamed or changed entities and removed services or
fields are what break automations in your main HA, so they also raise a
notification. The last ten reports are kept.

### Updating Home Assistant inside the container

On **System**, choose a version and install it. The process restarts, the new
Home Assistant is installed into a new venv (the page shows progress), and the
integration's requirements are reinstalled there. If the new version fails to
boot three times in a row, the container falls back to the previous one. What
happened (a fallback, a failed install) stays on **System** until the next
version change and is announced once as a notification. Before restarting,
the page warns when the target is older than the minimum Home Assistant the
running integration declares, and when keeping the configuration on a
downgrade could fail.

Every version change, up or down, takes a backup of the current configuration
first. This is what makes it practical to try an integration on several Home
Assistant versions.

Home Assistant migrates its configuration forward only: a newer version
rewrites `.storage` in its own format and never converts it back. A downgrade
therefore asks what the older version starts with:

- **Restore from a backup.** `.storage` comes back from the newest backup made
  on the target version or an older one, which is the configuration in a
  format that version understands. Changes made after that backup are lost.
- **Start clean and rebuild the integration**, the default when there is no
  such backup. The older version starts like a fresh install. After it boots,
  the integration's config entries are created again with their data and
  options, its own store files are copied, and entity ids, names, icons,
  hidden and disabled flags and device names are applied again, all from the
  backup taken just before the switch. Areas, labels, other entity settings
  and the last known states are not carried over. Changes made after the
  switch was scheduled are not rebuilt; the configuration from right before
  the clean start is kept in a backup of its own, which the notification names.
- **Keep the current configuration.** This works when the older version can
  read the newer storage formats; otherwise the boot fails and the container
  falls back to the version you came from.

The integration version and the manager state stay as they are in every case.

### Python versions

The container has one Python interpreter, the image's (`python:3.14`). Home
Assistant, the integration and every package it requires run on it, so all
three have to support that Python:

- **Home Assistant.** Only versions whose PyPI `requires_python` accepts the
  image's Python are offered on **System** or installed. A version that would
  need another Python is refused before the restart.
- **The integration's requirements.** The preflight resolves them with pip
  against the running venv. A package whose `requires_python` excludes the
  image's Python, or that conflicts with Home Assistant's pins, is a blocker.
  A package with no wheel for this Python and architecture has to be built from
  source during the install. The preflight builds it for real. The image has no
  compiler, so a pure-Python package builds and one with C code is a blocker.
- **The integration's own code.** The preflight compiles every `.py` file with
  the image's Python (a syntax error is a blocker naming the file and line). It
  also warns about imports of standard modules that Python has removed
  (`imp`, `distutils`, `asyncore`, `telnetlib` and the rest of PEP 594), unless
  the import is guarded by `try/except ImportError` or something installed
  provides the module.

What the preflight cannot see is caught by the smoke test after the switch:
an unhealthy version is rolled back automatically.

### Backups

Taken automatically before every start that changes something, before every
Home Assistant version change, before a restore and before replacing the
integration; optionally daily. On **System**
you can create, download, upload, delete and restore them. A restore is
applied at the next restart, can be partial (only `.storage`, only the manager
state, …), and is rolled back if it fails halfway. Restoring the YAML part also
removes root `*.yaml` / `*.yml` files that are not in the backup, so a file
created after it (a `secrets.yaml`, for example) does not survive the restore.
A restore interrupted halfway (a stop, a full disk) puts the previous
configuration back and stays scheduled, so the next boot tries again; if even
putting it back fails, the restore still stays scheduled and the pre-restore
backup named in the error is kept from pruning. Backups, restored files and
uploads are created readable by the container user only (umask 077).
Automatic pruning keeps the newest backups by the date they were made (never
later than the file's own date), never removes the backup it runs after, and
leaves uploaded backups alone for their first 7 days. An upload never replaces
an existing backup: a name already taken gets a `-2`, `-3`, … suffix.
A restore never rolls back the record of what happened: the timeline, the
resource history, the change reports and the last known release versions are
not part of backups, and neither are the login key and the logout record.

Every backup records the Home Assistant version it was made on (the *HA* column),
and Home Assistant only migrates a configuration forward. Restoring a backup
made on an older version asks what to do: keep the running Home Assistant (the
default: the configuration is migrated forward when it starts) or go back to the
version the backup was made on, for exactly the state of the backup. A backup
made on a newer version can only be restored together with a switch to that
version. A backup that does not record its version is only restored with
`.storage` after a confirmation ("restore anyway", `"force": true` in the API
body), since it may come from a newer version. The version only matters when `.storage` is restored: a partial restore
without it never changes Home Assistant. A switch installs the version at the restart if its venv is no longer
on the volume (only the current and the previous one are kept), takes a backup
of the current configuration first, and brings it back if that version does
not start.

### Health

`hass_<domain>/health` carries a verdict: `ok`, `degraded` or `error`, with the
reason, entity counts and when the integration last wrote a state. With
discovery on, your main HA gets a connectivity sensor and a health sensor for
the container. The thresholds are on the **MQTT** page; mark an integration
that only writes on events as `event`, so silence is not reported as a fault.

The **Overview** keeps a resource history: memory, CPU, event-loop lag and
volume usage, one sample a minute, for 48 hours by default and up to 120
(*kept for … hours* on the same card). Memory that keeps growing for hours, or
an event loop held for 500 ms or more in several minutes of the last hour,
raises a notification: the usual signs of a leak or of blocking code in the
integration.

### Logs and log files

**Logs** shows the process log: everything Home Assistant and the integration
log, with filters and a live follow. Each line carries its date and time, and
the list holds the newest 200 lines (following live drops the oldest). Loggers listed in the registry's
`quiet_loggers` start at WARNING; raise one at runtime while you investigate.

**Log files** shows files the integration writes itself, such as traffic dumps
or debug logs. It appears in the menu only when there are any. The files are
found through the integration's config entries (any setting ending in `.log`),
the registry's `log_dir`, and `*.log` files in the config root.

By default every line is shown whole. The **Formatting** box at the bottom of
the page splits lines into columns. A format is a JSON object:

| Key | Required | Meaning |
|---|---|---|
| `pattern` | yes | Python regular expression matched at the start of each line. Each named group `(?P<name>...)` becomes a column, in order. Lines that do not match are shown whole. |
| `hide` | no | Group names captured but not shown |
| `dim` | no | Group names shown in a muted colour |
| `color_by` | no | Group whose value picks the row colour |
| `colors` | no | Map from a `color_by` value to `ok`, `warn`, `bad`, `accent` or `muted` |

Inside JSON every backslash is written twice. For lines like
`2026-01-01 12:00:00.123 WARNING (MainThread) [custom_components.demo] text`:

```json
{
  "pattern": "^(?P<time>\\S+ \\S+) (?P<level>[A-Z]+) \\((?P<thread>[^)]*)\\) \\[(?P<logger>[^\\]]+)\\] (?P<message>.*)$",
  "hide": ["thread"],
  "dim": ["time", "logger"],
  "color_by": "level",
  "colors": {"WARNING": "warn", "ERROR": "bad", "CRITICAL": "bad", "DEBUG": "muted"}
}
```

The format is checked on save: the pattern must compile and have at least one
named group. It is stored in `integration_manager/settings.json`, so it
survives image updates and is part of backups. The filter box always searches
the whole line, hidden groups included. Matching has a time limit: when a
pattern is too slow for the lines on screen, the remaining lines are shown
whole and the page says so.

### Replacing the integration

Installing a different integration in a container **replaces** the current
one: after a backup, its config entries, versions, patches, YAML and retained
MQTT documents are removed. The UI asks before doing it. To run two
integrations, run two containers:

```bash
HRI_NAME=hri-other HRI_PORT=8088 docker compose -p hri-other up -d
```

Each gets its own container name, port and volume.

---

## Hardware access

The integration runs in a Linux container, so it needs the hardware to be
visible there:

- **USB/serial devices** on a Linux host: add the device to the service in
  your `docker-compose.override.yml`, for example
  `devices: ["/dev/serial/by-id/usb-...:/dev/ttyUSB0"]`.
- **Hosts where USB passthrough is awkward** (macOS, some NAS systems): run a
  small serial-to-TCP bridge on the host and point the integration at
  `socket://host.docker.internal:<port>`. The compose file already maps
  `host.docker.internal` to the host.
- **Network devices** need nothing special, but note that the container does
  not see mDNS/multicast from your LAN in bridge networking: configure devices
  by IP address.

---

## Patches

Sometimes an integration or one of its libraries needs a small fix before
upstream ships it. Patches are applied after the requirements, every time the
integration starts and at every boot:

- `patches/<domain>/` in this repository ships with the image (empty here;
  use it in your own image builds, with `docker-compose.build.yml`);
- your own go to `integration_manager/patches/<domain>/` on the volume (upload
  on the Integration page); a file of the same name overrides a bundled one.

Two formats: a `*.py` module with `apply(ctx)` and `status(ctx)` (robust,
because it can find code by pattern), or a unified diff `*.patch` (applied only
when its context matches, never leaves broken Python behind). Two optional
headers retire a patch on its own:

```python
# integration-version: 1.2.0, 1.2.1   only for these versions
# applies-to: some-lib<2.0            only while this requirement matches
```

The Integration page has an editor: *New .py patch* and *New .patch diff* start
from a template, *Edit* opens an existing patch (a bundled one is saved as your
copy under the same name). *Check* changes nothing: for a diff it shows where
each hunk lands, or, when its context is gone, the closest lines in the file
next to what the hunk expects; a module runs its `status(ctx)`.

When a patch stops fitting the code it targets (upstream changed it, a file is
gone, the module fails), the integration still starts without it and a
notification on the Overview says which patch and why. It goes away once every
patch applies again.

---

## For integration authors: dev mode

Test an integration from your working copy without publishing a release:

```bash
HRI_DEV_SRC=/path/to/your/checkout docker compose \
  -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.dev.yml up -d
```

(With explicit `-f` files Compose no longer loads the override on its own:
list it, or leave it out if you do not have one.)

The directory is mounted read-only at `/dev-src`. The **Install** page lists
every `manifest.json` it finds there; *Install as local* copies it into the
version store as version `local`, which you start like any other. *Reinstall +
restart* refreshes the running copy after you edit the code.

`HRI_DEBUGPY=5678` (set by the dev overlay, bound to `127.0.0.1` only) makes
the process listen for a debugger: attach VS Code to `localhost:5678`.
Exceptions show up on the **Logs** page.

---

## MQTT reference

```
hass_<domain>/status                                online | offline (retained, last will)
hass_<domain>/health                                retained JSON, every 60 s
hass_<domain>/<integration>/<domain>/<object_id>    one retained document per entity
hass_<domain>/<integration>/event_stream/<object_id>  events of event entities, not retained
hass_<domain>/services/<domain>                     retained service catalog
hass_<domain>/cmd/<domain>/<object_id>/<field>      commands (used by discovery)
hass_<domain>/call/<domain>/<service>               service call, JSON payload
hass_<domain>/result/<domain>/<service>             call result, not retained
hass_<domain>/manager                               retained JSON, every 60 s: updates, resources
hass_<domain>/manager/cmd/<action>                  manager actions (with manager_commands)
hass_<domain>/manager/result                        outcome of a manager action, not retained
<prefix>/device/hass_<domain>_<device>/config       HA device-based discovery
```

- **Entity document**: state, attributes, `last_changed`, `last_updated`,
  `last_reported`, and the registry metadata (unique id, name, device class,
  unit, icon, category, device).
- **Discovery** (off by default): one retained config per device. Entities of
  every domain that has an MQTT platform become native entities with working
  commands; the rest (cameras, media players, weather, …) are mirrored as
  read-only sensors with all attributes. Per-entity rules on the Entities page
  or as JSON can exclude an entity or change its name, icon, category or
  default enablement on the MQTT side only.
- **Service calls**: publish a JSON object to `call/<domain>/<service>` (service
  data plus optional `entity_id`, and an optional `_id`); the result comes back
  on `result/...`. A repeated `_id` within five minutes is answered from memory
  and never executed twice. `homeassistant`, `shell_command`, `python_script`,
  `persistent_notification`, `hassio` and `integration_manager` are never
  callable. A call reaches only entities the container publishes: an
  `entity_id` of `all`, or an entity, area, floor, label or device that resolves
  to an excluded or unknown entity, is refused. A `device_id` that is not a
  Home Assistant device (a hardware address a service takes as data) stays plain
  service data. A call needs a JSON object, `{}` when it has no data: an empty
  payload is rejected.
- **Manager device**: with discovery on, or with `manager_discovery` alone (for
  example while running in shadow mode), the main Home Assistant gets a
  `hass-remote-integration (hass_<domain>)` device. It shows whether the
  integration is up and its health, has update entities for the integration,
  for Home Assistant in the container and for hass-remote-integration itself,
  and sensors for memory, CPU, event-loop lag (the worst delay of a
  one-second timer in the last minute, which is how an integration that blocks
  the loop shows up), volume usage and the patch status.
- **Manager actions** (`manager_commands`, off by default): *Install* on the
  integration and Home Assistant update entities, plus *Restart*, *Back up now*
  (at most every 10 minutes) and *Check for updates* (every 5 minutes) buttons. Installing the integration runs the
  preflight, then installs and starts the release the way the UI does (backup,
  smoke test, automatic rollback) and restarts when the loaded code has to be
  replaced; installing Home Assistant (upgrades only) takes a backup, keeps the
  configuration and restarts. Anyone who can publish under the base topic can use them, so
  turn this on only on a broker with credentials. hass-remote-integration
  itself is updated by pulling a new image.
- **Stop, uninstall, restore**: the identity (`hass_<domain>`) belongs to the
  running integration. *Stop* is not a removal: the whole device, the manager
  device included, goes unavailable on the main Home Assistant and keeps its
  entities with their customisations until the integration starts again.
  *Uninstall* clears everything retained under that identity, so the main Home
  Assistant removes the entities and devices. Entities that a restore, an
  import or a rebuild took away before a restart are removed there five
  minutes after Home Assistant in the container has started (only entities
  that exist neither as a state nor in its entity registry by then). The timeline, the resource history and
  the change reports are not part of backups, so a restore does not roll them
  back.
- Before connecting, the container checks that no *foreign* retained data sits
  under its base topic, and refuses to connect if there is (override with
  `force_base_topic`).

---

## Security

By default there is **no login**, like many self-hosted appliances on a
trusted LAN. Set `HRI_PASSWORD` (or `HRI_PASSWORD_FILE`, for example a Docker
secret) to require a password:

- the browser gets a session cookie from the login page, valid for 30 days,
  and **log out** in the top bar ends every session of the UI, in all browsers,
  including one opened a moment before (each logout starts a new session
  generation, signed into the cookie and kept across restarts and restores);
- scripts send the password as `Authorization: Bearer <password>`;
- after 5 wrong attempts from one address, that address is refused for 15
  minutes;
- changing the password logs every browser out.

Over plain HTTP the password and the session travel unencrypted, so on a
network you do not trust put the UI behind a reverse proxy with TLS. The
installation progress page of the very first start, served before Home
Assistant runs, is not protected; it only shows the progress.

Behind a reverse proxy, note that Home Assistant's HTTP server in the container
is not set up for proxies: it answers `400 Bad Request` to any request that
carries an `X-Forwarded-For` header, so configure the proxy not to send one.
Every request then comes from the proxy's address: five wrong passwords from
anywhere block new logins and `Bearer` scripts behind that proxy for 15 minutes
(browsers already logged in keep working). The server cannot tell that the
proxy speaks TLS, so set `HRI_COOKIE_SECURE=1` to mark the session cookie
`Secure`. An SSH tunnel or a VPN avoids all of this.

Without a password, anyone who can reach the port controls the container. The UI installs code
from any GitHub repository, accepts Python patches and runs service calls, so
access to the port means running arbitrary code inside the container, with
access to its volume, its secrets and every device or network it can reach.
Treat the port like SSH access to that container.

To make the UI reachable only from the Docker host, bind the port to localhost
in your `docker-compose.override.yml` and use an SSH tunnel or a reverse proxy
with authentication for remote access:

```yaml
services:
  hass-remote-integration:
    ports: !override
      - "127.0.0.1:8087:8087"
```

What is in place:

- A host-header guard against DNS rebinding: requests are served for IP
  addresses, `localhost` and local names (`.local`, `.lan`, `.home`,
  `.internal`, `.localdomain`, `.home.arpa`); add other names under *allowed host names* on
  **System**.
- State-changing requests need JSON or an explicit header, so a web page on
  another origin cannot trigger them.
- Secrets (MQTT password, GitHub token, parent HA token) are write-only in the
  UI, stored in files readable only by the owner, and never logged or included
  in the diagnostics zip. The diagnostics zip, the log tails and the inspection
  of an imported Home Assistant backup mask passwords, tokens, device keys
  (`local_key`, `noise_psk`, `encryption_key`, Z-Wave `network_key` and
  `s0`/`s2_*_key`, `bindkey`, `aes_key`, `ssl_key`, …), PINs, one-time codes,
  HMAC keys, webhook ids and cloudhook URLs, `Authorization` values (`Bearer`,
  `Basic` and any other scheme) and credentials in URLs.
- A release is downloaded only up to 100 MB and unpacked only up to 300 MB and
  20000 files; symbolic links in the archive are skipped. A requirement in a
  manifest that is a pip option (`--index-url …`, `-e …`) or not a valid
  requirement blocks the preflight and refuses the install and the start.
  The environment builder downloads exactly the commit its Check verified. Backups contain them; the login key and the logout
  record stay out of backups, so a restore never revives a logged-out session.
  The key of an encrypted Home Assistant backup you import is only used for
  that request.
- Dangerous service domains are not callable, over MQTT or from the UI.

**Do not expose the port to the internet.** Put it behind a reverse proxy with
authentication if you need remote access.

To report a security problem, see [SECURITY.md](SECURITY.md).

---

## Configuration reference

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `HRI_PORT` | `8087` | Port of the UI and API |
| `HRI_NAME` | `hass-remote-integration` | Container and volume name |
| `HRI_VERSION` | `latest` | Image tag Compose pulls, for example `0.13.0` |
| `TZ` | `UTC` | Time zone |
| `HA_VERSION_LATEST` | `1` | `0` installs the image's baseline HA on a fresh volume instead of the newest |
| `HRI_DEV_SRC` | `./dev-src` | Dev mode: directory mounted at `/dev-src` |
| `HRI_DEBUGPY` | unset | Dev mode: debugger port |
| `HRI_CALL_TIMEOUT` | `60` | Seconds a service call or command may take before it is reported as a timeout |
| `HRI_TRACEMALLOC` | unset | Diagnostics: allocation tracing frames (costs memory) |
| `HRI_TRACE_IMPORT` | unset | Diagnostics: log who imports the given packages |
| `HRI_DEBUG` | unset | Debug logging for the manager |
| `HRI_PASSWORD` | unset | Password for the web UI and API; unset or empty means no login |
| `HRI_PASSWORD_FILE` | unset | File holding the password, for example a Docker secret; wins over `HRI_PASSWORD` |
| `HRI_COOKIE_SECURE` | unset | `1` marks the session cookie `Secure` (behind a reverse proxy with TLS) |

### Files on the volume

```
/config/
  venv-<ha version>/            one per installed Home Assistant (venv-current links the active one)
  custom_components/<domain>/   the deployed integration
  integration_manager/
    state.json                  running integration, versions, pending actions
    settings.json               settings, tokens, log-file format (mode 600)
    auth_key                    signs login sessions, only with a password set (mode 600)
    auth_revoked                time of the last logout: sessions from before it are invalid
    mqtt.json                   broker configuration (mode 600)
    mqtt_rules.json             per-entity MQTT rules
    registry.json               your registry entries (see below)
    versions/<domain>/<tag>/    version store
    patches/<domain>/           your patches
    yaml/<domain>.yaml          YAML configuration
    events.jsonl                timeline
    process.log                 process log (rotated to process.log.1 and .2)
    change_reports.json         what the last version switches changed
    resource_history.json       resource samples of the Overview
    hacs_catalog.json           cached HACS list for the Install page search
  backups/                      backups (zip)
```

A registry entry in `integration_manager/registry.json` has this shape; only
`repo` is required:

```json
{"integrations": {"my_integration": {
  "name": "My integration",
  "repo": "owner/my_integration",
  "patch_module": "my_lib",
  "quiet_loggers": ["my_lib", "custom_components.my_integration"],
  "log_dir": "my_integration_logs"
}}}
```

| Key | Meaning |
|---|---|
| `name` | Display name on the Install page |
| `repo` | GitHub `owner/repo` that publishes the releases |
| `patch_module` | Python package whose site-packages the patches target |
| `quiet_loggers` | Loggers started at WARNING (default `custom_components.<domain>`) |
| `log_dir` | Directory under `/config` where the integration writes log files |

### API

Every page is backed by a JSON API on the same port, so everything can be
scripted. With a password set, send it as `Authorization: Bearer <password>`. POST
bodies are JSON (`Content-Type: application/json`), and requests that reach out
to the internet or another server (`/api/catalog`, `/api/patch_editor`,
`/api/parity`, `/api/releases/preview`, `/api/diagnostics`, `/api/log_files/tail`, `?refresh=1`)
also need `X-Requested-With: fetch`. The main entry points:

| Area | Endpoints |
|---|---|
| Status | `GET /api/status`, `GET /api/summary`, `GET /api/manager`, `GET /api/manager/history?hours=`, `GET /api/mqtt/status`, `GET /api/events`, `GET /api/notifications`, `POST /api/notifications/dismiss_all` |
| Integration | `POST /api/install`, `GET /api/change_reports`, `POST /api/run/{start,stop}`, `GET /api/releases`, `POST /api/releases/preflight`, `POST /api/installed/<domain>/{uninstall,rollback_full,remove_version}` |
| Builder / dev | `GET /api/catalog?q=`, `POST /api/build/{check,prepare}`, `GET /api/dev`, `POST /api/dev/install` |
| Configuration | `POST /api/flow/start`, `POST /api/flow/<id>`, `GET/POST /api/yaml/<domain>`, `GET /api/patches/<domain>`, `GET /api/patch_editor/<domain>?name=`, `POST /api/patch_editor/<domain>/{check,save}`, `GET /api/entries` |
| MQTT | `GET/POST /api/mqtt/config`, `POST /api/mqtt/{reconnect,republish}`, `GET /api/mqtt/discovery`, `GET /api/mqtt/commands` |
| Entities | `GET /api/entities`, `GET /api/devices`, `GET /api/services`, `POST /api/services/call` |
| System | `GET /api/ha`, `POST /api/ha/{update,rollback}`, `POST /api/restart`, `GET /api/backups`, `POST /api/backups/create`, `POST /api/backups/<name>/restore`, `POST /api/import/{upload,inspect,apply}` |
| Cutover | `GET /api/parity`, `POST /api/cutover/{status,enable,undo}` |
| Logs | `GET /api/logs`, `GET /api/log_files`, `GET /api/log_files/tail?file=&lines=&q=`, `GET/POST /api/settings` (`log_format`) |
| Diagnostics | `GET /api/diagnostics` (zip, secrets removed), `GET /api/diag/memory` |

---

## Troubleshooting

- **The page keeps showing the installation progress.** The first start
  downloads Home Assistant; a slow connection can take several minutes. The
  container log (`docker logs <name>`) shows pip's progress.
- **"restart required" does not go away.** Click *Restart process* on the
  Overview; some changes (a new version of a loaded integration, YAML) only take
  effect at a restart.
- **MQTT says the base topic is in use.** Something else left retained messages
  under `hass_<domain>/`. Remove them, or tick `force_base_topic` if they are
  yours from an earlier setup.
- **Entities appear twice in my main HA.** Discovery is on while the main HA
  still runs the same integration. Undo on **Cutover**, remove the
  integration from the main HA, enable again.
- **Health says degraded although everything works.** An integration that only
  writes states on events looks silent; set its health mode to `event` on the
  **MQTT** page.
- **Something went wrong and I need help.** *Diagnostics zip* on **System**
  collects versions, statuses, the timeline and recent logs, with secrets
  removed.

---

## Development

```bash
docker build -t hass-remote-integration:local . && sh verify.sh recreate   # rebuild, boot check, memory
sh verify.sh status
sh verify.sh test      # validates discovery payloads against the installed HA's MQTT schemas
sh verify.sh unit      # unit tests (tests/, stdlib unittest) in the container's HA venv
```

`verify.sh` reads `HRI_NAME`, `HRI_PORT`, `HRI_IMAGE`, `HRI_NETWORK`, `HRI_PASSWORD` and `TZ`
from the environment or from `.env`.

CI runs on every push to `main` and every pull request: syntax checks, then,
natively on both `amd64` and `arm64`, an image build, a boot on a fresh volume,
the discovery schema test and the unit tests. Publishing
a release builds the `amd64` and `arm64` image and pushes it to
`ghcr.io/trailro/hass-remote-integration` (`<version>`, `<major>.<minor>` and,
for a stable release, `latest`).

Inside the container the Home Assistant venv is `/config/venv-current/bin/python`
(the image's own `python3` does not have Home Assistant).

A few things that shaped the code, useful if you read it:

- Home Assistant's loader imports the `custom_components` namespace once, so
  the manager's source ships under `/app/manager_src` and is copied to the
  volume at boot.
- `SETUP_PORT` must be set before anything imports `homeassistant`, or the
  http server binds to 8123.
- The boot skips `homeassistant.bootstrap` and the recorder/logbook preloads to
  keep memory low; the rest is Home Assistant's own import graph.

## Limitations

- The web UI password is optional and travels unencrypted over plain HTTP: use a
  reverse proxy with TLS on networks you do not trust.
- One integration per container; two versions of the same integration cannot
  run at the same time.
- No Home Assistant frontend: integration features that exist only as frontend
  panels are not available.
- Discovery of entities without a `unique_id` works, but they cannot be
  renamed or disabled in the registry.
- Cutover does not remove the integration from your main HA for you: do that
  yourself before enabling discovery (the Cutover page checks it when the main
  HA is configured).
- The Python version is fixed by the image. Home Assistant versions or
  integrations that need another Python cannot run until a release moves the
  image to that Python. An image with a newer Python is tested against Home
  Assistant and the manager before release, not against every integration: after
  updating hass-remote-integration, run the preflight on the integration you
  use.
- Packages without a wheel for your architecture that need a compiler (C, Rust)
  cannot be installed. Wheels differ between amd64 and arm64, so an integration
  can install on one and not the other.
- Packages that load native system libraries (`libusb`, `bluez`, codecs) need
  those libraries in the image. The preflight does not check them. A missing
  library shows up when the integration loads.
- The code checks read the source only. Modules imported dynamically
  (`importlib`, `__import__`), code that behaves differently on this Python at
  run time, and incompatibilities inside requirements are caught by the smoke
  test and the automatic rollback, not by the preflight.

## License

[Apache License 2.0](LICENSE). Home Assistant and the integrations you run
with this tool keep their own licenses; they are downloaded at runtime and not
part of this repository.
