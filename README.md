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
  topic `hass_<domain>`, discovery ids `hass_<domain>_...`. Several containers
  share one broker and one main HA without clashing.

---

## Requirements

- Docker with Compose.
- An MQTT broker reachable from the container (mosquitto or any other).
- Your main Home Assistant with the MQTT integration, if you want the entities
  to appear there — **2025.10 or newer**, and 2026.5 or newer if the integration
  you mirror has a `date`, `time` or `datetime` entity (see *What the main Home
  Assistant needs*). The Home Assistant inside the container is a different
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
# HRI_VERSION=0.21.0      # optional: pin a release (default: latest)
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
- **YAML config**: for integrations configured in `configuration.yaml`, paste
  what would go under `<domain>:`. It is validated on save and applied at boot.
  When a later release imports that YAML into a config entry, a notification
  says so: remove the YAML then, because it is still applied at every boot.
- **Import from your existing Home Assistant** (on **System**): upload a
  standard HA backup (the uncompressed `.tar` Home Assistant writes, encrypted
  or not). The config entries of the installed integration come over with
  their data *and* options, and entity ids, names, icons and disabled flags
  are aligned, so entities keep the same ids they had in your main HA. A
  config entry whose id is not plain letters and digits is skipped. A store
  file belongs to the longest domain of the backup it is named after:
  `foo_bar_tokens` comes with `foo_bar`, never with `foo`. An import is
  refused while another import, or a start, stop or install, is running: the
  import decides as it goes whether the entry is stored enabled, and a stop
  finishing underneath it would leave an enabled entry behind a manager that
  reports the integration stopped.

### 3. Start it

Click **Start** on the **Overview** or **Integration** page. The version is
deployed, its requirements installed, patches applied, and the config entries
the manager disabled (on a stop or a switch) are enabled again; an entry you
disabled yourself stays disabled. A backup is taken first when something
changes. Starting a version other than the deployed one runs its preflight
first (not for an integration without a GitHub repository; see
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
been running fine, see the [health watchdog](#the-health-watchdog), which is
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
   that the main HA no longer has config entries or entity ids of the
   integration. The URL must not contain `user:password@`: the token
   authenticates. The page matches the entities this container announces over
   MQTT with the MQTT entities your main HA created from that discovery, by
   unique id, and lists what is missing on either side and what differs in
   state, names and flags. The integration's own entities on your main HA are
   not part of the comparison: compare those by hand before the cutover.
3. When you are happy: **remove the integration from your main HA** (delete
   its config entries; disabling keeps its entity ids registered, and the
   Cutover page refuses then), then click *Enable discovery* on **Cutover**.
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
is copied onto the volume at boot. With `HRI_VERSION` pinned, change it first.
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
from (`v0.21.0 · 1a2b3c4`), linking to that release. When GitHub has newer
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
Starting the version that is already deployed, an integration without a GitHub
repository, a rollback and a restore skip the preflight. A dev build does not:
the check reads the copy on the volume, never GitHub, so an uploaded tree is as
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
smoke-tested too, without a further automatic rollback. One full rollback runs at
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
never did. A
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
version a fresh volume installs (`HA_VERSION_DEFAULT`, 2026.9.3) — the floor is
the oldest release the manager was measured on, the default is a recent one to
start from. The second rule applies above the floor: an older release pins
requirements published before this image's Python existed, PyPI has no wheel
for them and the image has no compiler, so the manager resolves the chosen
version's pins itself — nothing is installed,
the answer is cached for an hour — and refuses a version whose requirements
cannot install, naming the package. A check can take minutes on an old version,
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
  The rebuild holds the manager like an import: an install, start, stop,
  uninstall, full rollback, restore, Home Assistant version change or process
  restart is refused while it runs (try again). It waits for an action that is
  already running
  and then rebuilds only if the integration still runs.
- **Keep the current configuration.** This works when the older version can
  read the newer storage formats; otherwise the boot fails and the container
  falls back to the version you came from.

The integration version and the manager state stay as they are in every case.

### Python versions

The container has one Python interpreter, the image's (`python:3.14`). Home
Assistant, the integration and every package it requires run on it, so all
three have to support that Python:

- **The image's floor.** The image is built with two Home Assistant versions,
  both build-time `ARG`s: `HA_VERSION_MIN` (2026.5.0 here) is the oldest
  release it installs at all, and `HA_VERSION` → `HA_VERSION_DEFAULT`
  (2026.9.3 here) is what a fresh volume installs when it is not told to take
  the newest, and the fallback when PyPI cannot be reached. Anything older
  than the floor is refused outright, before any of the checks below and with
  no force path. A venv of an older version already on the volume can still be
  switched to, and a rollback to the recorded previous version is not blocked
  by it. Both are booted in CI on every change — the newest stable Home
  Assistant, the default and the floor, each on a fresh volume with the
  discovery schemas and the unit tests run against it — so neither number is a
  claim nobody checks. A weekly job does the same against whatever Home
  Assistant is newest that week and against its newest pre-release, so a
  release that breaks the manager is found here rather than by you. The floor
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
  access](#hardware-access) for what a compose file has to give them.
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

### Backups

Taken automatically before every start that changes something, before every
Home Assistant version change, before a restore and before replacing the
integration; optionally daily. On **System**
you can create, download, upload, delete and restore them. A restore is
applied at the next restart, can be partial (only `.storage`, only the manager
state, …), and is rolled back if it fails halfway. *Cancel restore* cancels a
restore scheduled by hand; a restore that belongs to a scheduled Home Assistant
version change is cancelled together with that change (choose the running
version under Home Assistant), and cancelling it on its own is refused; a full
rollback's restore is refused too (restart to finish the rollback, or start the
version it left to undo it). A
restore and *Cancel restore* are refused (try again) while a Home Assistant
version change, a full rollback, an install, a start or stop, or an import is
being prepared or running, and a version change is refused while a restore or a
full rollback is being scheduled or a restore is being cancelled.
Restoring the YAML part also
removes root `*.yaml` / `*.yml` files that are not in the backup, so a file
created after it (a `secrets.yaml`, for example) does not survive the restore.
A restore that fails (a full disk, a file that cannot be written) puts the
previous configuration back and is not retried: the schedule is dropped and
the outcome is `failed` (if even the outcome cannot be written, the next boot
records it and still does not try again). A restore whose pre-restore backup
cannot be recorded in the schedule does not start. A restore cut off halfway
(`docker stop`, a power loss, Ctrl-C) stays scheduled, and the next boot
applies it again from the same pre-restore backup, or puts that backup back
if it fails again. If even putting the configuration back fails, Home
Assistant is not started on the half-restored configuration: the manager port
shows a status page naming the pre-restore backup, the restore is retried
every 5 minutes until it applies or is put back, and that backup is kept from
pruning and cannot be deleted. Deleting `integration_manager/restore-pending.json`
ends the wait and starts Home Assistant on the configuration as it is. A
restore replaces symbolic links inside the trees it restores with real files
and directories instead of writing through them, and does not start when
`.storage`, `custom_components` or `integration_manager`, of the parts being
restored, is itself a symbolic link; putting the previous configuration back
after a failed restore follows the same rule. A scheduled restore whose copy
of the backup is gone from the volume (`integration_manager/restore-pending-*.zip`
deleted by hand) is dropped at the next boot and recorded as failed, so it
neither protects its backup nor holds up a version change. A backup holds at
most 100000 files: taking a larger one fails, and an upload or restore of one
is refused (counted from the archive's directory before it is read, and not
listed with its details). Backups, restored files and
uploads are created readable by the container user only (umask 077).
A backup takes regular files only: a named pipe, socket or device in the backed-up
trees is skipped with a line in the log, and so is a symbolic link to a directory
(`custom_components` itself included). A symbolic link to a file is stored as that
file only when it points at a file a backup holds anyway (inside `/config`, in the
backed-up trees, not excluded); a link out of the volume, to the login key or to
another backup is skipped with a line in the log. What a backup reads is never
outside `/config`, and a special file never holds it up.
Automatic pruning keeps the newest backups by the date they were made (never
later than the file's own date), never removes the backup it runs after, and
leaves uploaded backups, and every copy taken before a restore (`<time>-pre-restore.zip`), alone for their
first 7 days; while that week lasts such a copy is also refused for deletion, since
it is the only way back once the restore has succeeded and its schedule is gone.
The backups pruning leaves alone still count toward the number kept: with
*keep 5*, a backup made by hand is one of the 5 newest, not a sixth. The backup a
restore came from is pruned and can be deleted like any other once that restore
is over (applied, failed and put back, or dropped at the boot). An upload never replaces
an existing backup: a name already taken gets a `-2`, `-3`, … suffix (a long name is
shortened to make room for it). An upload that cannot be written (a full volume)
answers with the reason and leaves no partial file behind; so does the upload of a
Home Assistant backup for an import.
A restore never rolls back the record of what happened: the timeline, the
resource history, the change reports and the last known release versions are
not part of backups, and neither are the login key and the logout record, the
port Home Assistant was set up with (`.storage/http`, which would pin a foreign
port when the backup comes from a container on another `HRI_PORT`; a restored
one from an older archive is dropped at the next boot), a store file Home
Assistant is writing at that moment (`.storage/tmp…`) or an original an import
set aside (`.storage/*.pre-import`, and `*.pre-import.done` once the import
completed). Nor are the records of what the broker holds (`mqtt_identity.json`,
`mqtt_cleanup_pending.json`): the broker is outside the volume, so an older copy
would forget retained data still there or clear data published since. A backup whose file
names are not in their plain form (`./`, `//`, `..`) is refused.

Every backup records the Home Assistant version it was made on (the *HA* column),
and Home Assistant only migrates a configuration forward. Restoring a backup
made on an older version asks what to do: keep the running Home Assistant (the
default: the configuration is migrated forward when it starts) or go back to the
version the backup was made on, for exactly the state of the backup. A backup
made on a newer version can only be restored together with a switch to that
version. A backup that does not record its version (or records something that
is not a version number, such as `unknown`) is only restored with
`.storage` after a confirmation ("restore anyway", `"force": true` in the API
body), since it may come from a newer version. The version only matters when `.storage` is restored: a partial restore
without it never changes Home Assistant. A switch installs the version at the restart if its venv is no longer
on the volume (only the current and the previous one are kept), takes a backup
of the current configuration first, and brings it back if that version does
not start.

### Health

`hass_<domain>/health` carries a verdict: `ok`, `degraded`, `error` or
`stopped` (nothing is running), with the
reason, entity counts and when the integration last wrote a state. With
discovery on, your main HA gets a connectivity sensor and a health sensor for
the container. The thresholds are on the **MQTT** page; mark an integration
that only writes on events as `event`, so silence is not reported as a fault.

#### The health watchdog

An integration that goes into `error` at three in the morning stays that way
until somebody looks. The **health watchdog** (on **System**, *off by
default*) restarts the process when the verdict has been `error` without
interruption for a while: 15 minutes by default, at most once an hour and at
most three times a day. Only `error` counts — a `degraded` version is kept on
purpose and `stopped` is your decision. An integration that is installed but
not configured yet also reports `error` (`not loaded (no config entry, no YAML
setup)`); a restart cannot configure it, so the watchdog leaves that one alone.

It never fights the rest of the manager. Nothing is restarted while an
install, start, stop, backup, import, restore or full rollback is running,
while no integration runs or Home Assistant itself is not running yet,
while a restore, a rebuild, a Home Assistant version change, a deferred start
or a full rollback is waiting for the next restart, while a smoke test is
pending or a config entry is still setting up, nor in the first 15 minutes
after a boot — the integration gets its whole grace window to set up first. A
restart it decided against puts one line on the timeline for that stretch, not
one a minute.

It cannot loop. After a restart the clock starts again from that boot, and the
window doubles for the next attempt (15 → 30 → 60 → 120 → 240 minutes, where
it stops doubling), so a
restart that did not help is not repeated at the same rate. When the daily
maximum is reached it gives up, says so once, and waits: an `ok` verdict
resets the ladder and the give-up, and the daily count drains as the restarts
age out of the last 24 hours. All of that survives the restart it triggers —
it lives in `state.json`, like the smoke test's verdict.

Every action is visible: a timeline entry naming the reason it acted on, the
attempt number and what it will do next; a persistent notification raised at
the boot after the restart (the restart ends the process that would have shown
it); and `watchdog` in `GET /api/status`, which the **System** page shows under
the setting.

The **Overview** keeps a resource history: memory, CPU, event-loop lag and
volume usage, one sample a minute, for 48 hours by default and up to 120
(*kept for … hours* on the same card). Memory that keeps growing for hours, or
an event loop held for 500 ms or more in several minutes of the last hour,
raises a notification: the usual signs of a leak or of blocking code in the
integration. Every notification the integration raises goes on the timeline;
more than three within two seconds become one line that counts them and names
the first three titles.

### Logs and log files

**Logs** shows the process log: everything Home Assistant and the integration
log, with filters and a live follow. Each line carries its date and time, and
the list holds the newest 200 lines (following live drops the oldest). Loggers listed in the registry's
`quiet_loggers` start at WARNING; raise one at runtime while you investigate.
A live follow keeps advancing even when a whole batch of new lines matched only
inside masked values, or a level or logger filter matched nothing, and a
follower that fell behind reads on at once while more lines are waiting. The
search runs once typing pauses.
The root logger is not one of them: a level set there would silence or flood
every logger at once, including the line that records the change, so it is
refused; raise the integration's own logger instead.
When more than 50000 lines wait to be written (a blocked output), newer lines
are dropped and a warning says how many.

**Log files** shows files the integration writes itself, such as traffic dumps
or debug logs. It appears in the menu only when there are any. The files are
found through the integration's config entries (any setting ending in `.log`,
with its rotated copies), and the `*.log` files and their rotated copies
(`*.log.1`, `*.log.2026-09-10`) in the registry's `log_dir` and in the config
root. Symbolic links are never listed, and neither is a file with more than one
hard link: a hard link is a second name for the same file, so nothing about the
path tells a log apart from a `secrets.yaml` linked under a `*.log` name.
Rotation by rename or by copy leaves one link, so nothing the integration writes
is lost by it. File names are masked like everything else on the page, and two
files whose names mask to the same text are still listed separately and each
opens its own file; after a restart the page selects the same file again by
its name, and says so when several files share that name. A tail reads at most
the last 32 MB of a file, and a search also stops after 20000 lines that hold
something the masking looks at (a word such as `token` or `key`); *lines read*
says how far it got.

**Download file**, next to the tail controls, saves the selected file whole.
It is masked exactly as the table is, line by line as it is sent, so nothing
is held in memory. A key block stays masked to its end even when the download
has long passed its `BEGIN` marker, and even when the log never closed it with
an `END` marker. The file is saved under its masked name, never its real one;
a name the mask cut the extension off gets `.log` back, so it opens as a log.
A file larger than 32 MB is sent from its end, the newest lines, which is the
tail's budget and is there for the same reason: masking costs per line, and
the page hands the whole answer to the browser at once. The name of the saved
file then says `-last-32MiB`, and the answer carries
`X-Log-Truncated: 33554432`. The button fetches with the header the endpoint
requires, which a plain link cannot send.

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
named group. Counted repeats are limited, because compiling writes each one out
and has no time limit: with every `{n}` and `{m,n}` written out `n` times, the
pattern may hold at most 10000 elements (`[0-9]{4}` is a few, `(?:[0-9]{2}:){100}`
a few hundred, `(?:a{1000}){1000}` a million and is refused). A field of any
length is `.*` or `[^ ]+`, which costs nothing. A stored format that this check
refuses (saved by an earlier version) is ignored: the Log files page shows whole
lines and says why. The format is stored in
`integration_manager/settings.json`, so it
survives image updates and is part of backups. The filter box searches the
whole line with secrets already masked, hidden groups included. Matching has a time limit: when a
pattern is too slow for the lines on screen, the remaining lines are shown
whole and the page says so.

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
about as near too). Two optional
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
  unit, icon, category, device). Access tokens are left out of the
  attributes: `access_token`, and any attribute whose URL carries a `token=`
  (the `entity_picture` of a camera, image or media player), which would open
  this container's proxy. Every entity of the container's Home Assistant gets a
  document, except the entities of the integrations listed in
  `exclude_integrations` (default `["integration_manager"]`; set with `POST
  /api/mqtt/config`, as a list or comma-separated text; their services are also
  left out of the catalog and not callable over MQTT), entities excluded by a
  rule, and `zone` entities: `zone.home` is the container's own home location,
  which every Home Assistant creates by itself (one published by 0.17.0 or
  older is removed from the main HA five minutes after the start, like any
  excluded entity). `entities_total` in `GET
  /api/mqtt/status` counts the entities with a state that get a document. A vacuum's document also has `fan_speed` and
  `state` at the top level (copies of the attribute and of the state), because
  the main HA's MQTT vacuum reads them only there. That `state` is `null` when
  the vacuum's state is not one of the six activities that platform knows
  (`unknown` and `unavailable`): it drops anything else and would otherwise
  keep showing the activity it had.
- **Protocol and document size**: the container connects with MQTT 5, so a
  broker can announce the largest packet it accepts. A document over that
  maximum, or over 1 MiB when none is announced, is skipped rather than sent: a
  broker that refuses an oversized packet closes the connection, and the client
  would replay the same document on every reconnect until nothing else gets
  through. A broker that refuses MQTT 5 gets an MQTT 3.1.1 connection, logged
  and on the timeline; 3.1.1 cannot announce a maximum, so only the 1 MiB limit
  applies there. `GET /api/mqtt/status` shows the protocol in `protocol`, and a
  skipped document is named in the log and on the timeline and counted there
  (`oversized_skipped`, `last_oversized`); the MQTT page shows the protocol and
  the last skipped document next to the connection state.
- **Subscriptions**: at every connection the container subscribes to
  `cmd/#`, `call/#` and `manager/cmd/+`. A broker that refuses them (an ACL
  that allows publishing but not subscribing) still gets every document, but
  no command, service call or manager action reaches the container: the
  refused topics and the broker's reason are logged once, put on the timeline,
  and shown in `subscribe_error` and `connect_error` of `GET /api/mqtt/status`
  and next to *connected* on the MQTT page while the connection stays up. Once
  means once per connection the container opens (saving the MQTT settings, an
  identity change, a restart), not once per reconnect of the client library;
  ending a connection itself clears both errors. Only a refusal the broker states can be seen:
  mosquitto's `acl_file` grants every subscription and silently drops what the
  client may not read (its dynamic security plugin refuses it). `status` turns
  `online` only once the broker
  has answered the subscription, so a command the main Home Assistant sends the
  moment the device becomes available is received; a refused subscription still
  turns it `online` (the documents keep flowing), and a broker that has not
  answered within 10 seconds gets `online` anyway, reported like a refusal
  until the answer comes. An `online` never follows the retained `offline` the
  container sends when it ends a connection itself. The MQTT client library's own messages are
  logged under `custom_components.integration_manager.mqtt_publisher.paho`
  (INFO and above; DEBUG gives a packet trace, which names topics and sizes but
  never the password or a payload).
- **Discovery** (off by default): one retained config per device. Entities of
  every domain that has an MQTT platform become native entities with working
  commands; the rest (cameras, media players, weather, …) are mirrored as
  read-only sensors with all attributes. Per-entity rules on the Entities page
  or as JSON (`GET/POST /api/mqtt/rules`, `{"rules": {"<entity id or glob>":
  {...}}}`) change only what is published: `exclude` (true/false), `name`,
  `enabled_by_default` (true/false), `entity_category` (`config` or
  `diagnostic`), `device_class` and `icon` (`mdi:<name>`). A `device_class` is
  refused, with the reason, when it is not one the main Home Assistant takes
  for an entity the rule matches, or when that entity's unit does not fit it
  (`W` with `temperature`): the main HA would refuse the whole device config.
  An entity a glob rule matches later that the class does not fit is announced
  without it, with a warning in the log, and the rest of the rule applies.
  An entity excluded while the container was down is removed from the main HA at the next connection. When
  two entities of one device would get the same component key
  (`image_processing.x` and `image.processing_x`), the second is skipped with a
  warning in the log; `discovery_collisions` in `GET /api/mqtt/status` counts
  them. When two entities ask for the same entity id on the main HA (a
  mirrored `camera.front` becomes `sensor.camera_front`, next to a real
  `sensor.camera_front`), both are announced, the main HA gives one a `_2`
  suffix, and the log names them; `discovery_default_id_duplicates` counts
  them. A light, fan, siren or humidifier whose state is `unknown` stays
  unknown on the main HA. A `text` entity whose state is `unknown` or
  `unavailable` here shows unavailable on the main HA rather than a value: its
  MQTT platform takes every payload as the text, so there is no payload that
  means *no value* there. A text whose value really is the word `None`, or
  empty, is shown as it is. An entity disabled in the container stays on the
  main HA with its customisations and shows unavailable there; it is still
  announced, with `enabled_by_default: false`, which the main HA applies only
  when it creates an entity. Deleting the entity removes it there, and so do
  renaming its entity id (the new id replaces it) and excluding it, also in the
  first five minutes after a start, while entities announced before the start
  are still kept for the orphan sweep (see *Stop, uninstall, restore*); when
  it was the last entity of a device that is gone from the container too, the device's
  discovery config is cleared as well (unless entities carried from the previous
  process are still setting up, see below), so no empty device is left on the main HA
  until the next full republish. In those first five minutes the entities an
  earlier process announced and that have not finished setting up here count as
  entities of their device: the device keeps its config (with a removal form for
  the one that went) until the orphan sweep decides, so they are not taken off
  the main HA and brought back a moment later. Renaming a device, or changing
  its model or its parent, reaches the main HA within a few seconds instead of
  waiting for that republish. A button, scene or notify entity has no state to mirror but still
  follows the availability of the entity behind it, so it shows unavailable on
  the main HA while that entity is.
  An integration named `call`, `cmd`, `result`, `services`, `manager`,
  `health` or `status` publishes its documents under
  `<name>-integration/<domain>/<object_id>`, so they never land on the
  command, call or result topics.
- **Commands**: numeric command topics accept only finite numbers. Text
  values, notify messages and select options are used exactly as sent, spaces
  included. The value sent to a `text` entity in password mode, on any of its
  command topics or with `text.set_value`, shows as `***` in the command history, the
  status and the log, also inside a service error that quotes it (the result
  sent back to the caller keeps it). The two bounds of a thermostat range change arrive as two
  commands and become one service call: the first waits up to 1 s for the
  second. An alarm panel with a code asks for it on the main HA and sends it
  with the action. A JSON payload on a vacuum's `send_command` topic needs a
  `command` string; its other keys are the command's parameters (`{"command":
  "spot_area", "rooms": [1]}`, the shape the main HA sends; a lone `params`
  object, as 0.17.0 took it, is used as the parameters); any other payload is
  sent as the command name. The state topic of a switch, light, fan, siren or
  humidifier takes only `ON`/`OFF`, `TRUE`/`FALSE` or `1`/`0` (any case,
  surrounding spaces ignored); any other payload is refused rather than read
  as *off*, with the reason under *recent commands* and in the log. The action
  tokens of a cover, valve, lock, alarm panel, vacuum or lawn mower match in any
  case; an unknown one is refused with the tokens that are accepted. Tilting a
  cover open or closed on the main HA arrives as tilt position 100 or 0, which
  is what its MQTT cover sends; when the cover here cannot set a tilt position,
  those two become `cover.open_cover_tilt` and `cover.close_cover_tilt` instead
  (and so does a tilt position of 100 or 0 sent from there, the only tilt such a
  cover has). A position in between is still sent as a position, which such a
  cover refuses: the main HA offers the slider anyway, because its MQTT cover
  takes every tilt feature from the tilt topic. A command
  larger than 256 KB, or nested deeper than 64 levels, is refused unread, with
  the reason in the same two places. A command, service call or manager action
  published with `retain` is never carried out, because a physical effect must
  not replay at every reconnect; the retained message is cleared from the broker
  as it arrives, and the log names the topic (it does not appear under *recent
  commands*). On a broker that
  speaks only MQTT 3.1.1 this holds for a retained command found when the
  container subscribes (at every connection); one published while the container
  is already connected reaches it without the retain flag, runs once, and its
  retained copy is cleared at the next connection.
- **What the main HA cannot show**: its MQTT platforms have no place for some
  of what an entity has here. A water heater's away mode (on/off is mirrored),
  installing an update with a backup, the title of a notify message (the
  message arrives), who changed an alarm panel (`changed_by`), and the
  `device_class`, `supported_features` and `entity_picture` attributes of an
  entity mirrored as a sensor (a media player's `tv`) stay in the container.
  A text value shows on the main HA without its leading and trailing spaces
  (Home Assistant strips what a template renders; a value sent from there
  keeps them). A vacuum command sent from the main HA carries its parameters
  only as a mapping (the main HA drops a list). A category set with an MQTT rule
  reaches an entity the main HA already has only after the main HA restarts,
  like `enabled_by_default`. Covers, vacuums and water heaters show the
  features the entity supports here, and a fan offers the same speed steps.
- **Service calls**: publish a JSON object to `call/<domain>/<service>` (service
  data plus optional `entity_id`, and an optional `_id`); the result comes back
  on `result/...`, with a `response` key for a service that returns response
  data (the catalog marks those `"response": "optional"` or `"required"`, from
  what the integration registered). A result over the broker's maximum packet
  size is answered without its response data, with `ok: false` and the reason,
  so the caller still gets an answer. So is a call that fails inside the
  container: `ok: false` with `internal error (<exception type>)`, and the log
  names where (never the message, which may quote the data). A repeated `_id` within five minutes is answered from memory
  and never executed twice (the latest 1000 `_id`s are kept); the comparison
  keeps the type, so `1` and `"1"` are two different calls. While the service
  still runs, also after the call was answered with a timeout, a repeat is
  answered `ok: null` with `state: running` and `duplicate: true`; once it
  ends, a repeat gets its final result (flagged `late` when it ended after the
  timeout). At most 50 service
  calls and commands run at once, a timed-out call counting until its service
  returns: beyond that a call is answered `too many calls in progress` (a
  retry with the same `_id` runs once there is room) and a command is
  rejected. `homeassistant`, `shell_command`, `python_script`,
  `hassio` and `integration_manager` are never callable.
  `persistent_notification` and `notify.persistent_notification` are not
  callable over MQTT and are left out of the MQTT service catalog; the Services
  page can still call them. `NaN`, `Infinity`, numbers too large to be finite
  (`1e999`), payloads larger than 256 KB, JSON nested deeper than 64 levels
  and JSON that does not parse are rejected, with an answer on `result/...`
  that still carries the `_id` when the outer object names one as a string, a
  finite number, `true`, `false` or `null` (read from at most the first 256 KB,
  without parsing the payload). Values of keys ending in
  `code`, `key`, `pin`, `otp` or `auth` as a word of their own (`code`, `user_code`,
  `api_key`, `user_pin`, `basic_auth`, not `zipcode`, `code_format`, `spin`,
  `author` or `oauth`), or in `usercode`, `passcode`, `pincode`, `password`,
  `passwd`, `passphrase`, `secret`, `token`, `apikey`, `passkey`, `bindkey`,
  `credential`, `credentials` or `psk` (`access_token`, `api_token`,
  `wifi_psk`, not `token_type`) are masked in the command
  history, the status and the log, and so is a rejected command's reason
  where it quotes the payload; `translation_key`, `sort_key` and
  `primary_key` stay readable. The masking reads at most the first 4 KB of a
  payload's text (every key of a JSON payload is still found), so a longer
  payload shows cut there in the history, the status and a rejected command's
  log line, and masking can never hold up the MQTT connection. A call reaches only entities
  the container publishes: an `entity_id` of `all`, or an entity, group (and
  its members), area, floor, label or device that resolves to an excluded or
  unknown entity, is refused, and so is a target that cannot be read (an id
  that is not a string). An area, floor, label or device is measured only against the
  entity domains the service can act on: Home Assistant hands an entity service
  its own component's entities and nothing else, so a room or device that also holds
  entities this container does not publish is no reason to refuse
  `light.turn_on` for it, while a service that is not an entity service keeps
  the strict check. Entity ids in the service data count too: fields
  ending in `entity_id` or `entity_ids`, `group_members`,
  `snapshot_entities`, `entities`, `add_entities` and `remove_entities` (a
  list or a mapping keyed by entity id),
  at any depth; an entity id in a field with another name is not recognised,
  so do not rely on excluding an entity to keep it from a service that takes
  it under a different name. A `device_id` that is not a
  Home Assistant device (a hardware address a service takes as data) stays plain
  service data. A call needs a JSON object, `{}` when it has no data: an empty
  or whitespace-only payload is rejected.
- **Manager device**: with discovery on, or with `manager_discovery` alone (for
  example while running in shadow mode), the main Home Assistant gets a
  `hass-remote-integration (hass_<domain>)` device. It shows whether the
  integration is up and its health, has update entities for the integration,
  for Home Assistant in the container and for hass-remote-integration itself,
  and sensors for memory, CPU, event-loop lag (the worst delay of a
  one-second timer in the last minute, which is how an integration that blocks
  the loop shows up), volume usage and the patch status. Health is published
  every minute once Home Assistant in the container has started; the two health
  entities go unavailable when three minutes pass without one, so a stuck
  container never keeps showing an old `ok`. They also stay unavailable during a
  restart until Home Assistant in the container has started again.
- **Manager actions** (`manager_commands`, off by default): *Install* on the
  integration and Home Assistant update entities, plus *Restart*, *Back up now*
  (at most every 10 minutes) and *Check for updates* (every 5 minutes) buttons. Installing the integration runs the
  preflight, then installs and starts the release the way the UI does (backup,
  smoke test, automatic rollback) and restarts when the loaded code has to be
  replaced; installing Home Assistant (upgrades only) takes a backup, keeps the
  configuration and restarts. The limits of *Back up now* and *Check for
  updates* survive a restart (if they cannot be saved, the action still runs
  and the limit holds until the restart). A restart asked for over MQTT, on its own or
  after an install, waits up to five minutes in all for a running manager action
  (an install, a backup, a check for updates) and for an install, start or
  backup started from the UI to finish; if one is still running then, or if the
  restart is refused for another reason, the restart is skipped and the result
  says so (`restart skipped: <action> is still running (<n> s)` for a manager action, `restart skipped: another action is still running` for one started from the UI). The result goes out once the restart is really under way, so an `ok`
  on `manager/result` means the process is going down and not only that the
  command was accepted. A refused command (an unknown action, a wrong payload,
  or `manager_commands` off) gets `ok: false` and the reason on
  `manager/result`, and nothing else is published; an unknown action's name is
  cut to 40 characters in the answer, its error and the history. While an action runs, any other
  action than a restart is answered `<action> is still running (<n> s)`; one
  that never returns stops holding the others after 30 minutes. Anyone who can publish under the base topic can use
  them, so turn this on only on a broker with credentials.
  hass-remote-integration itself is updated by pulling a new image.
- **Stop, uninstall, restore**: the identity (`hass_<domain>`) belongs to the
  running integration. *Stop* is not a removal: the whole device, the manager
  device included, goes unavailable on the main Home Assistant and keeps its
  entities with their customisations until the integration starts again.
  *Stop* also clears the retained service catalog, so the main Home Assistant
  is not left with services it cannot call; the next start publishes it again.
  While no integration runs there is no identity: `GET /api/mqtt/status`
  shows `base_topic` and the other topics as `null`, and so do `base_topic` in
  its `health` and in the `mqtt` part of a `POST /api/run/{start,stop}` answer.
  *Uninstall* clears everything retained under that identity, so the main Home
  Assistant removes the entities and devices. If the broker cannot be reached
  then (or refuses the cleanup), the integration is still removed here, the
  answer says so (`retained_cleanup_failed` with `retained_cleanup_error`) and
  the timeline records it; the cleanup of that identity (its documents and its
  discovery configs, nothing else) is kept on disk and retried every minute
  while MQTT is enabled, also with no integration installed, until the broker
  takes it. If MQTT is disabled at the
  uninstall, nothing is sent: an identity the container published before
  (the one it recorded last) gets the same kept cleanup, the answer says
  `retained_cleanup_deferred`, and it runs once MQTT is enabled again. A kept
  cleanup belongs to the broker it is for (host, port, TLS and username;
  `retained_cleanup_broker` in the answer and `broker` in
  `retained_cleanup_pending` of the MQTT status show its `host:port`): it is
  tried only while the
  MQTT settings name that broker, is never sent to another one, and completes
  once that broker is configured again (`retained_cleanup_other_broker` in
  the answer while it is not). Its `error` in the MQTT status says what it
  waits for now: MQTT disabled, the MQTT settings to name its broker again, the
  next try, or the error of the last one. If that broker is gone for good,
  stop the container and delete `mqtt_cleanup_pending.json` (or remove its entry for
  that broker). An `mqtt_identity.json` written by 0.16.x or older names no
  broker: a cleanup deferred from it belongs to the broker the MQTT settings
  name at the uninstall. Neither file is part of backups, so a restore never
  forgets a cleanup the broker still needs or brings back an old one. Starting the same integration again on that broker before then cancels
  it: its documents are live again.
  Entities that a restore, an
  import or a rebuild took away before a restart are removed there five
  minutes after Home Assistant in the container has started (only entities
  that exist neither as a state nor in its entity registry by then). The timeline, the resource history and
  the change reports are not part of backups, so a restore does not roll them
  back.
- Before connecting, the container checks that no *foreign* retained data sits
  under its base topic, and refuses to connect if there is (override with
  `force_base_topic`). A discovery prefix that is the base topic, or lies under
  it, is refused when the MQTT settings are saved.
- **TLS**: tick `tls` on the MQTT page (brokers usually take TLS on port
  8883). The broker's certificate is verified against the system CAs, or
  against `ca_certs`, a CA file inside `/config` (for example
  `/config/mqtt-ca.pem`). `tls_insecure` skips only the check that the
  certificate names the host: anyone holding a certificate from that CA can
  then pose as the broker and read the credentials. A certificate that fails
  the check shows on the MQTT page with its reason (`TLS handshake failed:
  unable to get local issuer certificate`), checked again at most once a
  minute while the connection keeps failing. A connection the broker accepts and
  then drops within ten seconds is reported as the broker closing it (a packet
  over its maximum is the usual cause), not as a TLS problem: that hint only
  fits a connection that was never accepted. The check for foreign
  retained data and every cleanup connect the same way. Client certificates
  are not supported.

### What the main Home Assistant needs

Discovery uses the device-based MQTT format, and some of what it publishes only
newer Home Assistant understands. Measured in September 2026 on `aarch64`,
against real instances of each release, with the container publishing an
integration of 14 entities:

| Main Home Assistant | What happens |
|---|---|
| **2025.10 and newer** | Everything works, with the two exceptions below. Entities get the ids they have in the container, nothing is logged. |
| 2024.11 – 2025.9 | Every entity is created and works (subject to the two exceptions below), but `default_entity_id` is silently dropped, so ids are generated from the device name (`sensor.hri_probe_no_device_probe_demo` instead of `sensor.probe_demo`). Nothing says so in any log. Anything on the main instance that names the original id — an automation, a script, a dashboard card — points at nothing. |
| Below 2024.11 | Nothing arrives at all: those releases do not subscribe to `<prefix>/device/+/config`, so no entity is created and no error appears anywhere. |

Two details on top of that:

- **`date`, `time` and `datetime` entities need 2026.5 or newer.** Home
  Assistant gained MQTT platforms for those three domains in 2026.5. An older
  main instance rejects the *whole device's* discovery payload over one of
  them — not just that entity — with `value must be one of [...] @
  data['components'][...]['platform']` in its log, so the device's other
  entities disappear with it. Until the main instance is updated, exclude those
  entities from MQTT (the Entities page, or a rule) and the rest of the device
  comes back.
- **Colour temperature on mirrored lights needs 2025.2 or newer**
  (`color_temp_kelvin`).

The Home Assistant *inside the container* is unrelated to this: it is the one
the manager installs, and its own floor is in *Python versions*.

---

## Security

By default there is **no login**, like many self-hosted appliances on a
trusted LAN. Set `HRI_PASSWORD` (or `HRI_PASSWORD_FILE`, for example a Docker
secret) to require a password:

- a `HRI_PASSWORD_FILE` that cannot be read, or that is empty (a Docker secret
  created but never populated), is treated as a password that failed to arrive:
  nothing is accepted until it is fixed, a login attempt says why (`POST
  /api/login` answers `503` with the reason), and the reason
  is in the log and on the timeline;
- the browser gets a session cookie from the login page, valid for 30 days,
  and **log out** in the top bar ends every session of the UI, in all browsers,
  including one opened a moment before (each logout starts a new session
  generation, signed into the cookie and kept across restarts and restores).
  When the volume cannot record the logout (full or read-only), every session
  still ends and the page says so, but only until the container restarts: the
  volume keeps the generation of the last logout it recorded, so at the restart
  the sessions issued between that logout and the failed one are valid again
  (until they expire), and the ones issued after the failed one (a login right
  after it included) end. Log out again once the volume is fixed;
- scripts send the password as `Authorization: Bearer <password>` (the
  scheme in any case);
- a line end at either end of `HRI_PASSWORD` (an `.env` file saved with
  Windows line ends) is not part of the password, since no login form or header
  can carry one; spaces are, so a password may start or end with one. The file
  of `HRI_PASSWORD_FILE` is read without the spaces and line ends around it;
- after 5 wrong attempts from one address, that address is refused for 15
  minutes (for IPv6, the whole /64 it belongs to); after 30 wrong attempts
  within 5 minutes from all addresses together (for example from a whole IPv6
  range), every password attempt is refused, also the right one, until the
  count drops (at most 5 minutes; logged and in the timeline). Browsers already
  logged in keep working;
- changing the password logs every browser out.

The password covers every path on the port, Home Assistant's own included:
anything under `/api/` without a session or `Bearer` header gets `401`. That
includes webhooks (`/api/webhook/<id>`) and other callbacks that a cloud
service or a device on the LAN sends to Home Assistant, and there is no
allowlist. An integration that receives webhooks only works without a password.

The session cookie is named `hri_session_<port>` (a session from an older
version under `hri_session` moves to the new name by itself). Browsers send
cookies to every port of a host name, so any other service on the same IP
address or name receives the cookie and can overwrite it; the port in the name
only keeps two instances on one host from logging each other out. To isolate
the UI from other web apps on the same machine, serve it under its own host
name (a reverse proxy with its own name, see below).

Over plain HTTP the password and the session travel unencrypted, so on a
network you do not trust put the UI behind a reverse proxy with TLS. The status
page served on every boot until Home Assistant runs (the system packages of
`HRI_APT_PACKAGES`, the PyPI lookup, a version install, the requirements, a
scheduled restore, removing unused venvs, or while a failed restore waits for a
retry) is not protected. It shows the phase, which names the Debian packages
`HRI_APT_PACKAGES` asks for, and, for a failed restore, the name of the backup
to restore from; until Home
Assistant starts and while no password is set, it also shows the tail of the
last install log, which the System page shows to anyone without a
password anyway. While a failed restore holds the boot, `/api/` paths answer
`503` with `installing: false` and `restore_failed: true`.

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
  **System**. A name written with its trailing dot (`hri.local.`) counts as the
  same name.
- State-changing requests need JSON or an explicit header, so a web page on
  another origin cannot trigger them; neither can it trigger the expensive
  reads (see *API*).
- Home Assistant's onboarding API (`/api/onboarding…`) answers `403`. An
  integration that depends on `frontend` or `panel_custom` loads it, and while
  no Home Assistant user exists it would let any page create the owner account.
- The **Log files** page lists regular log files only (`*.log` and rotated copies such as
  `*.log.1`); symbolic links and files with more than one hard link are skipped,
  so neither kind of link can put another file of the volume (`secrets.yaml`) on
  the page. Listing them needs `X-Requested-With: fetch`, like reading a tail.
- A `Content-Security-Policy` on every response: scripts only from the
  manager's own static files (no inline script), no plugins, no framing by
  other pages, no `<base>` rewrites. Inline style attributes are allowed, and
  so is the login page's own `<style>` element.
- Secrets (MQTT password, GitHub token, parent HA token) are write-only in the
  UI, stored in files readable only by the owner, and never logged or included
  in the diagnostics zip. The diagnostics zip, the log file tails and downloads, the records on
  the Logs page (message and traceback) and the inspection of an imported Home
  Assistant backup mask passwords (also `pwd`, `*_pw`, passphrases, passcodes
  and PIN codes), tokens, credentials, session ids, signatures, WiFi and other
  pre-shared keys (`psk`, `wifi_psk`), `auth` as a word of its own (`auth`,
  `basic_auth`, not `author` or `oauth`), every value named `…key` (`local_key`,
  `noise_psk`, `encryption_key`, Z-Wave `network_key`, `s0`/`s2_*_key` and
  `lr_s2_*_key`, `security_key`, `bindkey`, `aes_key`, `ssl_key`, `?key=` in
  URLs, …) except `translation_key`, `sort_key` and `primary_key`, Bluetooth
  `irk`/`ltk`, whole PEM blocks, PINs, one-time codes, HMAC keys, webhook ids
  and cloudhook URLs, `Authorization` values (`Bearer`, `Basic` and any other
  scheme), `Cookie`/`Set-Cookie` values and credentials in URLs (also a
  password holding `/` or `@`). Masking errs on the side of hiding too much.
  A quoted value is masked up to its closing quote, past escaped quotes (`\"`,
  `\'`), also as a JSON string inside another one (`\"password\": \"…\"`);
  a value whose quote never closes (a line cut short) is masked to the end of
  the line.
  The searches on the Logs and Log files pages run on the masked text, so
  looking for part of a key finds nothing: a row that appeared only while the
  search matched the key would let it be read out one character at a time.
  For the same reason, what a search answers besides its rows (the Logs page's
  `cursor` and `truncated`, how far a search on the Log files page reads) and
  how long it takes do not depend on what the masking hides.
  The web server logs every request line to `process.log` and the container
  log. Before a line is written, the search text of the Logs and Log files
  pages, the `file` a tail or a download asks for, and URL parameters named like a credential
  (`access_token`, `authSig`, …) become `***`, so searching for your own secret
  does not write it to disk. A search path counts in any spelling (`/API/logs`,
  `//api/logs`, `/api/logs;x`, `/x/../api/logs`), including the ones the
  server answers with 404 but still logs; lines written by an earlier version are masked where
  they are shown and in the diagnostics zip. A parameter name counts as a
  credential when it holds `token`, `secret`, `password` and the like anywhere,
  or `pass`, `sig`, `key`, `code` or `session` as a word of its own (`authSig`,
  `api_key`, but not `zipcode`, `keyword` or `design`; `translation_key`,
  `sort_key` and `primary_key` stay readable), and a percent-encoded name counts
  as its decoded one. A `-----BEGIN …-----` line with no END marker masks the
  rest of its own line and the base64-only lines under it, and nothing else.
  A key is recognised from its `-----END …-----` marker or from the shape of
  its own lines, so a search result or a tail that starts in the middle of a
  block is masked too; key material with no marker anywhere in the window and
  no name in front of it (a key cut off mid-write, or bytes pasted into a
  sentence) can still get through, which is why a log with secrets in it should
  not be shared casually.
  Backups contain
  them; the login key and the logout record stay out of backups, so a restore
  never revives a logged-out session. The key of an encrypted Home Assistant
  backup you import is only used for that request.
- A release is downloaded only up to 100 MB and unpacked only up to 300 MB and
  20000 files; symbolic links in the archive are skipped. A requirement in a
  manifest that is a pip option (`--index-url …`, `-e …`) or not a valid
  requirement blocks the preflight and refuses the install and the start.
  The environment builder downloads exactly the commit its Check verified.
- An imported Home Assistant backup must be the uncompressed `.tar` Home
  Assistant writes. Its configuration archive may be at most 2 GB, its
  `backup.json` at most 1 MB, and what it extracts at most 2 GB. Reading its
  configuration archive may decompress at most 20 times the archive's size (at
  least 2 GB), skipped members included, and an extended tar header over 1 MB,
  in the backup or in its configuration archive, is refused; neither the
  backup nor its configuration archive may hold more than 100000 files. A `backup.json`
  that is not a regular file or not valid JSON, or a configuration archive that is not a
  regular file, is refused with that reason. Config entries
  with an invalid id are skipped.
- Dangerous service domains are not callable, over MQTT or from the UI. Only
  a call over MQTT is limited to the entities the container publishes: the
  **Services** page and `POST /api/services/call` belong to the admin UI and can
  target any entity, `entity_id: all` and excluded entities included.
- Without `tls` (see *MQTT reference*), the broker connection, the MQTT
  password included, travels unencrypted.

**Do not expose the port to the internet.** Put it behind a reverse proxy with
authentication if you need remote access.

To report a security problem, see [SECURITY.md](SECURITY.md).

---

## Configuration reference

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `HRI_PORT` | `8087` | Port of the UI and API; a changed port is picked up at the next boot, and one pinned in `.storage/http` by an older setup or a restored backup is dropped; a value that is not a port number (1-65535) stops the container at boot with a line in the log. The image's healthcheck reads it too, so a changed port needs nothing else |
| `HRI_NAME` | `hass-remote-integration` | Container and volume name |
| `HRI_VERSION` | `latest` | Image tag Compose pulls, for example `0.21.0` |
| `HRI_REGISTRY` | `ghcr.io/trailro` | Where Compose pulls the image from: `ghcr.io/trailro` (GitHub Container Registry) or `docker.io/trailro26` (Docker Hub); the same image either way. A `docker-compose.yml` from 0.16.0 or older ignores it and pulls from GitHub Container Registry: download the file again to use it |
| `TZ` | `UTC` | Time zone; an unknown zone falls back to UTC, with an error in the log |
| `HA_VERSION_LATEST` | `1` | `0` installs the image's default Home Assistant (`HA_VERSION_DEFAULT`, 2026.9.3 here) on a fresh volume instead of the newest |
| `HRI_APT_PACKAGES` | unset | Debian packages the container installs at boot, before Home Assistant starts, for what pip cannot install (the `ffmpeg` binary, BlueZ): names separated by spaces or commas, for example `ffmpeg libpcap0.8t64`. Only Debian package names are accepted (`libc6:arm64` too); anything else — an option, a URL, a path, a shell metacharacter — is refused with a line in the log and nothing is installed for it, and the value never reaches a shell. Packages that are already installed are not installed again, so a restart costs nothing; they live in the container, not on the volume, so recreating the container or updating the image installs them again. A failure (no network, an unknown package) is recorded on **System** and does not stop the boot. The output is in `integration_manager/apt-install.log`. A `docker-compose.yml` from 0.17.2 or older does not pass it to the container: download the file again to use it |
| `HRI_DEV_SRC` | `./dev-src` | Dev mode: directory mounted at `/dev-src` |
| `HRI_DEBUGPY` | unset | Dev mode: debugger port |
| `HRI_DEBUGPY_HOST` | `127.0.0.1` | Dev mode: address debugpy binds inside the container (the dev overlay sets `0.0.0.0`) |
| `HRI_CALL_TIMEOUT` | `60` | Seconds a service call (over MQTT and from the Services page or `POST /api/services/call`) or a command may take before it is reported as a timeout (a whole number; a value that is not one logs a warning and uses 60, one below 1 uses 1) |
| `HRI_TRACEMALLOC` | unset | Diagnostics: allocation tracing frames (costs memory); a value that is not a number traces 25 |
| `HRI_TRACE_IMPORT` | unset | Diagnostics: log who imports the given packages |
| `HRI_DEBUG` | unset | Debug logging for the manager, and blocking-call detection on the event loop |
| `HRI_PASSWORD` | unset | Password for the web UI and API; unset or empty means no login |
| `HRI_PASSWORD_FILE` | unset | File holding the password, for example a Docker secret; wins over `HRI_PASSWORD`, and must not be empty |
| `HRI_COOKIE_SECURE` | unset | `1` marks the session cookie `Secure` (behind a reverse proxy with TLS) |

### Files on the volume

```
/config/
  venv-<ha version>/            one per installed Home Assistant (venv-current links the active one)
  custom_components/<domain>/   the deployed integration
  integration_manager/
    state.json                  running integration, versions, pending actions, the health watchdog's ledger (its restarts of the last 24 h, the backoff step, the last action); an unreadable one is kept as state.json.corrupt-<stamp>
    settings.json               settings (backup retention, smoke test, health thresholds, health watchdog), tokens, log-file format (mode 600); a damaged one is kept as settings.json.corrupt-<stamp>
    auth_key                    signs login sessions, only with a password set (mode 600)
    auth_revoked                time of the last logout: sessions from before it are invalid
    mqtt.json                   broker configuration (mode 600)
    mqtt_rules.json             per-entity MQTT rules
    mqtt_identity.json          base topic, discovery prefix and broker (host, port, TLS, user; no password) retained data was last published under
    mqtt_undiscover.json        whether a discovery cleanup still waits for the broker's confirmation
    mqtt_cleanup_pending.json   retained MQTT data of uninstalled integrations not cleared yet, per broker (unreachable, or MQTT disabled): retried every minute while MQTT is enabled with that broker
    ha.json                     Home Assistant version, version changes, boot failures, last restore, the last error already announced
    restore-pending.json        a restore scheduled for the next restart (with its zip)
    restore-applied.json        outcome of a restore that could not be recorded (a full volume), recorded at the next boot
    restore-failed.json         the same for a failed restore: recorded at the next boot, never applied again
    rebuild-pending.json        a clean-start rebuild still to run after a Home Assistant downgrade
    import-map.json             entity and device ids an import aligns at boot
    latest_versions.json        last known releases (update entities, the banner)
    manager_actions.json        when each MQTT manager action last ran
    registry.json               your registry entries (see below); one that cannot be read is kept as registry.json.corrupt-<stamp> when an entry is added
    versions/<domain>/<tag>/    version store
    patches/<domain>/           your patches
    yaml/<domain>.yaml          YAML configuration
    events.jsonl                timeline
    process.log                 process log (rotated to process.log.1 and .2)
    change_reports.json         what the last version switches changed
    resource_history.json       resource samples of the Overview
    hacs_catalog.json           cached HACS list for the Install page search
    ha-install.log              pip output of the last Home Assistant version install
    apt-install.log             apt output of the last boot that installed HRI_APT_PACKAGES
    import.tar                  an uploaded Home Assistant backup, until it is inspected
    import-extracted/           what the inspection unpacked from it, until the import or Clear
  .storage.pre-rebuild-<time>/  .storage set aside by a clean start: removed once the rebuild finished or a restore replaced .storage; kept (and logged) when the clean start was dropped, delete it by hand
  backups/                      backups (zip); <time>-pre-restore.zip is a copy taken before a restore, kept from pruning and deletion for 7 days
```

Settings, the MQTT configuration and the MQTT rules are written in the order
they were saved, and every pending save is written before a restart, a stop or
a backup. Edit these files by hand only while the container is stopped:
`settings.json` and `mqtt_rules.json` are read when the process starts and
overwritten by the next save from the UI, and a hand edit of `mqtt.json` is
picked up by *Reconnect* but lost after a second save from the MQTT page.
`registry.json` is read again whenever it changes. A `settings.json` that cannot
be read, is not valid JSON or is not a JSON object is not used: the manager starts
on the default settings (without the tokens) and says so in the log, on the
timeline and as a notification. One that is not valid JSON or not an object is
kept as `settings.json.corrupt-<stamp>` (the newest three, mode 600) before the
next save replaces it. In `settings.json` a switch
written as `"true"`/`"false"`, `"on"`/`"off"`, `"yes"`/`"no"` or `"1"`/`"0"` is
read as that value; any other text uses the default. A save that cannot be
written (a full volume) leaves the running configuration as it was and answers
with the reason, instead of a server error.

The numeric MQTT settings have ranges: `port` 1-65535, `qos` 0, 1 or 2,
`republish_interval_s` 30-86400 s and `full_republish_interval_min` 5-10080
min. The MQTT page refuses a port or a qos outside them and clamps the two
intervals. The same ranges are applied when `mqtt.json` is read, so a hand
edit cannot keep the manager from starting: a value out of range, or not a
number, falls back to its default (1883, 0, 300, 60) with a warning in the log.
So does a switch that is not `true`/`false`, a text setting that is not a
string, and an `exclude_integrations` that is not a list of domains. An
`mqtt.json` that cannot be parsed, or that is JSON but not an object (`[]`),
gives the default settings, MQTT disabled, with a warning in the log and on the
timeline, until the MQTT page saves them again. A `ca_certs` that resolves
outside `/config` (a hand edit, a restored file, a symbolic link out of the volume) is dropped the same way: the system CAs are
used, with a warning in the log.

A registry entry in `integration_manager/registry.json` has this shape; only
`repo` is required. A file of another shape, or one that is not valid JSON (empty,
a trailing comma), is ignored, with a line in the log saying what was expected: a
hand edit cannot keep the container from starting. Adding an entry from the UI
over such a file keeps it as `registry.json.corrupt-<stamp>` first.
The domain `integration_manager` is the manager itself: it is refused in the
registry (an entry for it is ignored, with a warning), and never installed,
started or uninstalled.
A `state.json` that cannot be read is kept as `state.json.corrupt-<stamp>` (the
newest three), the loss is reported on the timeline and as a notification, and
the manager adopts what it finds on disk: when exactly one integration has
config entries and a deployed copy with a `manifest.json`, and at least one
version of it is in the store, it is recorded as running again and can be
stopped, rolled back and updated as before (the deployed version comes from the
marker next to the code; one that cannot be identified is recorded without a
version). When no such integration is found, nothing is recorded as running, and
the notification says so.

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
| `quiet_loggers` | Loggers started at WARNING (default `custom_components.<domain>`); a value that is not a list of logger names is ignored with a warning in the log |
| `log_dir` | Directory under `/config` where the integration writes log files; its `*.log` files and their rotated copies appear on **Log files** |

### API

Every page is backed by a JSON API on the same port, so everything can be
scripted. With a password set, send it as `Authorization: Bearer <password>`. POST
bodies are JSON (`Content-Type: application/json`). Requests that reach out to
the internet or another server, upload files, or return patches, log file
tails, logs or diagnostics, or run or store patch code, also need `X-Requested-With: fetch`: `/api/catalog`,
`/api/patch_editor` (reading, *Check* and *Save*), `/api/patches/<domain>` (and its `/upload`), `/api/backups/upload`,
`/api/import/upload`, `/api/parity`, `/api/releases/preview`,
`/api/diagnostics`, `/api/diag/memory` (also without `refs`), `/api/logs`,
`/api/log_files`, `/api/log_files/tail` and `/api/log_files/download`, and aborting a flow
(`DELETE /api/flow/<id>`, `DELETE /api/options/<flow_id>`); without it they answer `400`.
`?refresh=1` on `/api/releases` and `/api/ha` is ignored without it.
`GET /api/status` answers without the header too (for monitors and
`verify.sh`), but then from a copy at most 10 seconds old, since building it
runs the `status(ctx)` of `.py` patches; send the header for a fresh one. The main entry
points:

| Area | Endpoints |
|---|---|
| Status | `GET /api/status`, `GET /api/summary`, `GET /api/manager`, `GET /api/manager/history?hours=`, `GET /api/mqtt/status` (`subscribe_error`; `retained_cleanup_pending`: a list of `{base_topic, broker, other_broker, deferred, error, since}`, the configured broker's first), `GET /api/events`, `GET /api/notifications`, `POST /api/notifications/dismiss_all`, `POST /api/notifications/<id>/dismiss` |
| Login | `POST /api/login` (`{"password": …}`, sets the session cookie; `503` with the reason while `HRI_PASSWORD_FILE` is empty or unreadable), `POST /api/logout` (ends every session; `500` with `ok: false` and the reason when the volume could not record it: every session still ends, but at the next restart the sessions from before that logout are valid again and those issued after it end) |
| Integration | `POST /api/install`, `GET /api/change_reports`, `POST /api/run/{start,stop,cancel_pending_start}`, `GET /api/releases`, `GET /api/releases/preview?domain=&tag=`, `POST /api/releases/preflight`, `POST /api/updates/check`, `POST /api/installed/<domain>/{uninstall,rollback_full,remove_version}` (uninstall answers `retained_cleared`, and while its MQTT cleanup waits: `retained_cleanup_failed` with `retained_cleanup_error`, or `retained_cleanup_deferred` when MQTT is disabled, plus `retained_cleanup_broker` (`host:port`) and `retained_cleanup_other_broker` when the settings name another broker), `GET/POST /api/registry` |
| Builder / dev | `GET /api/catalog?q=`, `GET /api/build/options`, `POST /api/build/{check,prepare}`, `GET /api/dev`, `POST /api/dev/install` |
| Configuration | `POST /api/flow/start`, `GET /api/flow/progress`, `POST/DELETE /api/flow/<id>`, `POST/DELETE /api/options/<flow_id>`, `GET/POST /api/yaml/<domain>`, `GET /api/entries`, `POST /api/entries/<entry_id>/{options,reload,delete}` (an unknown entry id, there or in a `reconfigure` flow start, answers 404 with a message) |
| Patches | `GET /api/patches/<domain>`, `POST /api/patches/<domain>/upload`, `POST /api/patches/<domain>/<name>/{apply,delete}`, `GET /api/patch_editor/<domain>?name=`, `POST /api/patch_editor/<domain>/{check,save}` |
| MQTT | `GET/POST /api/mqtt/config`, `GET/POST /api/mqtt/rules`, `POST /api/mqtt/{reconnect,republish}`, `GET /api/mqtt/discovery`, `GET /api/mqtt/commands` |
| Entities | `GET /api/entities`, `POST /api/entities/<entity_id>/{rename,name,disable,enable,delete,mqtt_exclude,mqtt_include,mqtt_name}`, `GET /api/devices`, `POST /api/devices/<device_id>/{name,delete}`, `GET /api/services`, `POST /api/services/call` |
| System | `GET /api/ha`, `POST /api/ha/{update,rollback,check}`, `POST /api/restart`, `GET/POST /api/settings` |
| Backups | `GET /api/backups`, `POST /api/backups/create`, `POST /api/backups/upload`, `GET /api/backups/<name>/download`, `POST /api/backups/<name>/{restore,delete}`, `POST /api/backups/restore/cancel` (answers `cancelled`; a restore that belongs to a scheduled Home Assistant version change is refused with `for_version`, and a full rollback's restore with `rollback`, the backup it restores) |
| Import | `POST /api/import/upload`, `GET/POST /api/import/inspect`, `POST /api/import/{apply,apply_all,clear}` |
| Cutover | `GET /api/parity`, `POST /api/parity/{test,remove_orphans}`, `POST /api/cutover/{status,enable,undo}`; `enable` takes `force`, which skips the checks on the main Home Assistant (MQTT loaded, the integration's config entries, entity ids still registered there) but not the container's own (an integration running, health, MQTT connected), and the answer and the timeline say `forced`; `undo` answers `cleared_discovery_configs` and `manager_device_kept`; a check on the main HA that cannot run (unreachable, its registry unreadable) blocks the enable rather than passing. Removing an orphan while discovery is off is refused, except for the manager device while `manager_discovery` announces it |
| Logs | `GET /api/logs?level=&prefix=&q=&since_id=&limit=` (`limit` 1 to 2000, `since_id` 0 to 2^63-1, otherwise `400`; the answer carries `cursor`, the next `since_id`), `GET /api/logs/loggers`, `POST /api/logs/level` (`{"logger": …, "level": …}`), `GET /api/log_files` (an `id` per file, which changes at every start), `GET /api/log_files/tail?id=&file=&lines=&q=` (`file` is the masked name, answered `409` when several files share it; a real name is not accepted), `GET /api/log_files/download?id=&file=` (the same file selection; the file masked and streamed as an attachment under its masked name, at most its last 32 MB, `X-Log-Truncated` when it was cut), `GET/POST /api/settings` (`log_format`) |
| Diagnostics | `GET /api/diagnostics` (zip, secrets removed), `GET /api/diag/memory[?refs=<type>]` (one probe at a time: a second one meanwhile answers `429`) |

`POST /api/logs/level` accepts any existing logger; a logger that does not
exist yet (a library imported later) needs a dotted Python name, and at most
50 of those can be created.

`GET/POST /api/settings` carries the health watchdog as `watchdog` (a boolean,
off by default), `watchdog_after_min` (5–720), `watchdog_min_interval_min`
(15–1440) and `watchdog_max_per_day` (1–24); numbers outside the range are
clamped, not refused. `GET /api/status` answers `watchdog` with the same rules
under shorter names (`enabled`, `after_min`, `min_interval_min`, `max_per_day`)
plus `restarts_24h`, `attempts`, `window_min` (what the next attempt has to
wait through), `gave_up`, `last` (`at`, `integration`, `reason`,
`unhealthy_s`, `attempt`, `next`) and `pending` (`bad_for_s`, `window_s`,
`reason`) while a stretch of `error` is being timed.

`GET /api/ha` includes `apt`: what this boot did with `HRI_APT_PACKAGES`
(`packages`, `refused`, `ok`, `note`, `error`, `at`), or `null` when the
variable is not set.

`GET /api/ha` answers the version list as `versions`: the `recent_n` newest
stable releases (`recent` is still only those) plus every installed venv, the
running version, a scheduled one and the previous one. `versions_total` is how
many there are in all, and `?all=1` answers every stable release plus those
same local entries as `all_versions` — it reads the release list already in
memory, so unlike `?refresh=1` it costs no extra PyPI call and needs no
`X-Requested-With: fetch`. While PyPI has never answered in this process there
is no release list, so `all_versions` and `versions_total` fall back to what
the box has. `baseline` is the image's floor, `HA_VERSION_MIN` (`""` if the image
has neither variable; an image built before the two were split answers its
`HA_VERSION_DEFAULT` here): anything older is refused, which the page works out
for itself rather than being told once per version. `default_version` is
`HA_VERSION_DEFAULT`, what a fresh volume installs — not a floor, and nothing
is refused for being older than it. `verdicts` maps a version to
the `check` of `POST /api/ha/check` where that is known without resolving
anything — a report still in the hour-long cache, or the Python a release
needs.

`POST /api/ha/check` (`{"version": …}`) answers `check`: whether that version's
pinned requirements resolve from wheels on this image's Python, without
installing anything — `version`, `ok`, `checked`, `blockers`, `warnings`,
`notes`, `missing` (the pins with no wheel), and, when pip actually ran,
`python`, `machine`, `requirements` and `duration_s`. `checked: false` means
the question could not be answered (pip timed out, PyPI was unreachable, the
resolver gave up); `ok` then stays `true`, since nothing was found against the
version. The version running now answers `checked: false` with a note and runs
no pip, and so does a version already installed for this Python ("nothing to
resolve"); a version the floor, `requires_python` or PyPI itself refuses
answers `ok: false` with that refusal as the blocker rather than an error. The
request itself answers `ok: true` in all of those: the verdict is `check.ok`.
The answer is cached per version and image Python for an hour, and only one
check runs at a time.

`POST /api/ha/update` runs the same check and, without `force: true`, refuses a
version it blocks with `needs_force`, the report in `check` and the blockers in
`error` — before it takes a backup or writes anything. `force` skips that check
and nothing else: a version older than the image's floor (`HA_VERSION_MIN`), one this image's
Python cannot run (`requires_python`), or one PyPI does not list is refused
with no `needs_force`, because force cannot rebuild the image. A forced update
puts *scheduled with the dependency check skipped (force)* on the timeline,
whether or not the check would have passed. The manager device's *Install Home
Assistant* action takes all of those refusals and has no force at all.

`GET /api/summary` includes `manager_update`: the running release and the
newer ones the banner shows. `POST /api/run/start` takes `force`; a tag that is
not in the version store is refused without a preflight or `needs_force`. Without
`force`, a start with preflight blockers answers `needs_force` with the report in
`preflight`, a start whose preflight could not run says why in
`preflight_note`, and one that passes with warnings answers with
`preflight_warnings`. `ok: true` from a start means the version was deployed
and recorded, not that it set up: `smoke_test` in the answer says when the
health verdict is due (after the restart, when one is needed), the verdict
itself appears in `GET /api/status` under `smoke_test.last`, and `note` says
when no verdict is coming (the version
was already deployed and running, or the smoke test is off). `POST /api/backups/<name>/restore` takes `force` too: a
backup that does not record its Home Assistant version answers `needs_force`
when `.storage` is restored. `POST /api/services/call` refuses the same service
domains as the MQTT path but, unlike it, is not limited to published entities
(any target, `entity_id: all` included). It is bounded like the MQTT
path: a call that has not answered within `HRI_CALL_TIMEOUT` seconds is answered
`timeout after <n>s (service still running)` while the service goes on running,
and at most 50 calls from this endpoint run at once, a timed-out one counting
until its service returns; beyond that a call is answered `too many calls in
progress (50): try again later`.

---

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
- **A Home Assistant version is refused as "older than this image's
  baseline".** The image is built with a floor (`HA_VERSION_MIN`, 2026.5.0
  here) and refuses everything below it outright. This is not the pin check,
  and *Schedule anyway* is not offered. A venv of an older version already on
  the volume can still be switched to, and a rollback to the previous version
  is not blocked by it. Below that floor Home Assistant's own pinned
  requirements have no wheel for this image's Python, so a lower floor would
  only move the refusal to the pin check: what is needed is an image with an
  older Python, not a different build argument.
- **"restart required" does not go away.** Click *Restart process* on the
  Overview; some changes (a new version of a loaded integration, YAML) only take
  effect at a restart.
- **Restart process does nothing.** A restart is refused while the manager is
  busy: an install, start, stop or uninstall, an import, a clean-start rebuild
  after a Home Assistant downgrade, a full rollback, a
  restore being scheduled or cancelled, a backup, a patch being applied, a Home
  Assistant version change, the self-check right after boot, or a restart
  already under way. The page shows the error. Wait for it to finish
  and restart again. A restart that fails before anything stops (the state
  file cannot be written on a full volume, for example) is refused the same
  way, with the reason. Once a restart is accepted, the process gives Home
  Assistant about 205 s to stop and then exits anyway, so a stop that hangs
  still ends in a restart rather than in a container that is up and
  unreachable.
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

`verify.sh` reads `HRI_NAME`, `HRI_PORT`, `HRI_IMAGE`, `HRI_NETWORK`, `HRI_PASSWORD`
(or `HRI_PASSWORD_FILE`, which wins) and `TZ` from the environment or from `.env`.
`start` exits non-zero when the API does not come up (a timeout or a restart
loop). `HRI_ENV="K=V K2=V2"` passes more variables to the container, which is
how CI boots a version other than the newest (`HA_VERSION_LATEST=0
HA_VERSION_DEFAULT=<version>`).

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
  anywhere — the entities simply look wrong, or never appear.
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
