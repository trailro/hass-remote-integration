# Health and the health watchdog

How the running integration is judged, what the smoke test after a start
records, how the watchdog reloads or restarts it when the verdict stays bad,
and the resource history on **Overview**.

## The health verdict

`hass_<domain>/health` carries a verdict: `ok`, `degraded`, `error` or
`stopped` (nothing is running), with the reason, entity counts and when the
integration last wrote a state (see [the health document](#the-health-document)).
With discovery on, your main HA gets a connectivity sensor and a health sensor
for the container. The thresholds are on the **MQTT** page. Mark an integration
that only writes on events as `event`, so silence is not reported as a fault.

Silence is judged for `periodic` integrations only: an `event` one is never
judged on silence, whatever its stale basis. It is measured on the last state
*report* by default: any state write, even an unchanged value. A coordinator
that re-writes every entity on each poll keeps reporting after its connection
dies, so the verdict stays `ok`. For such an integration set the **stale
basis** to `updated` on the **MQTT** page: silence is then measured on the last
changed value or attribute, and the verdict turns `degraded` after *stale*
seconds. Pick a *stale* longer than its quietest normal stretch, or steady
values read as a fault. The document names the basis under `rules.stale_basis`.

For the first *stale* seconds after a start (at most 15 minutes), and again
after the MQTT identity changes, the entity checks (unavailable, silence, no
state yet) are skipped while the entities fill in; the config entries are
still judged. An `ok` in that grace carries `"grace": true`: it says nothing
about the entities, and the watchdog does not count it.

`since` is when this process first published the current verdict: a restart
starts it again, and it is absent while Home Assistant in the container is
starting. A change to `degraded` or `error` is logged once at WARNING, and so
is the first bad verdict after a boot; the way back to `ok` once at INFO.

### The health document

| Field | What it is |
|---|---|
| `integration` | The running integration's domain; `null` when none runs |
| `tag`, `version` | The running version's tag, and the version in its manifest |
| `loaded` | Whether Home Assistant has loaded the domain |
| `entries` | Its config entries: `title`, `state`, `reason`, `disabled_by` |
| `restart_required` | A change waits for a restart of the process |
| `last_error` | The manager's last recorded error, or empty |
| `patch` | The last computed summary of its patches, or `null` |
| `state` | `ok`, `degraded`, `error` or `stopped` |
| `reason` | Why the verdict is not `ok`; empty when it is |
| `entities` | Its enabled entities, those without a unique id included |
| `entities_with_state`, `entities_unavailable`, `entities_unknown` | Of those: how many have a state, and how many are `unavailable` or `unknown` |
| `last_state_update`, `last_state_update_age_s` | The newest changed value or attribute (local time with its offset) and its age in seconds; `null` without a state |
| `last_report`, `last_report_age_s` | The newest state write, even an unchanged value, and its age; `null` without a state |
| `rules` | The health rules in use: `stale_s`, `mode`, `unavailable_pct`, `stale_basis` |
| `grace` | `true` on an `ok` inside the grace after a start; absent otherwise |
| `since` | When this process first published the current verdict |
| `ha_version` | The Home Assistant version in the container |
| `manager_uptime_s` | Seconds since the MQTT side started, or last changed identity |
| `mqtt_published` | Messages this process has published |
| `notifications` | Persistent notifications open in the container |
| `base_topic` | The base topic; `null` while there is none |
| `updated_at` | When the document was built |

With nothing running, the document carries only `integration` (`null`),
`state` (`stopped`), `reason`, `since` and the last six.

## Smoke test

The smoke test after a start ([Start it](../README.md#3-start-it)) reads the
verdict once. An integration with no config entry and no YAML is not judged:
the verdict is `unconfigured`, with no rollback and no notification. A health
check that itself fails (an exception, not a verdict) is retried every minute,
three times, then recorded as `unknown` and never rolled back. A failed,
degraded or unknown smoke test raises a notification and stays as the last
error until another version runs healthy, also across the rollback's restart.

## The health watchdog

The watchdog (on **System**, off by default) acts when the verdict has been
`error` without interruption for a window, 15 minutes by default:

1. It reloads the integration's config entries, the same path as the
   **Reload** button, at most six times a day. An integration with no config
   entry (YAML only), or one out of reloads for the day, skips to step 2.
2. If the verdict is still not `ok` one window after the reload, it restarts
   the process, at most once an hour and three times a day.

| Setting | Default | Range |
|---|---|---|
| `watchdog` | off | |
| `watchdog_on_degraded` (*also on a lasting degraded*) | off | |
| `watchdog_after_min` (the window) | 15 | 5–720 |
| `watchdog_min_interval_min` (between restarts) | 60 | 15–1440 |
| `watchdog_max_per_day` (restarts in 24 h) | 3 | 1–24 |

`error` always counts; `degraded` only with `watchdog_on_degraded` (tick it
with the `updated` stale basis to catch a zombie on a dead source); `stopped`
is your decision. An integration installed but not configured reports `error`
(`not loaded (no config entry, no YAML setup)`); a restart cannot fix that, so
it is left alone. One with YAML stored on the Integration page that did not
load reports `not loaded (no config entry; the YAML setup did not load it)`
and is acted on: with no config entry to reload, the first step is the restart.

**It never fights the rest of the manager.** Nothing is reloaded or restarted:

- while an install, start, stop, backup, import, restore or full rollback runs;
- while a preflight (of an integration or a Home Assistant version) runs, for
  at most an hour, or a manager action from MQTT runs, for at most 30 minutes:
  a hung process is what the watchdog is for;
- while no integration runs or Home Assistant is not running yet;
- while a restore, rebuild, Home Assistant version change, deferred start or
  full rollback waits for the next restart, or an import from a Home Assistant
  backup waits on **System** (a deferred start kept blocked because its Home
  Assistant version did not boot waits for no restart, and does not count);
- while a smoke test is pending, or a config entry is still setting up, for at
  most 15 minutes (or `smoke_test_s`, if longer), the smoke test's limit;
- in the first 15 minutes after a boot.

A reload or restart it decided against puts one line on the timeline for that
stretch, not one a minute.

**It cannot loop.** A reload makes every entity write fresh states, so the
verdict can read `ok` for a minute or two with nothing fixed, and after a
restart the grace reads `ok` for up to *stale* seconds. Neither is a recovery:
only `ok` held for a whole window (`watchdog_after_min`, never doubled) resets
the ladder, the backoff and a give-up. A grace `ok` counts as no verdict at
all; a shorter `ok` ends the stretch, and the next stretch goes on from where
the ladder stood: to the restart after a reload, on the doubled window after a
restart. After a restart the clock starts from that boot, and the window
doubles for each attempt (15 → 30 → 60 → 120 → 240 minutes, then stays). At the
daily maximum it gives up, says so once and waits until the integration has
been `ok` for a whole window, or until restarts age out of the 24-hour count.
All of this survives the restart it triggers, in `state.json`.

Every action is visible: a timeline entry naming the reason (a reload says
`health degraded for 20 min (…): reloading <domain>'s entries`), the attempt
number and the next step; a persistent notification at the boot after a
restart; and `watchdog` in `GET /api/status`, which **System** shows under the
setting.

## Resource history

**Overview** keeps memory, CPU, event-loop lag and volume usage, one sample a
minute, for 48 hours by default and up to 120 (*kept for … hours*). Memory
that keeps growing for hours, or an event loop held for 500 ms or more in
several minutes of the last hour, raises a notification. Every notification
the integration raises goes on the timeline; more than three within two
seconds become one line that counts them and names the first three titles.
