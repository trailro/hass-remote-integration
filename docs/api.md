# API

## API

Every page is backed by a JSON API on the same port, so everything can be
scripted. With a password set, send it as `Authorization: Bearer <password>`. POST
bodies are JSON (`Content-Type: application/json`). Requests that reach out to
the internet or another server, upload files, or return patches, log file
tails, logs or diagnostics, or run or store patch code, also need `X-Requested-With: fetch`: `/api/catalog`,
`/api/patch_editor` (reading, *Check* and *Save*), `/api/patches/<domain>` (and its `/upload`), `/api/backups/upload`,
`/api/import/upload`, `/api/parity`, `/api/releases/preview`,
`/api/diagnostics`, `/api/diag/memory` (also without `refs`), `/api/logs`,
`/api/log_files`, `/api/log_files/tail` and `/api/log_files/download`, and aborting a flow
(`DELETE /api/flow/<id>`, `DELETE /api/options/<flow_id>`); without it they answer `400`.
`GET /api/backups/<name>/download` does not need it: the page sends that
header only through `fetch`, which would hold the whole backup in the
browser's memory before saving it, so the backup link is a plain download the
browser writes to disk as it arrives. Another page can start that download in
your browser (while you are logged in), but cannot read it.
`?refresh=1` on `/api/releases` and `/api/ha` is ignored without it.
`GET /api/status` answers without the header too (for monitors and
`verify.sh`), but then from a copy at most 10 seconds old, since building it
runs the `status(ctx)` of `.py` patches; send the header for a fresh one. The main entry
points:

| Area | Endpoints |
|---|---|
| Status | `GET /api/status`, `GET /api/summary`, `GET /api/manager`, `GET /api/manager/history?hours=`, `GET /api/mqtt/status` (`subscribe_error`; `retained_cleanup_pending`: a list of `{base_topic, broker, other_broker, deferred, error, since}`, the configured broker's first), `GET /api/events`, `GET /api/notifications`, `POST /api/notifications/dismiss_all`, `POST /api/notifications/<id>/dismiss` |
| Login | `POST /api/login` (`{"password": …}`, sets the session cookie; `503` with the reason while `HRI_PASSWORD_FILE` is empty or unreadable, or `HRI_PASSWORD` holds only spaces or tabs), `POST /api/logout` (ends every session; `500` with `ok: false` and the reason when the volume could not record it: every session still ends, but at the next restart the sessions from before that logout are valid again and those issued after it end) |
| Integration | `POST /api/install`, `GET /api/change_reports`, `POST /api/run/{start,stop,cancel_pending_start}`, `GET /api/releases`, `GET /api/releases/preview?domain=&tag=`, `POST /api/releases/preflight`, `POST /api/updates/check`, `POST /api/installed/<domain>/{uninstall,rollback_full,remove_version}` (uninstall answers `retained_cleared`, and while its MQTT cleanup waits: `retained_cleanup_failed` with `retained_cleanup_error`, or `retained_cleanup_deferred` when MQTT is disabled, plus `retained_cleanup_broker` (`host:port`) and `retained_cleanup_other_broker` when the settings name another broker), `GET/POST /api/registry` |
| Builder / dev | `GET /api/catalog?q=`, `GET /api/build/options`, `POST /api/build/{check,prepare}`, `GET /api/dev`, `POST /api/dev/install` |
| Configuration | `POST /api/flow/start`, `GET /api/flow/progress`, `POST/DELETE /api/flow/<id>`, `POST/DELETE /api/options/<flow_id>`, `GET/POST /api/yaml/<domain>`, `GET /api/entries`, `POST /api/entries/<entry_id>/{options,reload,delete}` (an unknown entry id, there or in a `reconfigure` flow start, answers 404 with a message) |
| Patches | `GET /api/patches/<domain>`, `POST /api/patches/<domain>/upload`, `POST /api/patches/<domain>/<name>/{apply,delete}`, `GET /api/patch_editor/<domain>?name=`, `POST /api/patch_editor/<domain>/{check,save}` |
| MQTT | `GET/POST /api/mqtt/config`, `GET/POST /api/mqtt/rules`, `POST /api/mqtt/{reconnect,republish}`, `GET /api/mqtt/discovery`, `GET /api/mqtt/commands` |
| Entities | `GET /api/entities` (an entity's attributes are the published ones whether or not it is published: an `access_token` and a picture URL carrying `token=` are left out of the row as well, and a token in the state is masked, so excluding an entity from MQTT never shows more than publishing it), `POST /api/entities/<entity_id>/{rename,name,disable,enable,delete,mqtt_exclude,mqtt_include,mqtt_name}`, `GET /api/devices`, `POST /api/devices/<device_id>/{name,delete}` (`delete` asks every config entry of the device first whether its integration can remove devices at all, and changes nothing when one cannot; an integration that refuses, or fails, after another entry was already detached answers `ok: false` with the entries detached so far in the error and `config_entries_detached`), `GET /api/services`, `POST /api/services/call` |
| System | `GET /api/ha`, `POST /api/ha/{update,rollback,check}`, `POST /api/restart`, `GET/POST /api/settings` |
| Backups | `GET /api/backups`, `POST /api/backups/create`, `POST /api/backups/upload`, `GET /api/backups/<name>/download`, `POST /api/backups/<name>/{restore,delete}`, `POST /api/backups/restore/cancel` (answers `cancelled`, and the timeline records the cancel; a restore that belongs to a scheduled Home Assistant version change is refused with `for_version`, and a full rollback's restore with `rollback`, the backup it restores) |
| Import | `POST /api/import/upload`, `GET/POST /api/import/inspect`, `POST /api/import/{apply,apply_all,clear}` |
| Cutover | `GET /api/parity`, `POST /api/parity/{test,remove_orphans}`, `POST /api/cutover/{status,enable,undo}`; `enable` takes `force`, which skips the checks on the main Home Assistant (MQTT loaded, the integration's config entries, entity ids held there by anything but this container's own mirrors) but not the container's own (an integration running, health, MQTT connected), nor what would replace the container's configuration under the new entities: an action running (install, start, import, restore, a Home Assistant version change being prepared) or a restore, full rollback, Home Assistant version switch or backup import scheduled for the next restart; the answer and the timeline say `forced`. `force` does skip a pending smoke test, and the answer and the timeline say `smoke_skipped`; the answer carries `checked`, false when the main HA was not checked (forced, or no main HA configured, which the timeline marks `unchecked`); `undo` answers `cleared_discovery_configs` and `manager_device_kept`; a check on the main HA that cannot run (unreachable, its registry unreadable) blocks the enable rather than passing. Removing an orphan while discovery is off is refused, except for the manager device while `manager_discovery` announces it. A matched row carries `state_comparable`: `button`, `scene`, `notify` and `event` are not compared by state (the command-only platforms have no state topic on the main HA, and an event entity's state is a "last triggered" timestamp each side keeps for itself), so those never count as differing; their availability, renames and disabled flag are still reported. An orphan of the manager device is removed by its component key rather than the unique id the removal form used to carry — a unique id that belongs to no component of that device is now refused instead of reported as removed |
| Logs | `GET /api/logs?level=&prefix=&q=&since_id=&limit=` (`limit` 1 to 2000, `since_id` 0 to 2^63-1, otherwise `400`; the answer carries `cursor`, the next `since_id`), `GET /api/logs/loggers`, `POST /api/logs/level` (`{"logger": …, "level": …}`), `GET /api/log_files` (an `id` per file, which changes at every start), `GET /api/log_files/tail?id=&file=&lines=&q=` (`file` is the masked name, answered `409` when several files share it; a real name is not accepted), `GET /api/log_files/download?id=&file=` (the same file selection; the file masked and streamed as an attachment under its masked name, at most its last 32 MB, `X-Log-Truncated` when it was cut), `GET/POST /api/settings` (`log_format`) |
| Diagnostics | `GET /api/diagnostics` (zip, secrets removed), `GET /api/diag/memory[?refs=<type>]` (one probe at a time: a second one meanwhile answers `429`) |

`POST /api/logs/level` accepts any existing logger; a logger that does not
exist yet (a library imported later) needs a dotted Python name, and at most
50 of those can be created.

`GET/POST /api/settings` carries the health watchdog as `watchdog` (a boolean,
off by default), `watchdog_on_degraded` (a boolean, off by default),
`watchdog_after_min` (5–720), `watchdog_min_interval_min` (15–1440) and
`watchdog_max_per_day` (1–24); numbers outside the range are clamped, not
refused. Its `health` object holds the per-integration rules: `{"<domain>":
{"mode": "periodic"|"event", "stale_s": 60–86400, "unavailable_pct": 1–100,
"stale_basis": "reported"|"updated"}}`, any of them left out for the default.
`GET /api/status` answers `watchdog` with the same rules under shorter names
(`enabled`, `on_degraded`, `after_min`, `min_interval_min`, `max_per_day`)
plus `restarts_24h`, `attempts`, `window_min` (what the next attempt has to
wait through), `gave_up`, `last` (`at`, `integration`, `state`, `reason`,
`unhealthy_s`, `attempt`, `next`), `reloads_24h`, `max_reloads_per_day`,
`last_reload` (`at`, `integration`, `state`, `reason`, `unhealthy_s`,
`entries`, `result`) and `pending` (`bad_for_s`, `window_s`, `reason`, `state`,
`next`: `reload` or `restart`) while a stretch is being timed.
`GET /api/status` also answers `health`: the verdict published on MQTT, as
`state`, `reason`, `basis` (the stale basis in use), `since` and `updated_at`
(all `null` before the first verdict of a boot). It is the last verdict built,
at most a minute old, not a new check.

`GET /api/ha` includes `apt`: what this boot did with `HRI_APT_PACKAGES`
(`packages`, `refused`, `ok`, `note`, `error`, `at`), or `null` when the
variable is not set.

`GET /api/ha` answers the version list as `versions`: the `recent_n` newest
stable releases (`recent` is still only those) plus every installed venv, the
running version, a scheduled one and the previous one. `versions_total` is how
many there are in all, and `?all=1` answers every stable release plus those
same local entries as `all_versions` — it reads the release list already in
memory, so unlike `?refresh=1` it costs no extra PyPI call and needs no
`X-Requested-With: fetch`. While PyPI has never answered in this process there
is no release list, so `all_versions` and `versions_total` fall back to what
the box has. `baseline` is the image's floor, `HA_VERSION_MIN` (`""` if the image
has neither variable; an image built before the two were split answers its
`HA_VERSION_DEFAULT` here): anything older is refused, which the page works out
for itself rather than being told once per version. `default_version` is
`HA_VERSION_DEFAULT`, what a fresh volume installs — not a floor, and nothing
is refused for being older than it. `verdicts` maps a version to
the `check` of `POST /api/ha/check` where that is known without resolving
anything — a report still in the hour-long cache, or the Python a release
needs.

`POST /api/ha/check` (`{"version": …}`) answers `check`: whether that version's
pinned requirements resolve from wheels on this image's Python, without
installing anything — `version`, `ok`, `checked`, `blockers`, `warnings`,
`notes`, `missing` (the pins with no wheel), and, when pip actually ran,
`python`, `machine`, `requirements` and `duration_s`. `checked: false` means
the question could not be answered (pip timed out, PyPI was unreachable, the
resolver gave up); `ok` then stays `true`, since nothing was found against the
version. The version running now answers `checked: false` with a note and runs
no pip, and so does a version already installed for this Python ("nothing to
resolve"); a version the floor, `requires_python` or PyPI itself refuses
answers `ok: false` with that refusal as the blocker rather than an error. The
request itself answers `ok: true` in all of those: the verdict is `check.ok`.
The answer is cached per version and image Python for an hour, and only one
check runs at a time.

`POST /api/ha/update` runs the same check and, without `force: true`, refuses a
version it blocks with `needs_force`, the report in `check` and the blockers in
`error` — before it takes a backup or writes anything. `force` skips that check
and nothing else: a version older than the image's floor (`HA_VERSION_MIN`), one this image's
Python cannot run (`requires_python`), or one PyPI does not list is refused
with no `needs_force`, because force cannot rebuild the image. A forced update
puts *scheduled with the dependency check skipped (force)* on the timeline,
whether or not the check would have passed. The manager device's *Install Home
Assistant* action takes all of those refusals and has no force at all.

`GET /api/summary` includes `manager_update`: the running release and the
newer ones the banner shows. `POST /api/run/start` takes `force`; a tag that is
not in the version store is refused without a preflight or `needs_force`. Without
`force`, a start with preflight blockers answers `needs_force` with the report in
`preflight`, a start whose preflight could not run says why in
`preflight_note`, and one that passes with warnings answers with
`preflight_warnings`. `ok: true` from a start means the version was deployed
and recorded, not that it set up: `smoke_test` in the answer says when the
health verdict is due (after the restart, when one is needed), the verdict
itself appears in `GET /api/status` under `smoke_test.last`, and `note` says
when no verdict is coming (the version
was already deployed and running, or the smoke test is off). `POST /api/backups/<name>/restore` takes `force` too: a
backup that does not record its Home Assistant version answers `needs_force`
when `.storage` is restored. `POST /api/services/call` refuses `homeassistant`,
`shell_command`, `python_script`, `hassio` and `integration_manager`, like the
MQTT path; the domains refused over MQTT only (`persistent_notification`,
`recorder`, `logger`, `system_log`, `backup`, `conversation`) stay callable
here, and, unlike MQTT, it is not limited to published entities
(any target, `entity_id: all` included). It is bounded like the MQTT
path: a call that has not answered within `HRI_CALL_TIMEOUT` seconds is answered
`timeout after <n>s (service still running)` while the service goes on running,
and at most 50 calls from this endpoint and the Services page run at once (a
count of their own, apart from the 50 of the MQTT path), a timed-out one counting
until its service returns; beyond that a call is answered `too many calls in
progress (50): try again later`.

---
