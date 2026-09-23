# API

Every page is backed by a JSON API on the same port, so everything can be
scripted. This file lists every endpoint, the headers they need, and the
fields the pages do not make obvious.

## Requests

With a password set, send `Authorization: Bearer <password>`
([Security](security.md)). POST bodies are JSON
(`Content-Type: application/json`).

These also need `X-Requested-With: fetch`, or they answer `400`: `/api/catalog`,
`/api/patch_editor` (reading, *Check*, *Save*), `/api/patches/<domain>` and its
`/upload`, `/api/backups/upload`, `/api/import/upload`, `/api/parity`,
`/api/releases/preview`, `/api/diagnostics`, `/api/diag/memory` (with or
without `refs`), `/api/logs`, `/api/log_files`, `/api/log_files/tail`,
`/api/log_files/download`, and aborting a flow (`DELETE /api/flow/<id>`,
`DELETE /api/options/<flow_id>`). Without it, `?refresh=1` on `/api/releases`
and `/api/ha` is ignored.

Exceptions:

- `GET /api/backups/<name>/download` needs no header, so the browser writes
  the backup to disk as it arrives. Another page can start that download in
  your logged-in browser, but cannot read it.
- `GET /api/status` answers without it (for monitors and `verify.sh`), from a
  copy at most 10 seconds old, since building it runs the `status(ctx)` of `.py`
  patches. Send the header for a fresh one.

## Endpoints

| Area | Endpoints |
|---|---|
| Status | `GET /api/status`, `GET /api/summary`, `GET /api/manager`, `GET /api/manager/history?hours=`, `GET /api/mqtt/status`, `GET /api/events`, `GET /api/notifications`, `POST /api/notifications/dismiss_all`, `POST /api/notifications/<id>/dismiss` |
| Login | `POST /api/login` (`{"password": …}`, sets the session cookie), `POST /api/logout` |
| Integration | `POST /api/install`, `GET /api/change_reports`, `POST /api/run/{start,stop,cancel_pending_start}`, `GET /api/releases`, `GET /api/releases/preview?domain=&tag=`, `POST /api/releases/preflight`, `POST /api/updates/check`, `POST /api/installed/<domain>/{uninstall,rollback_full,remove_version}`, `GET/POST /api/registry` |
| Builder / dev | `GET /api/catalog?q=`, `GET /api/build/options`, `POST /api/build/{check,prepare}`, `GET /api/dev`, `POST /api/dev/install` |
| Configuration | `POST /api/flow/start`, `GET /api/flow/progress`, `POST/DELETE /api/flow/<id>`, `POST/DELETE /api/options/<flow_id>`, `GET/POST /api/yaml/<domain>`, `GET /api/entries`, `POST /api/entries/<entry_id>/{options,reload,delete}` |
| Patches | `GET /api/patches/<domain>`, `POST /api/patches/<domain>/upload`, `POST /api/patches/<domain>/<name>/{apply,delete}`, `GET /api/patch_editor/<domain>?name=`, `POST /api/patch_editor/<domain>/{check,save}` |
| MQTT | `GET/POST /api/mqtt/config`, `GET/POST /api/mqtt/rules`, `POST /api/mqtt/{reconnect,republish}`, `GET /api/mqtt/discovery`, `GET /api/mqtt/commands` |
| Entities | `GET /api/entities`, `POST /api/entities/<entity_id>/{rename,name,disable,enable,delete,mqtt_exclude,mqtt_include,mqtt_name}`, `GET /api/devices`, `POST /api/devices/<device_id>/{name,delete}`, `GET /api/services`, `POST /api/services/call` |
| System | `GET /api/ha`, `POST /api/ha/{update,rollback,check}`, `POST /api/restart`, `GET/POST /api/settings` |
| Backups | `GET /api/backups`, `POST /api/backups/create`, `POST /api/backups/upload`, `GET /api/backups/<name>/download`, `POST /api/backups/<name>/{restore,delete}`, `POST /api/backups/restore/cancel` |
| Import | `POST /api/import/upload`, `GET/POST /api/import/inspect`, `POST /api/import/{apply,apply_all,clear}` |
| Cutover | `GET /api/parity`, `POST /api/parity/{test,remove_orphans}`, `POST /api/cutover/{status,enable,undo}` |
| Logs | `GET /api/logs?level=&prefix=&q=&since_id=&limit=`, `GET /api/logs/loggers`, `POST /api/logs/level`, `GET /api/log_files`, `GET /api/log_files/tail?id=&file=&lines=&q=`, `GET /api/log_files/download?id=&file=`, `GET/POST /api/settings` (`log_format`) |
| Diagnostics | `GET /api/diagnostics` (zip, secrets removed), `GET /api/diag/memory[?refs=<type>]` |

## Notes by area

### Status

- `GET /api/mqtt/status`: see the [MQTT reference](mqtt.md).
  `retained_cleanup_pending` is a list of `{base_topic, broker, other_broker,
  deferred, error, since}`, the configured broker's first.
- `GET /api/summary` includes `manager_update`: the running release and the
  newer ones the banner shows.
- `GET /api/status` `health`: the verdict published on MQTT (`state`,
  `reason`, `basis` = the stale basis in use, `since`, `updated_at`; all
  `null` before a boot's first verdict). It is the last verdict built, at most
  a minute old, not a new check.
- `GET /api/status` `watchdog`: the settings below under shorter names
  (`enabled`, `on_degraded`, `after_min`, `min_interval_min`, `max_per_day`),
  plus `restarts_24h`, `attempts`, `window_min` (what the next attempt waits
  through), `gave_up`, `last` (`at`, `integration`, `state`, `reason`,
  `unhealthy_s`, `attempt`, `next`), `reloads_24h`, `max_reloads_per_day`,
  `last_reload` (`at`, `integration`, `state`, `reason`, `unhealthy_s`,
  `entries`, `result`), and while a stretch is timed `pending` (`bad_for_s`,
  `window_s`, `reason`, `state`, `next`: `reload` or `restart`).

### Login

- `POST /api/login` answers `503` with the reason while `HRI_PASSWORD_FILE` is
  empty or unreadable, or `HRI_PASSWORD` holds only spaces or tabs.
- `POST /api/logout` ends every session. If the volume cannot record it, the
  answer is `500` with `ok: false` and the reason: after the next restart the
  sessions from before the logout are valid again and later ones end.

### Integration

`POST /api/run/start` takes `force`
([Updating the integration](../README.md#updating-the-integration)). Without
it, blockers answer `needs_force` with the report in `preflight`, a preflight
that could not run says why in `preflight_note`, and warnings come in
`preflight_warnings`. A tag not in the version store is refused with nothing
to force. `ok: true` means deployed and recorded, not set up: `smoke_test`
says when the health verdict is due (after the restart, if one is needed), the
verdict lands in `GET /api/status` `smoke_test.last`, and `note` says when none
is coming (already running, or the smoke test is off).

`POST /api/installed/<domain>/uninstall` answers `retained_cleared`, and while
its MQTT cleanup waits `retained_cleanup_failed` with `retained_cleanup_error`,
or `retained_cleanup_deferred` (MQTT disabled), plus `retained_cleanup_broker`
(`host:port`) and `retained_cleanup_other_broker` when the settings name
another broker ([MQTT](mqtt.md#when-the-cleanup-cannot-run)).

An unknown entry id, in `/api/entries/<entry_id>/…` or a `reconfigure` flow
start, answers `404` with a message.

### Configuration

The config flow page renders each selector as its control. A number shows as
a box, or as a slider when it asks for one and gives both ends, with its unit
next to the label. A text field that takes several values, and the typed
values of a multi-select that allows them, show one box per item; an item may
contain a comma or surrounding spaces and is sent as typed. A single custom
select value is sent whole. A multi-select in list mode shows as checkboxes, a
value typed into a single select replaces the one picked, and a duration part
may be a fraction. A value the page cannot convert (a fraction in a
whole-number field, broken JSON) is refused under its field.

### Entities and services

- `GET /api/entities` shows only the published attributes, published or not:
  `access_token` and picture URLs with `token=` are left out and a token in the
  state is masked ([MQTT](mqtt.md#entity-document)).
- `POST /api/devices/<device_id>/delete` first asks every config entry of the
  device whether its integration can remove devices, and changes nothing if one
  cannot. If one refuses or fails after another entry was detached, it answers
  `ok: false` with the entries detached so far in the error and in
  `config_entries_detached`.
- `POST /api/services/call` refuses only the never-callable domains
  ([list](mqtt.md#services-that-cannot-be-called)) and takes any target,
  `entity_id: all` and unpublished entities included. A call not answered
  within `HRI_CALL_TIMEOUT` seconds gets `timeout after <n>s (service still
  running)`. At most 50 calls from this endpoint and the Services page run at
  once (apart from MQTT's 50), a timed-out one counting until it returns;
  beyond that: `too many calls in progress (50): try again later`.
- The Services page sends a list for a multiple-choice field and for a text
  field that takes several values, one box per item. It accepts typed custom
  values, one box per value, sent as typed: a comma or surrounding spaces stay
  part of the value. Required fields are checked after the extra JSON is
  merged.

### Settings

`GET/POST /api/settings` carries the [health watchdog](health.md); numbers
outside the range are clamped, not refused:

| Field | Value |
|---|---|
| `watchdog`, `watchdog_on_degraded` | boolean, off by default |
| `watchdog_after_min` | 5–720 |
| `watchdog_min_interval_min` | 15–1440 |
| `watchdog_max_per_day` | 1–24 |

Its `health` object holds per-integration rules, any left out for the default:
`{"<domain>": {"mode": "periodic"|"event", "stale_s": 60–86400,
"unavailable_pct": 1–100, "stale_basis": "reported"|"updated"}}`.

### Home Assistant versions

What the floor and the check mean is in
[Home Assistant and Python versions](home-assistant-versions.md).

`GET /api/ha` answers:

| Field | Content |
|---|---|
| `apt` | what this boot did with `HRI_APT_PACKAGES` (`packages`, `refused`, `ok`, `note`, `error`, `at`); `null` when unset |
| `versions` | the `recent_n` newest stable releases (`recent` is only those), every installed venv, the running, a scheduled and the previous version |
| `versions_total` | how many versions exist in all |
| `all_versions` | with `?all=1`: every stable release plus the local entries |
| `baseline` | the floor, `HA_VERSION_MIN`; anything older is refused |
| `default_version` | `HA_VERSION_DEFAULT`, what a fresh volume installs; not a floor |
| `verdicts` | version → its `check`, where known without resolving (a cached report, or the Python a release needs) |

`?all=1` reads the release list in memory: no PyPI call, no
`X-Requested-With`. While PyPI has never answered in this process,
`all_versions` and `versions_total` fall back to what the box has. `baseline`
is `""` if the image has neither variable, and an image from before the two
were split answers its `HA_VERSION_DEFAULT`.

`POST /api/ha/check` (`{"version": …}`) answers `check`: whether the version's
pins resolve from wheels on this image's Python, installing nothing. Fields:
`version`, `ok`, `checked`, `blockers`, `warnings`, `notes`, `missing` (pins
with no wheel), and when pip ran `python`, `machine`, `requirements`,
`duration_s`. `checked: false` means no answer (pip timed out, PyPI
unreachable, resolver gave up) and `ok` stays `true`; the running version and
one already installed for this Python also answer it, with a note, and run no
pip. A version the floor, `requires_python` or PyPI refuses answers `ok: false`
with the refusal as blocker. The request answers `ok: true` in all these cases;
the verdict is `check.ok`. Cached per version and image Python for an hour.

`POST /api/ha/update` runs the same check first and, without `force: true`,
refuses a blocked version with `needs_force`, the report in `check` and the
blockers in `error`, before any backup or write. `force` skips only that check:
a version below `HA_VERSION_MIN`, one the image's Python cannot run
(`requires_python`) or one PyPI does not list is refused without
`needs_force`. A forced update always puts *scheduled with the dependency
check skipped (force)* on the timeline. The manager device's *Install Home
Assistant* refuses the same and has no force.

### Backups

- `POST /api/backups/<name>/restore` takes `force`: a backup that does not
  record its Home Assistant version answers `needs_force` when `.storage` is
  restored.
- `POST /api/backups/restore/cancel` answers `cancelled` and records it on the
  timeline. It refuses the restore of a scheduled Home Assistant version change
  (`for_version`) and of a full rollback (`rollback`, the backup it restores).

### Import

`POST /api/import/apply` and `/apply_all` answer `alignment`; what its counts
mean is in [Backups](backups.md#import-from-a-home-assistant-backup).

### Cutover

`POST /api/cutover/enable` checks the main Home Assistant, when one is
configured ([Shadow
mode](shadow-mode.md#keeping-the-main-home-assistant-untouched)):
MQTT loaded, no config entries of the integration, and no entity id about to
be announced held by anything but this container's own mirror of that very
entity. A mirror of another entity renamed onto the id blocks like an
unrelated MQTT entity or a leftover of an earlier identity of this container.

The answer carries `checked`: `true` after a checked enable, `false` when the
main HA was not checked. `force: true` skips the checks on the main HA; with
one configured, the answer says `forced: true` and the timeline `forced`. With
no main HA configured the enable goes ahead unchecked: `checked: false`,
`forced: false`, and the timeline says `unchecked`. `force` also skips a
pending smoke test (the answer and the timeline say `smoke_skipped`). It never
skips the container's own checks (an integration running, health, MQTT
connected), an action running (install, start, import, restore, a Home
Assistant version change being prepared), or a restore, full rollback, Home
Assistant version switch or backup import scheduled for the next restart.
`undo` answers `cleared_discovery_configs` and `manager_device_kept`.

`POST /api/parity/remove_orphans` is refused while discovery is off, except for
the manager device while `manager_discovery` announces it. A manager-device
orphan is removed by its component key; a unique id that is no component of
that device is refused.

A matched row of `GET /api/parity` carries `state_comparable`. `button`,
`scene`, `notify` and `event` are never compared by state (no state topic on
the main HA; an event's state is a per-side timestamp), only by availability,
renames and the disabled flag.

### Logs and diagnostics

See [Logs and log files](logs.md).

- `GET /api/logs`: `limit` 1–2000, `since_id` 0–2^63-1, otherwise `400`. The
  answer's `cursor` is the next `since_id`.
- `POST /api/logs/level` (`{"logger": …, "level": …}`) takes any existing
  logger; a new one (a library imported later) needs a dotted Python name, at
  most 50 created.
- `GET /api/log_files` gives each file an `id` that changes at every start.
  `tail` and `download` take `file`, the masked name (a real one is refused; a
  name several files share answers `409`).
- `download` streams the masked file as an attachment under its masked name,
  at most the last 32 MB, with `X-Log-Truncated` when cut.
- `GET /api/diag/memory` runs one probe at a time; a second answers `429`.
