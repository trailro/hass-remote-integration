# Home Assistant OS / Supervised app

On Home Assistant OS or a Supervised install, hass-remote-integration runs as
an app (called an add-on before 2026) from this repository. It is the same
image as the Docker install, with the same web UI; what differs is set up
below.

## Requirements

- **Home Assistant Core 2025.10 or newer**, enforced by the app: on an older
  Core the App Store shows it as unavailable and refuses to install, update or
  restore it. Below 2025.10 the entities HRI sends over MQTT lose their ids,
  below 2024.11 they do not arrive ([what it
  needs](mqtt.md#what-the-main-home-assistant-needs)).
- **2026.5 or newer** for `date`, `time` and `datetime` entities.
- **Supervisor 2026.07.1 or newer.** The Supervisor installs and updates apps
  only while it is the current stable release, so keeping it updated is
  enough.
- **Home Assistant OS** in the range the Supervisor supports (the newest major
  release and the three before it).

Tested on Home Assistant OS 18.3 / Supervisor 2026.09.2 / Core 2026.9.3.

## Install

1. **Settings > Apps > App Store** (**Settings > Add-ons > Add-on Store** on
   Core before 2026.2), menu **⋮ > Repositories**, add
   `https://github.com/trailro/hass-remote-integration`.
2. Install **hass-remote-integration**.
3. On its **Configuration** tab, set a password for the app's port (open to
   your network; see [Access](#access)), then start it.
4. **Open Web UI**, or **HRI** in the sidebar (**Show in sidebar** on the Info
   tab). The first start installs Home Assistant inside the app, which takes a
   few minutes and needs internet access; the page shows the progress.

For MQTT, the Mosquitto broker app is reachable as `core-mosquitto`, port
1883, with a Home Assistant user or a login set in the Mosquitto app. That is
the host a fresh app offers on **MQTT** until you save another one (a Docker
install offers `mosquitto`). A broker elsewhere on your network is reached by
its IP address.

## Options

Each option becomes the environment variable a Docker install sets (see
[Environment variables](../README.md#environment-variables)). An empty option
is the same as an unset variable. A change applies when you restart the app
from its **Info** tab; a restart from the web UI keeps the options the app
started with (see below).

| Option | Variable | Meaning |
|---|---|---|
| `password` | `HRI_PASSWORD` | Password for the web UI and API; empty means no login |
| `apt_packages` | `HRI_APT_PACKAGES` | Debian packages installed at boot, for what pip cannot install (`ffmpeg`) |
| `call_timeout` | `HRI_CALL_TIMEOUT` | Seconds a service call or command may take; empty means 60 |
| `ha_version_latest` | `HA_VERSION_LATEST` | Off: a fresh app installs the image's default Home Assistant instead of the newest |
| `debug` | `HRI_DEBUG` | Debug logging for the manager, and blocking-call detection |
| `cookie_secure` | `HRI_COOKIE_SECURE` | Marks the session cookie `Secure`, behind a reverse proxy with TLS |
| `ingress_users` | `HRI_INGRESS_USERS` | Home Assistant user names (the login name, any case) that may open the UI through Home Assistant; empty means every user. The variable is the list, comma separated |

Not options:

- the time zone is the one set in Home Assistant (**Settings > System >
  General**), which the Supervisor passes to the app;
- the port inside the app is always 8087. Change the port on your network on
  the app's **Network** tab, or clear it to turn the port off;
- `HRI_PASSWORD_FILE`, dev mode and the diagnostics variables are for Docker
  installs.

An options file the app cannot read stops it at boot, with a line in its log,
rather than starting without the password it may hold. The log names the
variables set from the options, never their values.

## Restarts and the Watchdog

The Supervisor starts a stopped app again only when the app's **Watchdog**
toggle, on its **Info** tab, is on; it is off by default. A Docker install has
its restart policy (`restart: unless-stopped`) for that.

- **A restart asked for from HRI** (**Restart** in the web UI or the API, the
  MQTT restart, the health watchdog, and the restart after a Home Assistant
  version change, a restore, a clean start or an integration update) starts
  HRI over inside the running app, with or without the Watchdog. What the
  Supervisor stops (**Stop** or **Restart** on the Info tab, an update of the
  app, a host shutdown) is left to the Supervisor.
- **Everything else ends the app's process**, and only the Watchdog starts it
  again: a crash, a stop that hangs (HRI ends it hard after about 225
  seconds), the kernel ending it for lack of memory, a new Home Assistant
  version that crashes at boot (the fallback to the previous version needs
  three boots), and a boot that cannot go on (no version can be installed, or
  the one it would install is older than the configuration: see [Home
  Assistant versions](home-assistant-versions.md)).

Keep the Watchdog on. HRI turns it on at the first start as an app, once for
its folder (`integration_manager/app-watchdog-enabled` records that it did):
if you turn it off afterwards, it stays off. If that first attempt fails, the
log says so and the next start tries again.

## The Supervisor token

The Supervisor gives the app a token (`SUPERVISOR_TOKEN`, and the older name
`HASSIO_TOKEN`). With it, anything in the app could read the app's options,
the password included, and change them. HRI reads the options with it at boot
and then removes both variables from its environment, so Home Assistant inside
the app and the integration it runs do not inherit them.

This keeps the token out of reach of ordinary code; it is not a sandbox. The
container's init process (PID 1, Docker's init) still holds the token in its
own environment, and everything in the container runs as root, so an
integration determined to read it can. Install integrations you trust.

## Files

The app's folder is its `/config`: `/app_configs/<id>_hass_remote_integration`
on the host (the `app_configs` share of the Samba app; `addon_configs` before
Supervisor 2026.06). The layout is the one in [Files on the volume](files.md).
Stopping the app gives Home Assistant inside up to 240 seconds to save its
registries. That is a ceiling, not a wait: a clean stop takes well under a
second.

Uninstalling the app leaves this folder on the host, about 800 MB with the
installed Home Assistant, unless you tick the option to delete the app's data
when you uninstall it.

## Backups

A Home Assistant backup that includes the app holds its whole folder except
what no backup needs (`backupkit.DISPOSABLE_GLOBS`, the same list the
manager's own backups leave out):

- `venv-*`, one installed Home Assistant each, about 800 MB. After a restore
  the first start installs Home Assistant again, which takes a few minutes and
  needs internet access;
- log files, Python caches, `deps/` and `tts/`;
- `backups/`, the manager's own backups: copies of the same state, which would
  otherwise be stored again in every Home Assistant backup. Download the ones
  you want to keep from **System**;
- files being written, and a restore or import in progress;
- the login key and the logout record: everyone logs in again after a restore.

A restore of a Home Assistant backup replaces the app's whole folder. So,
unlike a restore from **System**, it also brings back the timeline, the
resource history, `.storage/http`, `.storage/core.uuid` and the MQTT ledgers
(`mqtt_identity.json`, `mqtt_cleanup_pending.json`) as they were when the
backup was taken. The manager's own backups work as before and are the way to
move an integration to another install.

That restore also deletes the manager's own backups: `backups/` is not in the
Home Assistant backup, and the folder it restores has none. **System** then
lists no backups, and the backup taken before the last integration update goes
too, while the integration's state may still name it. A **Full rollback** is
refused with the reason (the backup no longer exists); a plain start of the
previous version still works. Download the backups you want to keep before
you restore a Home Assistant backup, and upload them again after.

`backup_exclude` in `app/config.yaml` names every entry below
`*_hass_remote_integration/`, the folder of this app's slug. If you build the
app yourself under another slug, change that part of every entry, or Home
Assistant backups will include the installed Home Assistant (about 800 MB).

## Serial devices

The app sees the host's serial devices (`uart`): a USB stick shows as
`/dev/ttyUSB0`, `/dev/ttyACM0` and under `/dev/serial/by-id/`. Use the
`by-id` path in the integration's configuration: it survives a reboot and a
different USB port. Every integration in the app can open every serial device
of the host, including a stick your main Home Assistant uses: give each stick
to one of them only.

## Access

The web UI opens two ways, with a different gate each:

- **Through Home Assistant (ingress)**: **Open Web UI** on the Info tab, or
  the **HRI** sidebar panel. It works wherever your Home Assistant does,
  remote access over HTTPS and Home Assistant Cloud (Nabu Casa) included,
  with nothing opened on your network. Home Assistant's login is the gate:
  HRI's password and host guard do not apply. The panel shows for
  administrators, but any logged-in Home Assistant user who has the panel's
  address can open it, and the UI installs code and runs service calls: set
  `ingress_users` to the users who may (anyone else gets `403`).
- **On the app's port** (8087 on the host by default): as a Docker install,
  with HRI's own password, session cookie and host guard
  ([Security](security.md)). If you use only the panel, clear the port on the
  app's **Network** tab; nothing is then reachable without Home Assistant.
  The host guard serves the port only under an IP address, `localhost` or a
  local name (`.local`, `.lan`, `.home`, …): under any other name, such as a
  split-DNS name or a `*.ts.net` one, it answers `403`. Open it once by IP
  address and add the name under *allowed host names* on **System**
  (`allowed_hosts`).

A write (install, start, settings, service call) made through the panel names
the Home Assistant user in the manager's log (`ingress: POST /api/… by Home
Assistant user '…'`). Backup uploads and a Home Assistant import stream
through the Supervisor, so their sizes are the same as on the port.

## Updates

The App Store offers an update once a release's image is published: the
release workflow sets the app's version only after the image is pushed.
Updating the app replaces the image and keeps the folder, like pulling a new
image for a Docker install. The Home Assistant inside the app is updated from
the web UI, as in [Home Assistant versions](home-assistant-versions.md).
