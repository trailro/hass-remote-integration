# Home Assistant OS / Supervised app

On Home Assistant OS or a Supervised install, hass-remote-integration runs as
an app (called an add-on before 2026) from this repository. It is the same
image as the Docker install, with the same web UI; what differs is set up
below.

## Install

1. **Settings > Apps > App Store**, menu **⋮ > Repositories**, add
   `https://github.com/trailro/hass-remote-integration`.
2. Install **hass-remote-integration**.
3. On its **Configuration** tab, set a password (the web UI port is open to
   your network), then start it.
4. **Open Web UI**. The first start installs Home Assistant inside the app,
   which takes a few minutes and needs internet access; the page shows the
   progress.

For MQTT, the Mosquitto broker app is reachable as `core-mosquitto`, port
1883, with a Home Assistant user or a login set in the Mosquitto app. A broker
elsewhere on your network is reached by its IP address.

## Options

Each option becomes the environment variable a Docker install sets (see
[Environment variables](../README.md#environment-variables)). An empty option
is the same as an unset variable. A change applies when the app restarts.

| Option | Variable | Meaning |
|---|---|---|
| `password` | `HRI_PASSWORD` | Password for the web UI and API; empty means no login |
| `apt_packages` | `HRI_APT_PACKAGES` | Debian packages installed at boot, for what pip cannot install (`ffmpeg`) |
| `call_timeout` | `HRI_CALL_TIMEOUT` | Seconds a service call or command may take; empty means 60 |
| `ha_version_latest` | `HA_VERSION_LATEST` | Off: a fresh app installs the image's default Home Assistant instead of the newest |
| `debug` | `HRI_DEBUG` | Debug logging for the manager, and blocking-call detection |
| `cookie_secure` | `HRI_COOKIE_SECURE` | Marks the session cookie `Secure`, behind a reverse proxy with TLS |

Not options:

- the time zone is the one set in Home Assistant (**Settings > System >
  General**), which the Supervisor passes to the app;
- the port inside the app is always 8087. Change the port on your network on
  the app's **Network** tab; **Open Web UI** follows it;
- `HRI_PASSWORD_FILE`, dev mode and the diagnostics variables are for Docker
  installs.

An options file the app cannot read stops it at boot, with a line in its log,
rather than starting without the password it may hold. The log names the
variables set from the options, never their values.

## Files

The app's folder is its `/config`: `/addon_configs/<id>_hass_remote_integration`
on the host (the `addon_configs` share of the Samba app). The layout is the
one in [Files on the volume](files.md). Stopping the app gives Home Assistant
inside up to 240 seconds to save its registries.

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

## Serial devices

The app sees the host's serial devices (`uart`): a USB stick shows as
`/dev/ttyUSB0`, `/dev/ttyACM0` and under `/dev/serial/by-id/`. Use the
`by-id` path in the integration's configuration: it survives a reboot and a
different USB port. Every integration in the app can open every serial device
of the host, including a stick your main Home Assistant uses: give each stick
to one of them only.

## No ingress yet

The web UI does not work under the path prefix Home Assistant's ingress puts
in front of it, so it is not in the sidebar. **Open Web UI** opens it on its
own port.

## Updates

The App Store offers an update once a release's image is published: the
release workflow sets the app's version only after the image is pushed.
Updating the app replaces the image and keeps the folder, like pulling a new
image for a Docker install. The Home Assistant inside the app is updated from
the web UI, as in [Home Assistant versions](home-assistant-versions.md).
