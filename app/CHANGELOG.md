# Changelog

Every version's changes are in the
[GitHub releases](https://github.com/trailro/hass-remote-integration/releases).

## 0.25.1

- **Security fix, sidebar panel (ingress):** a Home Assistant user could
  open the panel under another user's name, getting past `ingress_users`:
  the Supervisor passes on a copy of its user headers that the browser
  sends spelled in another case. HRI now accepts only the Supervisor's own
  spelling and answers `403` otherwise.
- Every logged-in Home Assistant user can open the panel, administrator or
  not (the others only do not see it in the sidebar). If some of them should
  not reach HRI, set `ingress_users` on the **Configuration** tab.

## 0.25.0

- **The options apply:** until now the app ran the 0.24.0 image, which does
  not read them, so a password set on the **Configuration** tab was not asked
  for on the app's port. From this version every option applies; check that
  the password is the one you want.
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
- **Session cookie:** named after the app's host name, so two HRI apps on
  one host do not log each other out.
- **Smaller backups:** the cached HACS list is left out of Home Assistant
  backups and HRI's own.
- **A refused start waits instead of looping:** when HRI will not start an
  older Home Assistant on a configuration a newer one wrote (for example
  after restoring an old backup), the app stays up and its page says why;
  after you fix it, restart the app.
