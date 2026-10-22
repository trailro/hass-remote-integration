# Changelog

Every version's changes are in the
[GitHub releases](https://github.com/trailro/hass-remote-integration/releases).

## Unreleased

- **The environment builder's combined change goes back as a whole.** After
  *Prepare* with a Home Assistant version and *start*, the new integration
  version starts at the target's first boot; when it does not set up there
  (`setup_retry` after the extra interval included), the smoke test now
  switches back to the old Home Assistant **and** to the integration version
  that ran before, in one restart. The configuration since the switch is
  kept but for the integration's config entries, the registries and its own
  stores, which come from the switch's backup; the boot deploys and patches
  the older version. A stop at any step is finished at the next boot
  (`ha.json` `integration_revert`), and a crash loop of the new version does
  the same. Before, Home Assistant went back alone with the new integration
  version still deployed (it often does not run there), and that version's
  own smoke test then tried a full rollback that failed. That smoke test now
  waits for the Home Assistant one and judges as always once it is done
  without acting; `smoke_fallback.integration` names both versions.
- **The integration preflight for another Home Assistant version resolves
  the requirements against that version's pins** (its
  `package_constraints.txt` and the requirements of the built-in components
  the integration depends on), not the running one's. ramses_cc 0.61.1 for
  2026.10.2, checked on 2026.9.4, was blocked on `serialx==1.10.0`, which
  2026.9.4 pins and 2026.10 does not. The pins come from the source the
  attribute check already reads (a finished venv of that version, else its
  wheel, kept while the process runs). When they cannot be read, a conflict
  on a package the running version pins is a warning, not a blocker; so is
  PyPI that cannot be reached for another version. Checking against the
  running version is unchanged, except that a conflict on one of its pins
  says so and points at the environment builder, which checks the
  integration version with a newer Home Assistant and switches both
  (switching Home Assistant alone first only helps if the running
  integration version supports it). A pin is blamed only when pip's
  conflict is about a requirement it does not satisfy.
- **Remove the previous Home Assistant venv by hand.** *Remove HA <version>
  venv* on **System** (`POST /api/ha/venv/remove`, the version typed to
  confirm) frees the 0.7-1 GB the rollback target takes once an upgrade is
  settled. A rollback to it then installs it again from PyPI, after the same
  checks as a switch to it. Refused while a version change, its smoke test,
  a switch back or a restore for that version still needs it. `GET /api/ha`
  adds `previous_installed`. A new version that crashes before it ever booted
  while the previous one has no venv for the image's Python (a newer image)
  is kept, and `last_error` says to set `"desired"` in
  `integration_manager/ha.json` to go back.
- **A Home Assistant switch checks the running integration's requirements
  against the target's pins** (`POST /api/ha/check` and `/api/ha/update`,
  **System**, the manager device's install action), as the integration
  preflight does: a conflict is a blocker (`needs_force`; the manager device
  never forces); pins that cannot be read, PyPI that cannot be reached, a
  pip that times out or gives no report, or another integration check
  holding pip for more than 10 s are a warning, asked again next time. Before, only the attributes the integration reads were checked.
  A stopped integration is not checked.
- **Log files lists the logs an integration writes in a folder of the config
  dir.** ramses_cc 0.61.1 writes `ramses_rf_logs/packet_log.log` (0.60.4
  wrote `packet_log.log` in the config root) and names no path in its
  config entry, so the page listed only the old version's stale file. Each
  folder directly under the config dir is now scanned too (one level; not
  links, hidden folders, venvs or Home Assistant's and the manager's own),
  with the same rules as before: no links, no file with more than one hard
  link.
- **No device registry deprecation warnings on Home Assistant 2026.10.** The
  manager read `DeviceEntry.config_entries` and `primary_config_entry` (the
  **Devices** page, a device delete, the registry import), which 2026.10
  logs as deprecated for a custom component. On 2026.10 and newer it reads
  `config_entry_id` (and the set only on the composite device 2026.10
  synthesizes for a pre-migration id, which does not log); before 2026.10
  it reads the set, which logs nothing there.

## 0.28.2

- **The `updated` stale basis no longer mistakes a dead source for a live
  one.** A value expiring to `unknown` or `unavailable`, an attribute losing
  its value, the old value an entity is put back with after a reload, and
  attributes that tick with the clock (ages and staleness counters; for
  ramses_cc 0.61.1 its device status sensors, except when a device was last
  heard) are no longer fresh data: with ramses_cc 0.61.1 on a dead
  connection the verdict stayed `ok` and the watchdog never acted. A climate
  entity's temperature, an attribute, still counts. Add attribute names and
  entity ids for an integration with `ignore_attributes` / `ignore_entities`
  in its health rules (`POST /api/settings`). Under `reported`, writes of
  `unknown`/`unavailable` count only when no entity has a value.
- **Health document:** `last_state_update` / `last_state_update_age_s` follow
  the fresh-data rules under either basis, and `last_report` /
  `last_report_age_s` leave out writes of `unknown`/`unavailable`. They no
  longer move with ticking attributes or expiring values, and an entity that
  has not changed since a restart counts from that start, so the ages can be
  larger than before.
- **Under `updated`, *stale* must exceed the integration's longest normal
  quiet stretch** (for ramses_cc 0.60.4 at least 900 s), or the verdict flaps
  and, with *also on a lasting degraded*, the watchdog can act on a healthy
  integration.
- **The MQTT page keeps `ignore_attributes` / `ignore_entities`** when it
  saves an integration's row; the lists are set through the API only.
- **A fresh volume told not to take the newest Home Assistant** (or without
  PyPI) now installs 2026.10.1 (was 2026.9.4).
- **Going back to 0.28.1:** it ignores the `ignore_attributes` /
  `ignore_entities` lists, and saving an integration's row on its **MQTT** page
  drops them.

## 0.28.1

- **The preflight catches a Home Assistant module attribute the target no
  longer has**, such as `cv.vol` (gone in 2026.10 with the move to probatio):
  ramses_cc 0.60.4 is now blocked for Home Assistant 2026.10, where it fails at
  setup. The attribute is looked up in the target's own source, never imported;
  reads guarded by `getattr`, `hasattr`, `try` or a version test are not
  reported. Report field `removed_ha_attributes`.
- **A Home Assistant version switch checks the running integration first**
  (System, `POST /api/ha/check` / `update`, the manager device): a blocker asks
  before switching anyway; MQTT never forces.
- **Smoke test after a version switch:** if the integration's entries no longer
  load on the new version, the container switches back once, after saving the
  configuration as the new version left it (backup kept 7 days) and restoring
  only the files the older version cannot read; the reason names the error from
  the log. An entry waiting for its device (`setup_retry`) keeps the version as
  degraded. The version left is not offered again automatically. `GET /api/ha`
  has `smoke` and `smoke_fallback`.

## 0.28.0

Fixes from a review of the codebase.

- **Interrupted imports are settled, never stuck.** An import interrupted by a
  restart (or by Home Assistant stopping) is settled at the next boot from the
  config entry Home Assistant actually saved: the imported stores are kept when
  the entry is on disk, the originals are put back when it is not. A stuck
  import can be resolved from the import page (**Resolve**,
  `POST /api/import/resolve`). An import is only committed once Home Assistant
  has really written the entry (its storage swallows write errors); a later
  update of the entry by the integration no longer looks like a failure.
  Originals that cannot be matched safely are kept as `*.pre-import.orphan`,
  never put back over a store in use.
- **A rebuild survives a restart:** stopping Home Assistant during a rebuild
  keeps its plan and the pre-rebuild copy, and the next boot resumes it.
- **Imported device names and disabled flags** apply only to the devices of
  the config entry they came from.
- **Preflight checks the full dependency closure** of the integration, as the
  start installs it, so a requirement of an indirect dependency that cannot
  install is found before anything changes.
- **A reinstall of the same tag within a second** now deploys the new copy
  (the copy's stored generation is its identity, not its install time).
- **MQTT:** malformed MQTT rules keep MQTT off with the reason instead of
  silently dropping rules (an `exclude` included); retained cleanup that could
  not read everything stays pending and is retried, and so does turning
  discovery off; background cleanup and sweeps run on the broker of the live
  connection and are dropped and retried if the connection changes meanwhile;
  service-call replies go to the connection the call came from.
- **Cutover:** entity ids of a named instance (`hass_<domain>-<name>`) are
  compared the way Home Assistant writes them, so they are no longer reported
  as renamed and a holder of the id is found before discovery is enabled.
- **Backups:** deleting a backup is refused while another manager action
  (install, start, restore, Home Assistant version change) runs, so an update
  never loses its rollback backup.
- **Diagnostics** no longer include a file path in an unreadable-log error;
  the stop watchdog cannot be held up by a blocked log write.

## 0.27.0

- **Fixed: Remove orphans could delete another container's entities.** Two
  containers of integrations like `a` and `a_binary` on one broker: the
  Cutover page of `a` could take some of `a_binary`'s entities for its own
  orphans and empty their discovery config. A container now clears a device
  it does not announce only when its retained config carries this
  container's own origin; anything else is left and reported (`not_ours`,
  `not_on_broker`, `unreadable`).
- **Unambiguous ids for new volumes.** A volume that never published uses
  `hass_<domain>-…` ids, as instances already did (`-` is in no domain, so
  two containers' ids never collide). **A volume that published with 0.26.0
  or older keeps its ids**: nothing is migrated, nothing changes on the main
  Home Assistant. A volume without `mqtt_identity.json` reads its own
  retained configs first and keeps the format it finds.
- **When the format cannot be decided** (a broker that refuses or hides the
  configs, an incomplete read), no discovery config is announced until it
  can, and the MQTT page says why and lets you choose. **Change id format**
  on the MQTT page switches a decided format behind a confirmation (entity
  ids and history stay; areas, names and labels set on the main HA do not).
- **Rolling back** a volume that started with `-` ids to 0.26.0 makes the
  main HA create its entities again with `_2` ids; updating again removes
  those and keeps the originals. See
  [MQTT: Id format](https://github.com/trailro/hass-remote-integration/blob/main/docs/mqtt.md#id-format).

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
