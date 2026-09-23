# Files on the volume

## Files on the volume

```
/config/
  venv-<ha version>/            one per installed Home Assistant (venv-current links the active one)
  custom_components/<domain>/   the deployed integration
  integration_manager/
    state.json                  running integration, versions, pending actions, the health watchdog's ledger (its restarts and reloads of the last 24 h, the backoff step, the last restart and the last reload); an unreadable one is kept as state.json.corrupt-<stamp>
    settings.json               settings (backup retention, smoke test, health thresholds, health watchdog), tokens, log-file format (mode 600); a damaged one is kept as settings.json.corrupt-<stamp>
    auth_key                    signs login sessions, only with a password set (mode 600); when it cannot be written (a full or read-only volume) a key held in memory is used and every session ends at the next restart
    auth_revoked                time of the last logout: sessions from before it are invalid
    mqtt.json                   broker configuration (mode 600)
    mqtt_rules.json             per-entity MQTT rules
    mqtt_identity.json          base topic, discovery prefix and broker (host, port, TLS, user; no password) retained data was last published under
    mqtt_undiscover.json        whether a discovery cleanup still waits for the broker's confirmation
    mqtt_cleanup_pending.json   retained MQTT data of uninstalled integrations not cleared yet, per broker (unreachable, or MQTT disabled): retried every minute while MQTT is enabled with that broker
    ha.json                     Home Assistant version, version changes, boot failures, last restore, the last error already announced
    restore-pending.json        a restore scheduled for the next restart (with its zip)
    restore-applied.json        outcome of a restore that could not be recorded (a full volume), recorded at the next boot
    restore-failed.json         the same for a failed restore: recorded at the next boot, never applied again
    rebuild-pending.json        a clean-start rebuild still to run after a Home Assistant downgrade
    import-map.json             entity and device ids an import aligns at boot
    latest_versions.json        last known releases (update entities, the banner)
    manager_actions.json        when each MQTT manager action last ran
    registry.json               your registry entries (see below); one that cannot be read is kept as registry.json.corrupt-<stamp> when an entry is added
    versions/<domain>/<tag>/    version store
    patches/<domain>/           your patches
    yaml/<domain>.yaml          YAML configuration
    events.jsonl                timeline
    process.log                 process log (rotated to process.log.1 and .2)
    change_reports.json         what the last version switches changed
    resource_history.json       resource samples of the Overview
    hacs_catalog.json           cached HACS list for the Install page search
    ha-install.log              the entrypoint's log since the last boot that installed Home Assistant: pip output of that boot's installs and of the manager's requirements, and every entrypoint line from then on (the container log has the same lines, without pip's output)
    apt-install.log             apt output of the last boot that installed HRI_APT_PACKAGES
    import.tar                  an uploaded Home Assistant backup, until it is inspected
    import-extracted/           what the inspection unpacked from it, until the import or Clear
  .storage.pre-rebuild-<time>/  .storage set aside by a clean start: removed once the rebuild finished or a restore replaced .storage; kept (and logged) when the clean start was dropped, delete it by hand
  backups/                      backups (zip); <time>-pre-restore.zip is a copy taken before a restore, kept from pruning and deletion for 7 days
```

Settings, the MQTT configuration and the MQTT rules are written in the order
they were saved, and every pending save is written before a restart, a stop or
a backup. Edit these files by hand only while the container is stopped:
`settings.json` and `mqtt_rules.json` are read when the process starts and
overwritten by the next save from the UI, and a hand edit of `mqtt.json` is
picked up by *Reconnect* but lost after a second save from the MQTT page.
`registry.json` is read again whenever it changes. A `settings.json` that cannot
be read, is not valid JSON or is not a JSON object is not used: the manager starts
on the default settings (without the tokens) and says so in the log, on the
timeline and as a notification. One that is not valid JSON or not an object is
kept as `settings.json.corrupt-<stamp>` (the newest three, mode 600) before the
next save replaces it. In `settings.json` a switch
written as `"true"`/`"false"`, `"on"`/`"off"`, `"yes"`/`"no"` or `"1"`/`"0"` is
read as that value; any other text uses the default. A save that cannot be
written (a full volume) leaves the running configuration as it was and answers
with the reason, instead of a server error.

The numeric MQTT settings have ranges: `port` 1-65535, `qos` 0, 1 or 2,
`republish_interval_s` 30-86400 s and `full_republish_interval_min` 5-10080
min. The MQTT page refuses a port or a qos outside them and clamps the two
intervals. The same ranges are applied when `mqtt.json` is read, so a hand
edit cannot keep the manager from starting: a value out of range, or not a
whole number (`8883.0`, `1.0` and `true` included), falls back to its default
(1883, 0, 300, 60) with a warning in the log.
So does a switch that is not `true`/`false`, a text setting that is not a
string, and an `exclude_integrations` that is not a list of domains. A
`main_ha_version` that is not a Home Assistant version (`yesterday`, `v2026.8`,
`2026`) is refused by the MQTT page with the reason; one that reached
`mqtt.json` by hand falls back to empty with a warning in the log, so nothing
is filtered rather than the wrong thing being filtered. An
`mqtt.json` that cannot be parsed, or that is JSON but not an object (`[]`),
gives the default settings, MQTT disabled, with a warning in the log and on the
timeline, until the MQTT page saves them again. A `ca_certs` that resolves
outside `/config` (a hand edit, a restored file, a symbolic link out of the volume) is dropped the same way: the system CAs are
used, with a warning in the log.

A registry entry in `integration_manager/registry.json` has this shape; only
`repo` is required. A file of another shape, or one that is not valid JSON (empty,
a trailing comma), is ignored, with a line in the log saying what was expected: a
hand edit cannot keep the container from starting. An entry whose `repo` is not
`owner/name` (a longer path, a `?` or `#`, `..`) is ignored the same way, with a
warning in the log: the repo becomes part of a GitHub API address. An invalid
entry in your file for a domain the bundled registry already lists leaves the
bundled entry in use. Adding an entry from the UI
over such a file keeps it as `registry.json.corrupt-<stamp>` first.
The domain `integration_manager` is the manager itself: it is refused in the
registry (an entry for it is ignored, with a warning), and never installed,
started or uninstalled.
A `state.json` that cannot be read is kept as `state.json.corrupt-<stamp>` (the
newest three), the loss is reported on the timeline and as a notification, and
the manager adopts what it finds on disk: when exactly one integration has
config entries and a deployed copy with a `manifest.json`, and at least one
version of it is in the store, it is recorded as running again and can be
stopped, rolled back and updated as before (the deployed version comes from the
marker next to the code; one that cannot be identified is recorded without a
version). When no such integration is found, nothing is recorded as running, and
the notification says so.

```json
{"integrations": {"my_integration": {
  "name": "My integration",
  "repo": "owner/my_integration",
  "patch_module": "my_lib",
  "quiet_loggers": ["my_lib", "custom_components.my_integration"],
  "log_dir": "my_integration_logs"
}}}
```

| Key | Meaning |
|---|---|
| `name` | Display name on the Install page |
| `repo` | GitHub `owner/repo` that publishes the releases |
| `patch_module` | Python package whose site-packages the patches target |
| `quiet_loggers` | Loggers started at WARNING (default `custom_components.<domain>`); a value that is not a list of logger names is ignored with a warning in the log |
| `log_dir` | Directory under `/config` where the integration writes log files; its `*.log` files and their rotated copies appear on **Log files** |
