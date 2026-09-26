# Files on the volume

All under `/config`. *Backup* says whether a backup holds the file
([Backups and restore](backups.md)).

## Top level

| Path | What it is | Backup |
|---|---|---|
| `venv-<ha version>/` | one per installed Home Assistant; `venv-current` links the active one | no |
| `custom_components/<domain>/` | the deployed integration | yes |
| `backups/` | backups (zip); `<time>-pre-restore.zip`, taken before a restore, is kept from pruning and deletion for 7 days | no |
| `.storage.pre-rebuild-<time>/` | `.storage` set aside by a clean start; removed once the rebuild finishes or a restore replaces `.storage`. Kept (and logged) if the clean start was dropped: delete it by hand | no |

## `integration_manager/`

| Path | What it is | Backup |
|---|---|---|
| `state.json` | running integration, versions, pending actions, watchdog ledger (24 h of restarts and reloads, backoff step, last restart and reload) | yes |
| `settings.json` | settings (backup retention, smoke test, health thresholds and watchdog), tokens, log-file format; mode 600 | yes |
| `registry.json` | your registry entries (below); re-read whenever it changes | yes |
| `ha.json` | Home Assistant version, version changes, boot failures, last restore, last error announced | yes, never restored |
| `auth_key` | signs login sessions (password set only); mode 600. Unwritable: a key in memory, sessions end at the next restart | no |
| `auth_revoked` | time of the last logout; older sessions are invalid | no |
| `mqtt.json` | broker configuration; mode 600 | yes |
| `mqtt_rules.json` | per-entity MQTT rules | yes |
| `mqtt_identity.json` | base topic, discovery prefix and broker (host, port, TLS, user; no password) of the last retained publish | no |
| `mqtt_undiscover.json` | whether a discovery cleanup awaits the broker's confirmation | no |
| `mqtt_cleanup_pending.json` | uncleared retained data of uninstalled integrations, per broker (unreachable, or MQTT off); retried every minute while MQTT is on with that broker | no |
| `restore-pending.json` | restore scheduled for the next restart (with its zip); deleting it ends a failed restore's wait | no |
| `restore-applied.json`, `restore-failed.json` | outcome of an applied or failed restore a full volume kept from being recorded; recorded at the next boot, never applied again | no |
| `rebuild-pending.json` | a clean-start rebuild still to run after a Home Assistant downgrade | yes |
| `import-map.json` | entity and device ids an import aligns at boot | yes |
| `import.tar`, `import-extracted/` | an uploaded Home Assistant backup until inspected, and what the inspection unpacked until the import or *Clear* | no |
| `versions/<domain>/<tag>/` | version store | yes |
| `patches/<domain>/` | your patches | yes |
| `yaml/<domain>.yaml` | YAML configuration | yes |
| `latest_versions.json` | last known releases (update entities, the banner) | no |
| `manager_actions.json` | when each MQTT manager action last ran | yes |
| `change_reports.json` | what the last version switches changed | no |
| `resource_history.json` | resource samples of the Overview | no |
| `hacs_catalog.json` | cached HACS list for the Install page search, read only when fetching it fails (it is fetched at the first search after a start, then every 12 hours) | no |
| `events.jsonl` | timeline | no |
| `process.log` | process log, rotated to `process.log.1` and `.2` | no |
| `ha-install.log` | since the last boot that installed Home Assistant: pip output of its installs and the manager's requirements, then every entrypoint line (the container log has them, without pip's) | no |
| `apt-install.log` | apt output of the last boot that installed `HRI_APT_PACKAGES` | no |

## Editing by hand

Edit only while the container is stopped: `settings.json` and
`mqtt_rules.json` are read at process start and overwritten by the next UI
save, and a hand edit of `mqtt.json` is picked up by *Reconnect* but lost after
a second save from the MQTT page. Saves are written in order, all before a
restart, stop or backup; one that cannot be written (full volume) keeps the
running configuration and answers with the reason. A damaged file never stops
the manager:

| File | When it cannot be used |
|---|---|
| `settings.json` | Unreadable, invalid JSON or not an object: default settings, no tokens, reported in the log, on the timeline and as a notification. Invalid JSON or a non-object is kept as `settings.json.corrupt-<stamp>` (newest three, mode 600) before the next save. An unreadable file (a permission, an I/O error) is never replaced: every save is refused with the reason until it is fixed and the container restarted. |
| `state.json` | Unreadable: kept as `state.json.corrupt-<stamp>` (newest three), reported on the timeline and as a notification. An integration that alone has config entries, a deployed copy with `manifest.json` and a stored version is recorded as running again (version from the marker by the code, or none), with stop, rollback and update. Otherwise nothing runs, and the notification says so. |
| `mqtt.json` | Unparsable or not an object (`[]`): default settings, MQTT disabled, warned in the log and on the timeline until the MQTT page saves. |
| `mqtt_rules.json` | Unreadable, invalid JSON or not a rules object: which entities it excludes is unknown, so MQTT fails closed (no connection, nothing published, no command taken; the main HA keeps its entities, unavailable) and rule changes are refused; reported in the log, on the MQTT page and as `rules_error` in `GET /api/mqtt/status`. The file stays in place; invalid JSON or not a rules object is also copied as `mqtt_rules.json.corrupt-<stamp>` (newest three, mode 600), an unreadable file is not. To recover, fix the file or put it back from the copy (or remove it to start over without rules), then press *Reconnect* on the MQTT page, save the MQTT settings, or restart: the rules are read again before connecting. |
| `auth_revoked` | Unreadable or not a number: every session issued before that boot ends, at every boot until the next logout writes it again. |
| `registry.json` | Wrong shape or invalid JSON (empty, trailing comma): ignored, with a log line saying what was expected; adding an entry from the UI first keeps it as `registry.json.corrupt-<stamp>`. |

In `settings.json`, a switch written as `"true"`/`"false"`, `"on"`/`"off"`,
`"yes"`/`"no"` or `"1"`/`"0"` reads as that value; other text uses the default.

In `mqtt.json`, a number out of range or not whole (`8883.0`, `1.0`, `true`),
a switch that is not `true`/`false`, a text setting that is not a string and an
`exclude_integrations` that is not a list of domains fall back to the default
with a warning in the log. The MQTT page refuses a bad `port` or `qos` and
clamps the intervals.

| Setting | Range | Default |
|---|---|---|
| `port` | 1-65535 | 1883 |
| `qos` | 0, 1 or 2 | 0 |
| `republish_interval_s` | 30-86400 s | 300 |
| `full_republish_interval_min` | 5-10080 min | 60 |

A `main_ha_version` that is not a Home Assistant version (`yesterday`,
`v2026.8`, `2026`) is refused by the MQTT page with the reason; written by
hand, it falls back to empty with a warning, so nothing is filtered rather than
the wrong thing. A `ca_certs` resolving outside `/config` (hand edit, restored
file, symbolic link) is dropped with a warning; the system CAs are used.

## Registry entries

An entry in `integration_manager/registry.json`; only `repo` is required:

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
| `quiet_loggers` | Loggers started at WARNING (default `custom_components.<domain>`); not a list of logger names: ignored with a warning |
| `log_dir` | Directory under `/config` of the integration's log files; its `*.log` files and rotated copies appear on **Log files** |

An entry whose `repo` is not `owner/name` (a longer path, `?`, `#`, `..`) is
ignored with a warning; it would become part of a GitHub API address. An
invalid entry for a bundled domain leaves the bundled entry in use. An entry
for `integration_manager`, the manager itself, is ignored with a warning; it
is never installed, started or uninstalled.
