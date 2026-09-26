# hass-remote-integration

Runs one Home Assistant integration in its own container and mirrors its
entities to your Home Assistant over MQTT. The integration is installed and
managed from this app's web UI: **Open Web UI** on the Info tab, or **HRI** in
the sidebar. It works through Home Assistant's login (remote access and Home
Assistant Cloud included); the app's port 8087 keeps its own password.

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
- A Home Assistant backup of the app holds the manager's own backups (since
  0.25.2; they hold no venv, so it grows by about *Backups to keep* times one
  to a few MB), the one a Full rollback needs included. Restoring it brings
  them back as they were then: download the ones made since first.
- Any logged-in Home Assistant user, administrator or not, can open the UI
  through Home Assistant (the panel is hidden from the others, not closed to
  them): the `ingress_users` option narrows it to the user names listed. Clear the
  port on the Network tab if you use only the sidebar.
- Uninstalling leaves the app's folder
  (`app_configs/<id>_hass_remote_integration`, about 800 MB) unless you also
  delete its data, but drops the app's options, the password included: a
  reinstall reuses the folder and starts with no password until you set one
  again on the Configuration tab.
- Stopping waits up to 240 seconds for Home Assistant inside; a clean stop
  takes well under a second.
- Keep the app's **Watchdog** (Info tab) on: HRI turns it on once at the
  first start. A restart from HRI's web UI then ends the app and the
  Watchdog starts it again (with it off, HRI starts over inside the app), but
  only the Watchdog starts the app again after a crash, a hung stop or an
  out-of-memory kill.
