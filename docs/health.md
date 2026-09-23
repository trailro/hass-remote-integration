# Health and the health watchdog

## Health

`hass_<domain>/health` carries a verdict: `ok`, `degraded`, `error` or
`stopped` (nothing is running), with the
reason, entity counts and when the integration last wrote a state. With
discovery on, your main HA gets a connectivity sensor and a health sensor for
the container. The thresholds are on the **MQTT** page; mark an integration
that only writes on events as `event`, so silence is not reported as a fault.

Silence is measured on the last state *report* by default: any state the
integration writes, even the same value again. Some integrations poll through a
coordinator that re-writes every entity on each interval whether or not new
data arrived, so when their connection dies the config entry stays `loaded`,
the reports keep coming and the verdict stays `ok`. For those, set the
integration's **stale basis** to `updated` on the **MQTT** page: silence is then
measured on the last value (or attribute) that changed, and a zombie that only
re-writes the same states turns `degraded` after *stale* seconds. Pick a
*stale* longer than the quietest normal stretch of that integration, or steady
values will read as a fault. The document names the basis in use under
`rules.stale_basis`, and `since` says when the verdict took its current value.
A change to `degraded` or `error` is logged once at WARNING, the way back to
`ok` once at INFO.

### The health watchdog

An integration that goes into `error` at three in the morning stays that way
until somebody looks. The **health watchdog** (on **System**, *off by
default*) steps in when the verdict has been `error` without interruption for a
while: 15 minutes by default. Its first step is gentle: it reloads the running
integration's config entries, the same path as the **Reload** button, which
revives a stuck connection in well under a second. At most six reloads a day. If
the verdict is still not `ok` one window after the reload, it restarts the
process: at most once an hour and at most three times a day. An integration
with no config entry (YAML only) has nothing to reload and goes straight to the
restart, and so does one that has used up its reloads for the day.

`error` always counts. `degraded` counts only when you tick *also on a lasting
degraded* (`watchdog_on_degraded`): a `degraded` version is otherwise kept on
purpose. Tick it together with the `updated` stale basis to catch an
integration that keeps writing the same states on a dead source. `stopped` is
your decision. An integration that is installed but not configured yet also
reports `error` (`not loaded (no config entry, no YAML setup)`); a restart
cannot configure it, so the watchdog leaves that one alone.

It never fights the rest of the manager. Nothing is reloaded or restarted while an
install, start, stop, backup, import, restore or full rollback is running,
while a preflight (of an integration version or of a Home Assistant version)
or a manager action from MQTT is running — each only for as long as it can still
be working: a preflight stops holding the watchdog off after an hour and a
manager action after 30 minutes, because a process that hung is exactly what the
watchdog is for — while no integration runs or Home Assistant itself is not running yet,
while a restore, a rebuild, a Home Assistant version change, a deferred start
or a full rollback is waiting for the next restart, while a smoke test is
pending or a config entry is still setting up, nor in the first 15 minutes
after a boot — the integration gets its whole grace window to set up first. A
reload or restart it decided against puts one line on the timeline for that
stretch, not one a minute.

It cannot loop. A reload makes every entity write fresh states, so the verdict
can read `ok` for a minute or two even when nothing is fixed. That blip does not
reset anything: the ladder only starts again from the reload once the verdict
has stayed `ok` for a whole window. Otherwise the next stretch goes on to the
restart. After a restart the clock starts again from that boot, and the
window doubles for the next attempt (15 → 30 → 60 → 120 → 240 minutes, where
it stops doubling), so a
restart that did not help is not repeated at the same rate. When the daily
maximum is reached it gives up, says so once, and waits: an `ok` verdict
resets the ladder and the give-up, and the daily count drains as the restarts
age out of the last 24 hours. All of that survives the restart it triggers —
it lives in `state.json`, like the smoke test's verdict.

Every action is visible: a timeline entry naming the reason it acted on (a
reload says `health degraded for 20 min (…): reloading <domain>'s entries`), the
attempt number and what it will do next; a persistent notification raised at
the boot after the restart (the restart ends the process that would have shown
it); and `watchdog` in `GET /api/status`, which the **System** page shows under
the setting.

The **Overview** keeps a resource history: memory, CPU, event-loop lag and
volume usage, one sample a minute, for 48 hours by default and up to 120
(*kept for … hours* on the same card). Memory that keeps growing for hours, or
an event loop held for 500 ms or more in several minutes of the last hour,
raises a notification: the usual signs of a leak or of blocking code in the
integration. Every notification the integration raises goes on the timeline;
more than three within two seconds become one line that counts them and names
the first three titles.
