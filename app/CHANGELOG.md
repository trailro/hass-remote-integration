# Changelog

Every version's changes are in the
[GitHub releases](https://github.com/trailro/hass-remote-integration/releases).

## 0.26.0

- **Two instances of the same integration can share a broker.** A new
  `HRI_INSTANCE=<name>` publishes under `hass_<domain>-<name>` (base topic,
  client id, discovery and unique ids, manager device). An HRI Manager
  instance derives it from its app slug (`local_hri_<name>`). Docker: set it
  in `.env` and download the compose file again. See
  [MQTT](https://github.com/trailro/hass-remote-integration/blob/main/docs/mqtt.md).
- **Nothing moves by itself:** an integration this volume already published
  keeps its identity, whatever `HRI_INSTANCE` says; existing installs and
  existing manager instances keep their topics, unique ids and entity ids.
  **Move to …** on the MQTT page switches on purpose (by default it leaves
  the old names on the broker; clearing them is explicit). An invalid
  `HRI_INSTANCE`, an app slug the Supervisor could not give, or a damaged
  `mqtt_identity.json` keeps MQTT off with the reason, never picks a name.
  Rolling back to 0.25.x after publishing under an instance identity moves it
  back to the plain name.
- **Host network for HRI Manager instances** (HRI Manager 0.2.0): the
  integration sees the LAN's mDNS, SSDP and broadcast. HRI listens on the
  port the Supervisor gives the app, and the sidebar panel works as before.
  On the host network the port needs the app's password: without one every
  request that is not the sidebar panel gets `403`. Home Assistant inside
  does not announce itself on the LAN (zeroconf, SSDP); the integration's
  discovery works. The app from this repository is not affected.
- **Healthcheck:** reads the port the app was given (`/run/hri-port`), else
  `HRI_PORT`.
- **CI** checks that the newest released HRI Manager accepts
  `app/config.yaml`.

## 0.25.2

- **HRI's backups are now in the Home Assistant backup of the app.** Until
  now they were left out, so restoring a Home Assistant backup deleted them,
  the one a **Full rollback** needs included. They hold no Home Assistant
  install, so the backup grows by about *Backups to keep* times one to a few
  MB (with *Backups to keep* at 0, keep all, by every backup ever made).
  Restoring it brings them back as they were when it was taken.
- **While Home Assistant backs up the app**, HRI waits with pruning and
  refuses to delete a backup ("try again in a few minutes"), so its backup
  of the app never fails on a file HRI removed halfway.
- **Release images:** the release build checks out the release tag itself,
  never a branch with the same name, and the step that updates the app's
  version no longer leaves its write token on disk.

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
