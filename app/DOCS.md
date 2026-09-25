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
