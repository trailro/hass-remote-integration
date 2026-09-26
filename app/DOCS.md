# hass-remote-integration

Runs one Home Assistant integration in its own container and mirrors its
entities to your Home Assistant over MQTT. The integration is installed and
managed from this app's web UI: **Open Web UI** on the Info tab.

- Options, backups, serial devices and updates of the app:
  [docs/app.md](https://github.com/trailro/hass-remote-integration/blob/main/docs/app.md)
- Everything else (first integration, MQTT, moving an integration over from
  your main Home Assistant, troubleshooting):
  [README](https://github.com/trailro/hass-remote-integration/blob/main/README.md)
  and [docs/](https://github.com/trailro/hass-remote-integration/tree/main/docs)

The first start installs Home Assistant inside the app and takes a few
minutes; the web UI shows the progress. It needs internet access.

- MQTT: the Mosquitto broker app is `core-mosquitto`, port 1883, the host a
  fresh app offers.
- Restoring a Home Assistant backup deletes the manager's own backups (they
  are not in it), the one a Full rollback needs included: download the ones
  you want to keep first.
- Uninstalling leaves the app's folder (about 800 MB) unless you also delete
  its data.
- Stopping waits up to 240 seconds for Home Assistant inside; a clean stop
  takes well under a second.
- Keep the app's **Watchdog** (Info tab) on: HRI turns it on once at the
  first start. A restart from HRI's web UI starts HRI over inside the app
  either way, but only the Watchdog starts the app again after a crash, a
  hung stop or an out-of-memory kill.
