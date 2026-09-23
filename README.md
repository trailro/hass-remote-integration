# hass-remote-integration

[![CI](https://github.com/trailro/hass-remote-integration/actions/workflows/ci.yml/badge.svg)](https://github.com/trailro/hass-remote-integration/actions/workflows/ci.yml)
[![Image](https://img.shields.io/badge/ghcr.io-hass--remote--integration-2496ED?logo=docker&logoColor=white)](https://github.com/trailro/hass-remote-integration/pkgs/container/hass-remote-integration)
[![Docker Hub](https://img.shields.io/docker/v/trailro26/hass-remote-integration?sort=semver&label=docker%20hub&logo=docker&logoColor=white)](https://hub.docker.com/r/trailro26/hass-remote-integration)
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
  topic and client id `hass_<domain>`, discovery ids `hass_<domain>_...`.
  Containers running different integrations share one broker and one main HA
  without clashing. Two containers running the *same* integration cannot share
  a broker: the names are derived from the domain and are not a setting, so
  they would take each other's connection and clear each other's retained
  data. Put both config entries in one container, or give each container its
  own broker.

---

## Requirements

- Docker with Compose.
- An MQTT broker reachable from the container (mosquitto or any other).
- Your main Home Assistant with the MQTT integration, if you want the entities
  to appear there — **2025.10 or newer**, and 2026.5 or newer if the integration
  you mirror has a `date`, `time` or `datetime` entity — unless you set
  `main_ha_version`, which covers only that second part: it leaves out the
  platforms and device classes the version you name does not have. It cannot
  give a pre-2025.10 instance its own entity ids, and below 2024.11 nothing
  arrives whatever you declare (see *What the main Home Assistant needs*). The Home Assistant inside the container is a different
  thing entirely, and the container installs it itself.
- Access to the hardware your integration needs: a USB/serial device passed
  into the container, or a network bridge (see [Hardware access](#hardware-access)).

Footprint: roughly 170–210 MB of RAM with a typical integration running, and about
800 MB of disk per installed Home Assistant version.

---

## Quick start

The image is built for `amd64` and `arm64` (a Raspberry Pi with a 64-bit OS,
Apple silicon, most NAS boxes) and published to two registries. Both get every
release with the same tags (`<version>`, `<major>.<minor>`, `latest`) and the
same digest, so use whichever you prefer:

| Registry | Image | Since |
|---|---|---|
| GitHub Container Registry (the default) | [`ghcr.io/trailro/hass-remote-integration`](https://github.com/trailro/hass-remote-integration/pkgs/container/hass-remote-integration) | 0.9.0 |
| Docker Hub | [`trailro26/hass-remote-integration`](https://hub.docker.com/r/trailro26/hass-remote-integration) | 0.16.0 |

```bash
docker pull ghcr.io/trailro/hass-remote-integration:latest
# or
docker pull trailro26/hass-remote-integration:latest
```

Docker Hub limits how often a host that is not logged in may pull; GitHub
Container Registry does not for a public image. Either is fine for installing
and updating one container.

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
# HRI_VERSION=0.23.0      # optional: pin a release (default: latest)
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
| **Entities / Devices / Services** | Inspect, rename, disable, call services (the form sends lists for multiple-choice fields and for text fields that take several values, one box per item, accepts typed custom values (one box per value, sent as typed: a comma or surrounding spaces stay part of it), and checks required fields after the extra JSON is merged) |
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
*Check*. Check resolves the ref to a commit, downloads that commit into a scratch directory, resolves its
Python requirements with `pip --dry-run`, evaluates patches and dependencies
and the minimum HA version, and tells you whether anything blocks the
combination, without installing anything. It leaves nothing on the volume
either: the scratch directory goes when it finishes, and a repository you typed
in yourself is added to the registry by *Prepare*, not by *Check*. Resolving and building packages that
come as source archives runs their build code (`setup.py`, PEP 517 hooks) in the
container, as the install would. *Prepare* installs
exactly the combination that passed, at the commit Check saw: if a branch has
moved since, run Check again, and if GitHub cannot say which commit the ref
points at, Prepare refuses. Check also warns when one of the release's
requirements needs something the image or the host does not provide (a program
or a shared library this image does not carry, the host's Bluetooth stack), when
pip's resolution landed years behind what the requirement allows,
and about configuration the release cannot take over: config entries here while the release has no config
flow, entries at a newer version than its config flow (Home Assistant cannot
migrate an entry back), and YAML stored here that a config flow release will
import. A release that declares a newer minimum Home Assistant in `hacs.json`
cannot be started on an older one; prepare it together with that Home
Assistant version.

### 2. Configure it

Choose whichever fits the integration, on the **Integration** page:

- **Config flow**: runs the integration's own setup dialog, like HA's
  frontend would (the integration must be started first, because the flow is
  its code). Step titles and descriptions, field labels and their hints,
  section names, menu entries, errors, progress messages and abort reasons
  come from the integration's `translations/en.json`, the same file HA's
  frontend reads; an integration that ships none shows the raw schema keys.
  Selectors render as the control they describe: durations, dates, times,
  colours, read-only constants, and a number as a box, or as a slider when it
  asks for one and gives both ends, either way with its unit next to the
  label; a text field that takes several values, and the typed values of a
  multi-select that allows them, show one box per item (an item may contain a
  comma or surrounding spaces and is sent as typed), and a single custom select
  value is sent whole. A multi-select in list mode shows as checkboxes, a value
  typed into a single select replaces the one picked, and a duration part may be
  a fraction. One the page does not know falls back to a JSON textarea saying so.
  A value the page cannot convert (a fraction in a whole-number field, broken
  JSON) is refused with the reason under that field, and nothing is sent.
  Home Assistant keeps a config flow in progress until someone ends it, and
  refuses a second flow for the same device with `already_in_progress`. Every
  flow of this integration that Home Assistant still holds — including one
  started here and left behind, because the page was reloaded or closed
  mid-step — is listed with a **Continue** and an **Abort** button, and
  *Start config flow* ends the flow this page holds before asking for a new
  one. Before, an abandoned flow was invisible here and refused every later
  start until a restart.
- **YAML config**: for integrations configured in `configuration.yaml`, paste
  what would go under `<domain>:`. It is validated on save and applied at boot.
  When a later release imports that YAML into a config entry, a notification
  says so: remove the YAML then, because it is still applied at every boot.
- **Import from your existing Home Assistant** (on **System**): upload a
  standard HA backup (the uncompressed `.tar` Home Assistant writes, encrypted
  or not). The config entries of the installed integration come over with
  their data *and* options, and entity ids, names, icons and disabled flags
  are aligned, so entities keep the same ids they had in your main HA. Both
  directions: an entity you had turned off stays off here, and one its own
  integration ships disabled (a signal strength, a diagnostic) that you had
  turned on is turned on here as it is created — it has no state to wait for,
  so it is aligned from the registry event itself. A
  config entry whose id is not plain letters and digits is skipped. A store
  file belongs to the longest domain of the backup it is named after:
  `foo_bar_tokens` comes with `foo_bar`, never with `foo`. An import is
  refused while another import, or a start, stop or install, is running: the
  import decides as it goes whether the entry is stored enabled, and a stop
  finishing underneath it would leave an enabled entry behind a manager that
  reports the integration stopped. When an import replaces a store file this
  volume already had, the original is kept as `.storage/<store>.pre-import`
  until the import is done, and a restart in the middle of an import puts it
  back. The import is done as soon as its config entry is written to
  `.storage/core.config_entries`: the manager writes that file at once (Home
  Assistant would write it a second later) and only then removes the set-aside
  original, before it deletes the extracted backup. A restart at any point
  leaves either the imported entry with the imported store, or no entry and
  the volume's own store as it was.
  An import never goes next to a config entry the integration already has
  here: *Import* of one entry (`POST /api/import/apply`) is refused with
  "already has a config entry here" (delete that entry on the config flow page
  first; the uploaded backup stays for the retry), and *Import all*
  (`POST /api/import/apply_all`) skips every integration that has entries of
  its own and lists them under `skipped`. An entry imported from this same
  backup before is skipped as "already imported", and the other entries of its
  integration still come over. Import all deletes the uploaded backup once
  nothing failed, skipped entries included, even when everything was skipped:
  to import a skipped integration after deleting its entry, upload the backup
  again.
  The result's `alignment` counts what was aligned at the moment the entry had
  just set up: `entities` and `devices` are the ones that already existed then
  (an entity with a state, or one its integration ships disabled that you had
  turned on). Entities and devices the integration creates later are aligned as
  they are created and are not added to those counters; `pending_entities` and
  `pending_devices` are the map entries still waiting for theirs at that moment,
  so a small `entities` with a large `pending_entities` is normal for an
  integration that adds its entities after setup.

### 3. Start it

Click **Start** on the **Overview** or **Integration** page. The version is
deployed, its requirements installed, patches applied, and the config entries
the manager disabled (on a stop or a switch) are enabled again; an entry you
disabled yourself stays disabled. A backup is taken first when something
changes. Each requirement gets 30 minutes to install: one that takes longer (a
build that hangs, a download that never ends) is stopped together with
everything it started and reported as `pip failed for: <requirement>`, so the
manager does not stay busy until the container restarts. Starting a version other than the deployed one runs its preflight
first (not for a release of an integration without a GitHub repository, but a dev build always; see
[Updating the integration](#updating-the-integration)); blockers ask whether
to start anyway. If a start fails after the new files went out, the files of the
version that was running are put back.

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
notification says it is degraded. An integration with no config entry and no
YAML stored here is not judged: the verdict is `unconfigured`, with no rollback
and no notification. A health check that itself fails (an exception in the
manager, not a verdict) is tried again every minute, three times, and then
recorded as `unknown`, never rolled back. A failed, degraded or unknown smoke test raises a
notification and stays in the last error until another version runs healthy,
also across the rollback's restart. The smoke test judges a *change* once,
minutes after it; for an integration that breaks later, on a version that has
been running fine, see the [health watchdog](docs/health.md#the-health-watchdog), which is
off by default and stands down while a smoke test is pending.

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
HA for a while, with MQTT enabled and discovery off, and compare what the
container announces over MQTT with what your main HA has made of it.

1. Make sure only one side talks to the hardware in a way that conflicts
   (for example, only one side sends commands to the devices).
2. On **Cutover**, give your main HA's URL and a long-lived access token
   (optional). They are used to compare, and by *Enable discovery* to check
   that the main HA no longer has the integration's config entries, and that
   nothing there already holds an entity id this container is about to
   announce. The exception is this container's own mirror of that very entity,
   sitting where the mirror would go anyway; an id held by anything else blocks
   the enable and is named, together with what holds it — an unrelated MQTT
   entity, a leftover of an earlier identity of this container, or a mirror of
   this container you renamed onto that id, which collides with the entity the
   id belongs to exactly as a stranger would. The entity would otherwise arrive
   there with a `_2` id.
   The URL must not contain `user:password@`: the token
   authenticates. Everything the page asks the main HA for is a read, and none
   of it needs an administrator — while a long-lived token carries every right
   of the user who created it, so create it under a dedicated user without
   admin rights. The page matches the entities this container announces over
   MQTT with the MQTT entities your main HA created from that discovery, by
   unique id, and lists what is missing on either side and what differs in
   state, names and flags. The integration's own entities on your main HA are
   not part of the comparison: compare those by hand before the cutover.
3. When you are happy: **remove the integration from your main HA** (delete
   its config entries; disabling keeps its entity ids registered, and the
   Cutover page refuses then), then click *Enable discovery* on **Cutover**.
   The page refuses while something would replace this container's
   configuration at the next restart (a scheduled restore, rollback, version
   switch or import, an action still running) or while a smoke test is
   pending: restart or wait first, or the entities the main HA creates now
   would be replaced.
   Your main HA creates the entities from MQTT; the page watches until all of
   them exist. The entity ids stay the same, but the unique ids are new
   (`hass_<domain>_<entity id>`), so areas, labels and custom names set on the
   removed entities have to be set again.
4. Changed your mind? *Undo* removes every discovery config again, so your
   main HA drops the entities. The manager device stays while `manager_discovery`
   is on: it does not depend on entity discovery, and the Undo answer says
   `manager_device_kept`.

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
is copied onto the volume at boot, next to the old one, and takes its place
only once the copy is complete: on a full volume the copy fails, the old
manager starts, and the log says so — the UI you need to free the disk stays
up. With `HRI_VERSION` pinned, change it first.
Home Assistant writes its registries last when it stops, so the compose file
gives the container 240 s to stop (`stop_grace_period`; a stop that hangs
ends the process on its own after about 235 s, before Docker kills it) and runs an init process; with plain
`docker run`, add `--init --stop-timeout 240 --restart unless-stopped`. The
restart policy is required: a restart from the UI or MQTT exits the process
and relies on Docker to start it again. A `docker-compose.yml` downloaded before 0.14.0
still says 120 s: download it again (or change `stop_grace_period`) when you
update. One downloaded before 0.18.0 does not pass `HRI_APT_PACKAGES` on, so
setting it in `.env` does nothing at all: download the compose file again (the
command in *Quick start*) when you update.

Since 0.19.0 the image carries a healthcheck, so `docker ps` says `healthy` once the manager
API answers and `unhealthy` when it stops answering, and
`depends_on: condition: service_healthy` works. Unlike the two settings above
this one needs no new compose file: a service that does not define
`healthcheck:` itself inherits the image's, whatever the compose file's age. It
asks `GET /api/status` on `HRI_PORT` every 30 s with the image's own Python, and
holds off for the first 20 minutes, which is where the first install of Home
Assistant fits (see *Troubleshooting*).

The top bar shows the version that runs and the commit its image was built
from (`v0.23.0 · 1a2b3c4`), linking to that release. When GitHub has newer
releases than the one running (checked with the other update checks), a banner
under the top bar says so and links the release notes of each newer release,
newest first. Hiding it applies in that browser only, until a newer release is
published.

With a password set, 0.11.0 changes the session cookie format: log in once
after updating. Going back to an image older than 0.11.0 is possible (the
volume stays readable), but that older image honours logouts only by time, so
a session you logged out of since the update can work again there until it
expires or you log out once more.

### Updating the integration

Install the new release (Install or Integration page), then *Switch to* it.
The switch runs the **Preflight** on the copy in the version store first (the
files the switch deploys, not what the tag or branch names on GitHub now), or
reuses its own check of that same copy from the last 30 minutes (kept in
memory, per stored copy and running Home Assistant version, so a restart or a
reinstall forgets them; the *Preflight* button checks the release on GitHub and
is not reused). If the version is installed again while its check runs, the
start is refused: start it again. Blockers stop the switch and
are listed with a choice to start anyway; warnings do not stop it (a requirement
that needs a program, a library or the host's Bluetooth stack the container has
not got, and a resolution pip backtracked years behind, are warnings). An update
started from your main HA over MQTT refuses on blockers, since nobody is there
to confirm, and says why in its result. Through the API, `POST /api/run/start`
answers `needs_force` with the report, and `force: true` starts anyway; a
version that is not in the store is refused plainly, with nothing to force.
Starting the version that is already deployed, a release of an integration
without a GitHub repository, a rollback and a restore skip the preflight. A dev
build does not, also one of a domain that has no repository (a new domain
installed from the dev directory is registered without one): the check reads
the copy on the volume, never GitHub, so an uploaded tree is as
checkable as a release, and its report is keyed on when that copy was
installed, so the next upload is checked again. A
preflight that cannot run (GitHub is unreachable, for example) does not stop the
start; the API result then says why in `preflight_note`, and a start that passes
with warnings carries them in `preflight_warnings`. A stored copy with no
`manifest.json` for the domain is the exception: that is not a transient failure
but a copy there is no point deploying, and it blocks like any other. The manager backs up,
switches, restarts if needed, smoke-tests, and rolls back on its own if the new
version does not set up; a degraded version is kept and reported. *Full
rollback* on the Integration page brings back the previous version together
with the config as it was before the update; its restart is
smoke-tested too, without a further automatic rollback. The previous version is
the one that actually ran: a switch that never got its restart never loaded, so
switching twice in a row leaves the Full rollback target, and the backup that
goes with it, at the version the process is still running. One full rollback runs at
a time: a second one (a double click, or a manual one while the automatic one
runs) is refused. A full rollback is also refused while a Home Assistant version change, an
install or a restore is being prepared, and while a switch with a configuration
restore or a clean start is scheduled; the smoke test's automatic rollback waits
for those three instead. Once a full rollback has scheduled its restore, a
restore by hand is refused until the restart finishes it, and so is *Cancel
restore*: the rollback already selected the previous version, which must not
start on the configuration the newer one migrated. To undo a full rollback
before that restart, start the version it left again: that start drops the
rollback's restore, and the previous version stays the
Full rollback target as before. Any other start is refused until the restart, and so
are *Stop*, *Uninstall*, a second full rollback, an install and removing the
version it goes back to or the one it left: each answer says to restart to
finish the rollback, or to start the version it left to undo it. After an automatic
rollback there is no Full rollback target: the version the smoke test rejected
is never offered again that way.

A full rollback records the version it goes back to before it schedules the
restore, so one cut off halfway (`docker stop`, a power loss) finishes at the
next boot instead of putting the rejected version back on the restored
configuration. If the restore did not happen at all (dropped, or failed),
the rollback is given up: the integration stays on the
version it was running, and the reason goes on the timeline. The backup a full rollback
restores is kept from pruning and refused for deletion until that restore is
over: applied, failed, dropped at the boot, or undone by starting the version
the rollback left.

A boot that finds an installed integration's config entries enabled and its
copy deployed while nothing is recorded as running (a restore that brought back
the entries of a backup taken while it ran, after a stop) records it as running,
as after a damaged `state.json`: that boot sets its entries up anyway, and
*Stop*, the health verdict and the MQTT identity then describe what runs. The
deployed version comes from the marker next to the code; the timeline and the
log say it was adopted. When more than one installed integration is in that
state, none is adopted and the log says so.

A downgrade of the integration after its config entries were migrated to a
newer format usually fails (`migration_error`), which the preflight warns
about. Full rollback right after the upgrade brings the entries back as they
were.

Once the new version has run (after the smoke test), *What changed between
versions* on the Integration page compares its entities and services with those
of the version before. A degraded version is kept, so it gets that report too
(an entity with no state at all counts as removed there); a version that did not
set up gets none. The report lists entities added, removed or renamed, entities whose unit,
device class, state class or category changed, and services or service fields
added or removed. State attributes are not compared. An entity that only gains
or only loses its unique id in the new version, under the same entity id, counts
as the same entity in both directions, not as one removed and one added. Removed, renamed or changed entities and removed services or
fields are what break automations in your main HA, so they also raise a
notification. The last ten reports are kept.

### Updating Home Assistant inside the container

<!-- SUMMARY home-assistant-versions.md -->
See [Home Assistant and Python versions inside the container](docs/home-assistant-versions.md).

### Backups

<!-- SUMMARY backups.md -->
See [Backups and restore](docs/backups.md).

### Health

<!-- SUMMARY health.md -->
See [Health and the health watchdog](docs/health.md).

### Logs and log files

<!-- SUMMARY logs.md -->
See [Logs and log files](docs/logs.md).

### Replacing the integration

Installing a different integration in a container **replaces** the current
one: after a backup, its config entries, versions, patches, YAML and retained
MQTT documents are removed (a cleanup the broker does not take is kept and
retried as for an uninstall, see *MQTT reference*; the install's answer does
not report it). The UI asks before doing it. To run two
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
- **Bluetooth** is not a package problem: `bleak` and the rest install and
  import in any container, and then find no adapter. The container needs the
  host's Bluetooth stack: a running `bluetoothd`, the host's D-Bus system
  socket (`volumes: ["/run/dbus:/run/dbus:ro"]`), host networking and
  `NET_ADMIN`/`NET_RAW`. Even then the adapter is shared with the host.
  The preflight warns about a requirement that needs it while the socket is not
  mounted.

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
when its context matches, never leaves broken Python behind; a hunk that only
adds or removes lines without a single context line, as `diff -U0` makes, is
refused because it cannot be located; each hunk is looked for from its own line
shifted by the hunks before it, like GNU patch, and a hunk whose lines occur in
more than one place about as near is refused as ambiguous; a patched block
that also exists as a twin still counts as applied, unless an unpatched copy is
about as near too). A diff's file paths may carry one `a/`, `b/` or `./` prefix
and then name the file relative to the integration's own directory
(`a/const.py`), as `custom_components/<domain>/…` (`a/custom_components/<domain>/const.py`),
or relative to site-packages for a library (`a/some_lib/module.py`); the
integration's domain on its own (`a/<domain>/const.py`) is none of these and
reports the file absent. Two optional
headers retire a patch on its own:

```python
# integration-version: 1.2.0, 1.2.1   only for these versions
# applies-to: some-lib<2.0            only while this requirement matches
```

The Integration page has an editor: *New .py patch* and *New .patch diff* start
from a template, *Edit* opens an existing patch (a bundled one is saved as your
copy under the same name). *Check* changes nothing: for a diff it shows where
each hunk lands, `ambiguous` for a hunk that matches more than one place about
as near, or, when its context is gone, the closest lines in the file
next to what the hunk expects; a module runs its `status(ctx)`.

When a patch stops fitting the code it targets (upstream changed it, a file is
gone, the module fails), the integration still starts without it and a
notification on the Overview says which patch and why. It goes away once every
patch applies again. It is worked out again when patches are applied (a start,
a boot, *Apply*) and when one is deleted; uploading or saving a patch changes
it only at the next of those.

A patch retired by its own headers is listed as `skipped` and is nothing to act
on — unless it patched an installed library rather than the integration's own
files. Those files are not redeployed by a version change, so the change is
still in them: the row says `skipped, still applied` and what to do about it
(reinstall that distribution, or switch back and delete the patch). The
integration's own files come back with every version change, so a patch of those
that no longer applies is simply gone.

---

## For integration authors: dev mode

Test an integration from your working copy without publishing a release:

```bash
HRI_DEV_SRC=/path/to/your/checkout docker compose \
  -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.dev.yml up -d
```

(With explicit `-f` files Compose no longer loads the override on its own:
list it, or leave it out if you do not have one.)

The directory is mounted read-only at `/dev-src` (the `dev_source_dir` setting,
`POST /api/settings`, points it at another absolute path in the container when
your own mount puts the checkout elsewhere). The **Install** page lists
every `manifest.json` it finds there (except the manager's own
`integration_manager`); *Install as local* copies it into the
version store as version `local`, which you start like any other. *Reinstall +
restart* refreshes the running copy after you edit the code. Symbolic links in
the directory are skipped, never followed, and the limits of a release archive
apply (300 MB, 20000 files); `__pycache__`, `.git`, `.mypy_cache` and
`.pytest_cache` are left out.

`HRI_DEBUGPY=5678` (set by the dev overlay) makes the process listen for a
debugger: attach VS Code to `localhost:5678`. debugpy binds `127.0.0.1` inside
the container unless `HRI_DEBUGPY_HOST` says otherwise; the dev overlay sets it
to `0.0.0.0`, because a published port cannot reach the container's loopback.
The overlay publishes the port on the host's `127.0.0.1` only, but inside
Docker every container on the same network can reach it, and debugpy has no
authentication: whoever connects runs code in the container. Use the dev
overlay only on a Docker network you trust.
Exceptions show up on the **Logs** page.

`HRI_DEBUG=1` also turns on Home Assistant's blocking-call detection (off
otherwise): file, directory and import calls on the event loop are logged
with the line that made them, and `time.sleep` or a blocking HTTP request on
the loop raises, as in a regular Home Assistant.

---

## MQTT reference

<!-- SUMMARY mqtt.md -->
See [MQTT reference](docs/mqtt.md).

## Security

<!-- SUMMARY security.md -->
See [Security: what the container protects, and how](docs/security.md).

## Configuration reference

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `HRI_PORT` | `8087` | Port of the UI and API; a changed port is picked up at the next boot, and one pinned in `.storage/http` by an older setup or a restored backup is dropped; a value that is not a port number (1-65535) stops the container at boot with a line in the log. The image's healthcheck reads it too, so a changed port needs nothing else |
| `HRI_NAME` | `hass-remote-integration` | Container and volume name |
| `HRI_VERSION` | `latest` | Image tag Compose pulls, for example `0.23.0` |
| `HRI_REGISTRY` | `ghcr.io/trailro` | Where Compose pulls the image from: `ghcr.io/trailro` (GitHub Container Registry) or `docker.io/trailro26` (Docker Hub); the same image either way. A `docker-compose.yml` from 0.16.0 or older ignores it and pulls from GitHub Container Registry: download the file again to use it |
| `TZ` | `UTC` | Time zone; an unknown zone falls back to UTC, with an error in the log |
| `HA_VERSION_LATEST` | `1` | `0` installs the image's default Home Assistant (`HA_VERSION_DEFAULT`, the version the image was built with) on a fresh volume instead of the newest |
| `HRI_APT_PACKAGES` | unset | Debian packages the container installs at boot, before Home Assistant starts, for what pip cannot install (the `ffmpeg` binary, BlueZ): names separated by spaces or commas, for example `ffmpeg libpcap0.8t64`. Only Debian package names are accepted (`libc6:arm64` too); anything else — an option, a URL, a path, a shell metacharacter, or a name ending in `-` (`libturbojpeg0-`), which is apt's own *remove* operator and not part of any package name — is refused with a line in the log and nothing is installed for it, and the value never reaches a shell. A name ending in `+` (`g++`) is fine: that character really is part of Debian package names. Do not add a `+` that is not part of the name: apt reads `ffmpeg+` as `ffmpeg` and installs it, but the check for what is already installed looks for a package literally called `ffmpeg+`, finds none, and runs apt again at every boot. Each name must be the exact name of a package: apt is told not to read one as a pattern, so a typo such as `python3.1.` fails like any unknown package instead of installing every package whose name it happens to match. Packages that are already installed are not installed again, so a restart costs nothing; they live in the container, not on the volume, so recreating the container or updating the image installs them again. A failure (no network, an unknown package) is recorded on **System** and does not stop the boot. The output is in `integration_manager/apt-install.log`. A `docker-compose.yml` from 0.17.2 or older does not pass it to the container: download the file again to use it |
| `HRI_DEV_SRC` | `./dev-src` | Dev mode: directory mounted at `/dev-src` |
| `HRI_DEBUGPY` | unset | Dev mode: debugger port |
| `HRI_DEBUGPY_HOST` | `127.0.0.1` | Dev mode: address debugpy binds inside the container (the dev overlay sets `0.0.0.0`) |
| `HRI_CALL_TIMEOUT` | `60` | Seconds a service call (over MQTT and from the Services page or `POST /api/services/call`) or a command may take before it is reported as a timeout (a whole number; a value that is not one logs a warning and uses 60, one below 1 uses 1) |
| `HRI_TRACEMALLOC` | unset | Diagnostics: allocation tracing frames (costs memory); a value that is not a number traces 25 |
| `HRI_TRACE_IMPORT` | unset | Diagnostics: log who imports the given packages |
| `HRI_DEBUG` | unset | Debug logging for the manager, and blocking-call detection on the event loop |
| `HRI_PASSWORD` | unset | Password for the web UI and API; unset or empty means no login, only spaces or tabs keeps the UI closed until it is fixed |
| `HRI_PASSWORD_FILE` | unset | File holding the password, for example a Docker secret; wins over `HRI_PASSWORD`, and must not be empty |
| `HRI_COOKIE_SECURE` | unset | `1` marks the session cookie `Secure` (behind a reverse proxy with TLS) |

### Files on the volume

<!-- SUMMARY files.md -->
See [Files on the volume](docs/files.md).

### API

<!-- SUMMARY api.md -->
See [API](docs/api.md).

## Troubleshooting

- **The page keeps showing the installation progress.** The first start
  downloads Home Assistant; a slow connection can take several minutes.
  Without a password the page shows pip's progress; it is also in
  `integration_manager/ha-install.log` on the volume, while the container log
  shows only the start and end of the install. From the start of the
  container until Home Assistant is started (the system packages of
  `HRI_APT_PACKAGES`, the PyPI lookup, the install, the manager's requirements,
  a scheduled restore, removing unused venvs) the
  page and every `/api/` path answer `503` with a `Retry-After: 5`, and under
  `/api/` with a JSON body naming the phase (`phase`) and the seconds since the
  container started (`elapsed`), so the image's healthcheck does not call the container healthy while there is no
  manager API yet. `docker stop` during these steps stops pip and exits at
  once. A version in `integration_manager/ha.json` that is not a Home
  Assistant version number (edited by hand) is ignored and logged. A slow install
  runs as long as it keeps making progress; an install that writes nothing at
  all for 15 minutes is taken for hung and fails: the container starts the Home
  Assistant version it already had, or, on a first start, exits and Docker
  starts it again.
- **`docker ps` says the container is unhealthy, or stays `starting`.** The
  healthcheck asks the manager for `GET /api/status` on `HRI_PORT` from inside
  the container. `starting` is the first 20 minutes, which covers the first
  install described above: while the entrypoint answers `503` the container is
  not healthy, and the first answer of the manager itself makes it healthy at
  once. `unhealthy` after that means the manager stopped answering for three
  probes in a row (about 90 s); `docker inspect --format '{{json
  .State.Health}}' <name>` shows what the probe got, and `docker logs <name>`
  why. A password changes nothing: the `401` of `/api/status` is the manager
  answering. To see the probe's own error, run it by hand:
  `docker exec <name> python -c "import http.client, os;
  c = http.client.HTTPConnection('127.0.0.1', int(os.environ.get('HRI_PORT') or 8087));
  c.request('GET', '/api/status'); print(c.getresponse().status)"`.
- **The page says Home Assistant is not started: a restore failed and could not
  be put back.** The configuration is half restored and the page names the
  backup that holds the configuration from before. Free space or fix the error
  shown in `docker logs <name>`; the restore is retried every 5 minutes. To
  start on the configuration as it is, delete
  `integration_manager/restore-pending.json` on the volume.
- **The integration needs `ffmpeg` or another system package.** Name the
  Debian packages in `HRI_APT_PACKAGES` (see *Environment variables*) and
  restart the container: the entrypoint installs them before Home Assistant
  starts, and the page shows that step like the other boot phases. What apt
  printed is in `integration_manager/apt-install.log` on the volume, and
  **System** shows, next to the venvs, what this boot did with the variable: the
  packages, whether they were already installed, the names it refused (anything
  that is not a Debian package name) and the error of a failed install. Nothing
  of this stops the boot — Home Assistant starts without the packages and the
  integration that needs them fails where you can see it. Bluetooth needs more
  than a package: the container also has to reach the host's adapter and D-Bus,
  which no package can give it.
- **A Home Assistant version is refused as "unlikely to install".** Its pinned
  requirements have no wheel for this image's Python (the message names them),
  and the image carries no compiler, so pip would fail minutes into the install
  and the container would fall back to the version you came from. Choose a
  newer version, or add a compiler with `HRI_APT_PACKAGES=build-essential`,
  recreate the container and confirm the version anyway when the page offers to
  — building those packages takes a long time and needs the memory for it. A
  version that says *could not check* is not refused: the resolution could not
  answer (PyPI unreachable, or pip gave up on the dependency graph), and the
  install may well work.
- **A Home Assistant version is refused as "older than this image's floor".**
  The image is built with a floor (`HA_VERSION_MIN`, 2026.5.0 here) and
  refuses everything below it outright. This is not the pin check, and
  *Schedule anyway* is not offered. A venv of that version already on the
  volume changes nothing: it is refused as well. The container keeps booting
  the version it runs, and a rollback to the previous version still works.
  Below that floor Home Assistant's own pinned
  requirements have no wheel for this image's Python, so a lower floor would
  only move the refusal to the pin check: what is needed is an image with an
  older Python, not a different build argument.
- **"restart required" does not go away.** Click *Restart process* on the
  Overview; some changes (a new version of a loaded integration, YAML) only take
  effect at a restart. A reference that moved counts as a new version:
  preparing `main`, a branch or a dev build again gives the same name different
  code, which the running process cannot pick up on its own — the start says a
  restart is required, and the smoke test waits for it rather than judging the
  code that is still loaded.
- **Restart process does nothing.** A restart is refused while the manager is
  busy: an install, start, stop or uninstall, an import, a clean-start rebuild
  after a Home Assistant downgrade, a full rollback, a
  restore being scheduled or cancelled, a backup, a patch being applied, a Home
  Assistant version change, the self-check right after boot, or a restart
  already under way. The page shows the error. Wait for it to finish
  and restart again. A full volume does not refuse it: the state file holds a
  badge and a line of history, and freeing the disk is often what the restart is
  for, so a write that fails is logged and the restart goes ahead. A restart
  that fails for another reason before anything stops is refused the same way,
  with the reason. Once a restart is accepted, the process gives Home
  Assistant about 225 s to stop (the same total a `docker stop` allows: there
  the count of 205 s starts once Home Assistant's first stage, up to 20 s, is
  over) and then
  exits anyway, so a stop that hangs
  still ends in a restart rather than in a container that is up and
  unreachable.
- **MQTT says the base topic is in use.** Something else left retained messages
  under `hass_<domain>/`. Remove them, or tick `force_base_topic` if they are
  yours from an earlier setup.
- **MQTT reconnects every few seconds, "closed the connection ... after
  accepting it".** Either a document over the broker's maximum packet size
  (the log names it), or another client connecting with the same client id
  `hass_<domain>`: usually a second container running the same integration on
  that broker. The two cannot share one broker; see the start of this README.
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

`verify.sh` reads `HRI_NAME`, `HRI_PORT`, `HRI_IMAGE`, `HRI_NETWORK`, `HRI_PASSWORD`
(or `HRI_PASSWORD_FILE`, which wins) and `TZ` from the environment or from `.env`.
`start` exits non-zero when the API does not come up (a timeout or a restart
loop). `HRI_ENV="K=V K2=V2"` passes more variables to the container, which is
how CI boots a version other than the newest (`HA_VERSION_LATEST=0
HA_VERSION_DEFAULT=<version>`).

`custom_components/integration_manager/ha_compat.json` says, for every MQTT
platform and every device class, the oldest Home Assistant that has it. It is
generated, not written: `python3 tools/gen_ha_compat.py` downloads one Home
Assistant wheel per minor release (2025.1 and newer — below 2024.11 no
discovery arrives at all), reads the platforms out of `mqtt/const.py` and the
device classes out of each domain's `const.py` or `__init__.py`, and writes the table with a
stamp of what it scanned and when. It needs network access and a few minutes.
Re-run it when a new Home Assistant release is out; until then a main Home
Assistant at or above the newest release in the table is treated as knowing
everything, which is what it was before the table existed.

CI runs on every push to `main` and every pull request: syntax checks, then,
natively on both `amd64` and `arm64`, an image build, a boot on a fresh volume,
the discovery schema test and the unit tests — and on `amd64` the same boot
again on the image's default Home Assistant and on its floor, each checked to
have installed the version it was given rather than having fallen back to
another. A weekly workflow repeats all of it against the newest stable Home
Assistant and the newest pre-release, opening an issue when one of them breaks
the manager and a pull request moving `HA_VERSION_DEFAULT` when a newer stable
passes. Publishing
a release builds the `amd64` and `arm64` image and pushes it to
`ghcr.io/trailro/hass-remote-integration` and to Docker Hub as
`trailro26/hass-remote-integration` (`<version>`, `<major>.<minor>` and, for the
newest stable release, `latest`; the Docker Hub push needs the `DOCKERHUB_TOKEN`
repository secret). The newest stable release also updates the Docker Hub
overview from this README, up to *Everyday operation*.

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
- With a password set, Home Assistant's webhooks and other unauthenticated
  callbacks on the port are refused too (`401`); there is no allowlist for them.
- One integration per container; two versions of the same integration cannot
  run at the same time.
- No Home Assistant frontend: integration features that exist only as frontend
  panels are not available.
- Discovery of entities without a `unique_id` works, but they cannot be
  renamed or disabled in the registry.
- Cutover does not remove the integration from your main HA for you: do that
  yourself before enabling discovery (the Cutover page checks it when the main
  HA is configured).
- The main Home Assistant has its own floor: below 2025.10 the entities come
  up with generated ids instead of their own, and below 2024.11 nothing arrives
  at all (see *What the main Home Assistant needs*). Nothing in the container
  can detect which version the main instance runs, so neither case is reported
  anywhere — the entities simply look wrong, or never appear. `main_ha_version`
  does not help with either: it filters platforms and device classes, which is
  not what breaks in those two cases.
- The Python version is fixed by the image. Home Assistant versions or
  integrations that need another Python cannot run until a release moves the
  image to that Python. An image with a newer Python is tested against Home
  Assistant and the manager before release, not against every integration: after
  updating hass-remote-integration, run the preflight on the integration you
  use. How far back Home Assistant can go is set by the image's floor
  (`HA_VERSION_MIN`, which no force lifts), and that floor is the oldest
  release whose pinned requirements still have wheels for this Python (see
  *Python versions*).
- Packages without a wheel for your architecture that need a compiler (C, Rust)
  cannot be installed. Wheels differ between amd64 and arm64, so an integration
  can install on one and not the other.
- Packages that load native system libraries (`libusb`, `bluez`, codecs) need
  those libraries in the image. The image carries `libturbojpeg`, which Home
  Assistant's camera component wants; `HRI_APT_PACKAGES` adds what a particular
  integration needs, installed at boot. The preflight warns about the packages
  it knows (`_SYSTEM_DEPS` in `preflight.py`); any other missing library shows
  up when the integration loads.
- Renaming an entity here recreates it on the consuming Home Assistant. The
  discovery `unique_id` is derived from the entity id, so a rename looks like a
  different entity to the consumer: the old one is deleted and a new one is
  created, without the area, the custom name and the hidden flag it was given
  there. Rename before cutover, or leave the entity alone and set the name the
  consumer sees with an MQTT rule.
- An entity hidden here stays visible on the consuming Home Assistant. MQTT
  discovery has no `hidden` option, and the MQTT rules have no such field; the
  registry's `hidden` flag is published in the entity document, but nothing on
  the other side reads it. `enabled_by_default: false` (an MQTT rule) is the
  only way to keep an entity out of the way there.
- The code checks read the source only. Modules imported dynamically
  (`importlib`, `__import__`), code that behaves differently on this Python at
  run time, and incompatibilities inside requirements are caught by the smoke
  test, not by the preflight: a version that does not set up is rolled back,
  a degraded one is kept and reported.

## License

[Apache License 2.0](LICENSE). Home Assistant and the integrations you run
with this tool keep their own licenses; they are downloaded at runtime and not
part of this repository.
