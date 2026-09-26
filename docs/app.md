# Home Assistant OS / Supervised app

On Home Assistant OS or a Supervised install, hass-remote-integration runs as
an app (called an add-on before 2026) from this repository. It is the same
image as the Docker install, with the same web UI; what differs is set up
below. Supervised installs are deprecated and unsupported by Home Assistant.

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
[Environment variables](../README.md#environment-variables);
`HRI_INGRESS_USERS` exists only in the app). An empty option
is the same as an unset variable. A change applies when the app starts a
fresh container: a restart from its **Info** tab, or a restart from HRI while
the Watchdog is on. A restart in place, with the Watchdog off, keeps the
options the app started with (see below).

| Option | Variable | Meaning |
|---|---|---|
| `password` | `HRI_PASSWORD` | Password for the web UI and API on the app's port; empty means no login there (the sidebar panel uses Home Assistant's login) |
| `apt_packages` | `HRI_APT_PACKAGES` | Debian packages installed at boot, for what pip cannot install (`ffmpeg`) |
| `call_timeout` | `HRI_CALL_TIMEOUT` | Seconds a service call or command may take; empty means 60 |
| `ha_version_latest` | `HA_VERSION_LATEST` | Off: a fresh app installs the image's default Home Assistant instead of the newest |
| `debug` | `HRI_DEBUG` | Debug logging for the manager, and blocking-call detection |
| `cookie_secure` | `HRI_COOKIE_SECURE` | Marks the session cookie `Secure`, behind a reverse proxy with TLS |
| `ingress_users` | `HRI_INGRESS_USERS` | Home Assistant user names (the login name, any case) that may open the UI through Home Assistant; empty means every user. The variable is the list, comma separated |

Not options:

- the time zone is the one set in Home Assistant (**Settings > System >
  General**), which the Supervisor passes to the app;
- the port inside the app is 8087. Change the port on your network on
  the app's **Network** tab, or clear it to turn the port off. An HRI Manager
  instance on the host network is the exception: see [Host
  network](#host-network);
- `HRI_PASSWORD_FILE`, dev mode and the diagnostics variables are for Docker
  installs.

An options file the app cannot read stops it at boot, with a line in its log,
rather than starting without the password it may hold. The log names the
variables set from the options, never their values.

## Restarts and the Watchdog

The Supervisor starts a stopped app again only when the app's **Watchdog**
toggle, on its **Info** tab, is on; it is off by default. A Docker install has
its restart policy (`restart: unless-stopped`) for that. With the Watchdog on,
the Supervisor also restarts the app when its healthcheck (`GET /api/alive`,
liveness only) fails three times in a row, about 90 s; an install in progress
answers it, so an install is never cut short that way.

- **A restart asked for from HRI** (**Restart** in the web UI or the API, the
  MQTT restart, the health watchdog, and the restart after a Home Assistant
  version change, a restore, a clean start or an integration update) ends
  the app's process when the Watchdog was on at the app's start, and the
  Supervisor starts a fresh container, as after a crash. With the Watchdog
  off, or when HRI could not read it at the start, HRI starts over inside the
  running app instead. What the Supervisor stops (**Stop** or **Restart** on the Info tab, an update of the
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
log says so and the next start tries again. A restore from **System** keeps
that record; a restore of a Home Assistant backup of the app made before 0.25
removes it, and the next start turns the Watchdog on again.

## The Supervisor token

The Supervisor gives the app a token (`SUPERVISOR_TOKEN`, and the older name
`HASSIO_TOKEN`). With it, code in the app can read and rewrite the app's
options through the Supervisor API. HRI uses it at boot, then removes both
variables from its environment, so Home Assistant inside the app and the
integration it runs do not inherit them: dropping the token stops that code
from reading or rewriting the app's options through the Supervisor API.

It does not hide the password from code running in the app.
`/data/options.json` (root, `0600`) and the `HRI_PASSWORD` environment
variable are readable by any integration, everything in the container running
as root, exactly as `HRI_PASSWORD` is in a plain Docker install. The password
protects the web UI and API from the network, not from the integration.

Nor is it a sandbox: the container's init process (PID 1, Docker's init)
still holds the token in its own environment, so an integration determined to
read it can. Install integrations you trust.

## Files

The app's folder is its `/config`: `/app_configs/<id>_hass_remote_integration`
on the host (the `app_configs` share of the Samba app; `addon_configs` before
Supervisor 2026.06). The layout is the one in [Files on the volume](files.md).
Stopping the app gives Home Assistant inside up to 240 seconds to save its
registries. That is a ceiling, not a wait: a clean stop takes well under a
second.

Uninstalling the app leaves this folder on the host, about 800 MB with the
installed Home Assistant, unless you tick the option to delete the app's data
when you uninstall it. The app's options go either way, the password among
them: a reinstall reuses the folder's state and starts with no password until
you set one again on the **Configuration** tab.

## Backups

A Home Assistant backup that includes the app holds its whole folder except
what no backup needs (`backupkit.APP_BACKUP_EXCLUDE_GLOBS`: what the
manager's own backups leave out, but those backups themselves):

- `venv-*`, one installed Home Assistant each, about 800 MB. After a restore
  the first start installs Home Assistant again, which takes a few minutes and
  needs internet access;
- log files, Python caches, `deps/` and `tts/`;
- the cached HACS list of the Install page (`hacs_catalog.json`), fetched
  again at the first search;
- files being written (a backup being made or uploaded included), and a
  restore or import in progress;
- the login key and the logout record: everyone logs in again after a restore.

The manager's own backups (`backups/`) are in it when the installed app is
0.25.2 or newer. The Supervisor backs an app up with the configuration of the
version installed, so:

- the backup Home Assistant takes right before the update from 0.25.1 or
  older to 0.25.2 still leaves `backups/` out;
- restoring a Home Assistant backup made while an older version of the app
  ran brings that version back, with its configuration: later Home Assistant
  backups leave `backups/` out again until you update the app.

They hold no venv, so each is small: about one to a few MB,
depending on the integration's `.storage`. A Home Assistant backup of the app
grows by roughly *Backups to keep* (`backup_keep`, 5 by default) times that,
plus the backups pruning keeps on top of it (the pre-update backup, a
pre-restore backup for 7 days, an upload for 7 days). With *Backups to keep*
at 0 (keep all) nothing is pruned, so every Home Assistant backup of the app
holds every backup HRI ever made and grows with each one. Delete the ones you
no longer need from **System**.

Each of the manager's backups holds `integration_manager/settings.json` (its
tokens) and `mqtt.json` (the broker password) as they were when it was made.
So a Home Assistant backup of the app holds past credentials too, not only
the current ones: a broker password you have since changed is still in it.
Protect Home Assistant backups with a password: only then does Home
Assistant encrypt the app's part of the backup.

The app keeps running while Home Assistant backs it up. Around that, the
app's `backup_pre` and `backup_post` commands set and clear
`integration_manager/ha-backup-running`: meanwhile HRI prunes nothing and
refuses to delete a backup ("try again in a few minutes"), since a file
deleted while Home Assistant reads the folder fails the app's part of the
backup. The next prune catches up. A mark older than 2 hours (a backup that
never finished) is ignored.

A restore of a Home Assistant backup replaces the app's whole folder. So,
unlike a restore from **System**, it also brings back the timeline, the
resource history, `.storage/http`, `.storage/core.uuid` and the MQTT ledgers
(`mqtt_identity.json`, `mqtt_cleanup_pending.json`) as they were when the
backup was taken. The manager's own backups work as before and are the way to
move an integration to another install.

That restore brings back the manager's own backups as they were when the Home
Assistant backup was taken, together with the state that names them: the
backup taken before the last integration update is there again, so a **Full
rollback** works after the restore (unless the Home Assistant backup was
taken while HRI was writing that very backup: then the rollback is refused and a
plain start of the previous version still works). Backups made after that Home Assistant
backup are gone with the rest of the folder: download them from **System**
first if you want to keep them, and upload them again after. A restore of a
Home Assistant backup made while the installed app was older than 0.25.2 (the
one taken right before the update to 0.25.2 included) still brings back no
backups: **System** then lists none, and a **Full rollback** is refused with
the reason (the backup no longer exists); a plain start of the previous
version still works.

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

## Raw sockets

The app asks for no extra capabilities. The Supervisor plans to drop
`NET_RAW` from app containers by default (a flag since 2026.09.3): from then
on, integrations that need raw sockets, such as ARP/DHCP scanners and
nmap-style device trackers, may not work in the app. A plain Docker install
is not affected.

## Host network

An integration that finds its devices by mDNS, SSDP or UDP broadcast finds
nothing from the app's own network: the container does not see the LAN's
multicast and broadcast. An instance created with [HRI
Manager](https://github.com/trailro/hass-remote-integration-manager) can run
on the host's network instead (its **Host network** choice, off by default;
the app from this repository never does). What changes then:

- **The port.** The instance's `ingress_port` is `0`: the Supervisor picks a
  free port for it (62000 to 65500), keeps it for the app from then on, and
  sends the sidebar panel there. HRI asks the Supervisor for that port at
  every start of a fresh container and listens on it, the healthcheck
  included; the app's log names it (`app: port …, host network`). Two
  instances on one host never share a port.
- **The port is on your network.** On the host network it listens on every
  interface of the host, the Network tab has nothing to turn off, and a port
  without a password would be HRI open to anyone on the LAN. So without the
  app's `password`, every request to the port that is not the sidebar panel
  gets `403` ("Set the app's password to use hass-remote-integration on its
  port"), the page shown while Home Assistant installs too; only
  `/api/alive`, the healthcheck, still answers there. With a password the
  port works as the app's port always does, behind HRI's login.
- **Home Assistant is not announced.** The Home Assistant inside does not
  announce itself on the LAN by either of the two ways Home Assistant does:
  not over mDNS (zeroconf's `_home-assistant._tcp` service), and not over
  SSDP (the UPnP servers the ssdp component starts on every address, which
  advertise the device `urn:home-assistant.io:device:HomeAssistant:1` and
  serve its description with a presentation URL). The companion apps, a new
  Home Assistant's onboarding and UPnP tools never offer it. Only these two
  self-announcements are off: the integration's own discovery works (zeroconf
  browsing, SSDP searches and listening), and a service an integration
  announces itself is announced.

What it costs: the app shares the host's network namespace. The integration
sees and can use every interface of the host and reaches every service the
host listens on, `localhost` included, as a program on the host would. Choose
it only for an integration that needs discovery.

A plain Docker install with `network_mode: host` gets none of this
automatically: `HRI_PORT` is the port there too, HRI cannot tell that the port
is on the LAN, and without `HRI_PASSWORD` it is open. Set a password.

## Access

The web UI opens two ways, with a different gate each:

- **Through Home Assistant (ingress)**: **Open Web UI** on the Info tab, or
  the **HRI** sidebar panel. It works wherever your Home Assistant does,
  remote access over HTTPS and Home Assistant Cloud (Nabu Casa) included,
  with nothing opened on your network. Home Assistant's login is the gate:
  HRI's password and host guard do not apply. The panel shows only for
  administrators, but that only hides it: the Supervisor opens ingress
  sessions for every logged-in Home Assistant user, administrator or not, so
  anyone with the panel's address can open the UI, which installs code and
  runs service calls. `ingress_users` is the way to restrict it: set it to
  the users who may (anyone else gets `403`). The user comes from the
  `X-Remote-User-Name` header the Supervisor sets, and both it and
  `X-Remote-User-Id` must be in the Supervisor's exact spelling: the
  Supervisor forwards a browser's own copy spelled in another case in place
  of its own, so such a request gets `403`, with or without `ingress_users`.
- **On the app's port** (8087 on the host by default; on the [host
  network](#host-network), the port the Supervisor gave the app, refused
  without the app's password): as a Docker install,
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

The App Store offers an update once the image of a new stable release is
published: the Image workflow sets the app's version only after that image is
pushed, and only for the newest stable release (a pre-release is never
offered, and the version never goes back).
Updating the app replaces the image and keeps the folder, like pulling a new
image for a Docker install. The Home Assistant inside the app is updated from
the web UI, as in [Home Assistant versions](home-assistant-versions.md).
