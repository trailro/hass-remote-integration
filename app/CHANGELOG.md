# Changelog

Every version's changes are in the
[GitHub releases](https://github.com/trailro/hass-remote-integration/releases).

## 0.25.0

- **Sidebar panel (ingress):** the web UI opens from **HRI** in the sidebar
  or **Open Web UI**, behind Home Assistant's login, so it also works through
  remote access and Home Assistant Cloud (Nabu Casa). The new
  `ingress_users` option limits it to the Home Assistant users listed. The
  port 8087 keeps HRI's own password and can be turned off on the Network
  tab.
- **Watchdog and restarts:** HRI turns the app's Watchdog on once. A restart
  from HRI (web UI, API, MQTT, after an update or a restore) now brings the
  app back: with the Watchdog on the Supervisor starts it again, with it off
  HRI starts over inside the app.
- **Requirements:** Home Assistant Core 2025.10 or newer. The app's folder
  is mapped as `app_config`, the current name of the same mount: nothing
  moves.
- **Supervisor token:** used at boot only, to turn the Watchdog on and read
  it, then dropped. The docs now say plainly that the password stays
  readable by the integration inside the app: it guards the web UI and API
  from the network.
- **One new login:** the session cookie is now named after the app's host
  name, so two HRI apps on one host no longer log each other out.
- **Smaller backups:** the cached HACS list is left out of Home Assistant
  backups and HRI's own.
