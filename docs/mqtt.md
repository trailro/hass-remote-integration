# MQTT reference

What the container publishes to your broker, what it accepts back (commands,
service calls, manager actions), how discovery presents it to your main Home
Assistant, and how retained data is cleaned up.

## Topics

| Topic | Content |
|---|---|
| `hass_<domain>/status` | `online` \| `offline` (retained, last will) |
| `hass_<domain>/health` | retained JSON, every 60 s ([Health](health.md)) |
| `hass_<domain>/<integration>/<domain>/<object_id>` | one retained document per entity |
| `hass_<domain>/<integration>/event_stream/<object_id>` | events of event entities, not retained |
| `hass_<domain>/services/<domain>` | retained service catalog |
| `hass_<domain>/cmd/<domain>/<object_id>/<field>` | commands (used by discovery) |
| `hass_<domain>/call/<domain>/<service>` | service call, JSON payload |
| `hass_<domain>/result/<domain>/<service>` | call result, not retained |
| `hass_<domain>/manager` | retained JSON, every 60 s: updates, resources |
| `hass_<domain>/manager/cmd/<action>` | manager actions (with `manager_commands`) |
| `hass_<domain>/manager/result` | outcome of a manager action, not retained |
| `<prefix>/device/hass_<domain>-<device>/config` | HA device-based discovery (`hass_<domain>_<device>` on a volume that published with 0.26.0 or older: [Id format](#id-format)) |

An integration named `call`, `cmd`, `result`, `services`, `manager`, `health`
or `status` publishes under `<name>-integration/<domain>/<object_id>`, so its
documents never land on the command, call or result topics.

`hass_<domain>` in these topics is the base topic, which is also the client id
and the start of every discovery id: `hass_<domain>-<instance>` for a container
with `HRI_INSTANCE` set ([Identity](#identity)).

## Identity

The identity is the name everything published derives from: the base topic,
the client id, the availability topics, the discovery ids and unique ids
(`hass_<domain>-<entity_id>`, `hass_<domain>-<instance>-<entity_id>`; `hass_<domain>_<entity_id>`
on a volume that published with 0.26.0 or older: [Id format](#id-format)), and the
manager device (`hass-remote-integration (<identity>)`, `sensor.<identity>_health`,
...). It is `hass_<domain>` for the running integration, or
`hass_<domain>-<instance>` when the container has `HRI_INSTANCE=<instance>`.
The `-` is in no integration domain (Home Assistant's and HRI's allow only
`a`-`z`, `0`-`9` and `_`), so an instance never takes another integration's
name: `hass_hri-probe` (integration `hri`, instance `probe`) is not
`hass_hri_probe` (integration `hri_probe`). The main HA builds entity ids from
names with `-` turned into `_`, so there an instance's manager device may ask
for the same entity id as another integration's (`sensor.hass_hri_probe_health`);
the one created second gets a suffix, as below. The MQTT page shows the base
topic in use and where it comes from; `GET /api/mqtt/status` says the same in
`identity_source` (`default`, `instance`, `remembered`, `invalid`, or `null`
with nothing running), `identity_instance`, `identity_problem` (why MQTT has no
identity) and `identity_warning` (an `HRI_INSTANCE` problem a remembered
identity leaves unused, or why discovery waits for the [id format](#id-format)).

### The rule

1. If this volume already published the running integration, it keeps the
   identity it published under (`remembered`), whatever `HRI_INSTANCE` says
   now: the names on the broker and the entities on the main HA stay as they
   are.
2. Otherwise it is `hass_<domain>-<HRI_INSTANCE>` (`instance`), or
   `hass_<domain>` with `HRI_INSTANCE` unset or empty (`default`).

What a volume published is recorded in `integration_manager/mqtt_identity.json`
at every connection, with the integration it belongs to. A new identity counts
for rule 1 only once a connection under it has held for more than 10 seconds
(`pinned`): a connection the broker drops at once (another client with the same
client id takes it back, a packet over the broker's maximum) or a login it
refuses keeps nothing, so correcting `HRI_INSTANCE` after such a start still
applies. A record written by
0.25.x or older names no integration: it counts for the integration whose
plain `hass_<domain>` it holds. So an install that published as
`hass_hri_probe` keeps `hass_hri_probe`, its client id and its discovery unique
ids, and with them its entity ids on the main HA, after an update that sets
`HRI_INSTANCE` (an HRI Manager instance: [Home Assistant OS app](app.md#hri-manager-instances)).
An integration this volume never published (a fresh volume, another
integration, a volume that only ran with MQTT disabled) takes rule 2. The
manager's own backups leave `mqtt_identity.json` out ([Backups](backups.md)), so
a backup restored into another container takes that container's identity.

Only a missing `mqtt_identity.json` is a volume that never published. One that
cannot be read, is not JSON, or has a shape no version wrote (a hand edit, a
damaged volume; a record without an integration is accepted only as exactly
what 0.25.x wrote: `base`, `prefix`, `broker`, with a plain `hass_<domain>`)
gives no identity at all: MQTT stays disconnected with the reason
(`identity_problem`, the MQTT page, the log, the timeline, and the preflight of
every install), rather than taking a new identity that would leave the old
names on the broker and have the main HA create every entity again. Correct the
file, or remove it (the identity is then chosen as for a volume that never
published, and the plain `hass_<domain>` keeps the `_` ids its retained
discovery configs still hold: [Id format](#id-format)), then save the MQTT
settings or press Reconnect: the file is read again. The record is written
with an fsync of the file and its directory, and only when it changes.

`HRI_INSTANCE` is 1 to 32 characters of `a`-`z`, `0`-`9` and `_`, not starting
with `_`; every HRI Manager instance name fits. Any other value is refused: it
is never used and never replaced by the plain name. An integration that would
take it (rule 2) gets no identity: MQTT stays disconnected (`connect_error`,
`identity_problem` and the MQTT page name the value and why), the log says so as
an error and the timeline at every start, and the preflight of every install
lists it as a warning. An integration that keeps a remembered identity (rule 1)
does not need it: MQTT connects under that identity, and the same places show
the value as a warning (`identity_warning`, a warning in the log). The
integration itself runs either way; fix or remove the variable and restart.

In the Home Assistant OS app the instance name comes from the app's slug, which
the app reads from the Supervisor at start. When the Supervisor did not answer
(and `HRI_INSTANCE` is not set), the app runs with `HRI_INSTANCE_UNKNOWN=1`,
and the name counts as invalid in the same way: an integration with a
remembered identity keeps connecting, one without stays disconnected with "the
app's slug could not be read from the Supervisor ...: restart the app" rather
than take the plain name, which another container may hold, and keep it.

### Id format

Unique ids, device identifiers and device discovery ids are the identity, a
separator and the rest: `hass_demo-sensor.power`, `hass_demo-<device id>`,
`hass_demo-demo_nodevice` (the device-less entities of the integration `demo`).
The manager device's discovery id is `<identity>_manager` in every format. An
instance always uses `-`. The plain identity `hass_<domain>` uses:

- `-` on a volume that never published it: `"id_format": 2` in
  `mqtt_identity.json`.
- `_` on a volume that published it with 0.26.0 or older
  (`hass_demo_sensor.power`), for good: its record has no `id_format`, and gets
  `"id_format": 1` at the next connection. Nothing it announced changes and
  nothing is migrated: unique ids, device identifiers, discovery topics and the
  entity ids on the main HA stay what they were. Before that connection the
  container reads the retained discovery configs once: configs of this
  identity's exact origin with `-` ids mean it started with `-` and was rolled
  back to 0.26.0 (see [Rolling back](#rolling-back-to-a-release-without-instances)),
  and it keeps `-` (`"id_format": 2`, source `scan`). A read that is not
  complete leaves it `_`, with a warning in the log.

Why: a `_` is also inside domains, so with `_` the ids of two plain identities
can meet. `hass_a_` + `binary_sensor.x` is `hass_a_binary_` + `sensor.x`, and
`hass_a_` + `binary_demo_nodevice` is `hass_a_binary_` + `demo_nodevice`: the
Cutover page of container `a` took container `a_binary`'s entities for orphans
of its own, and **Remove orphans** sent an empty config to `a_binary`'s device,
which deleted its entities on the main HA. No domain, entity id, device id or
instance name holds a `-`, so with `-` no identity's ids are another's. Moving
an existing volume to `-` would have the main HA create every entity again (new
unique ids), which is why a volume keeps the format it published.

When the record does not say (no `mqtt_identity.json`: a fresh volume, or one
whose damaged file was removed; or a [Move](#two-instances-of-the-same-integration)
back to the plain identity, when the record holds the instance), the
connection first reads the retained discovery configs under the discovery
prefix: one with this identity's exact origin and `hass_<domain>_` ids means
this volume published them, and it keeps `_`; none means `-`. So removing a
damaged record does not make the main HA create the entities again, and a Move
back to the plain identity keeps `_` while what it left is still retained.

That read needs the broker to let the container subscribe to
`<prefix>/device/+/config` and to deliver the retained configs there. A broker
whose ACL silently drops those messages makes the read find nothing, so a
volume that published `_` ids and lost its record takes `-`, and the main HA
creates every entity again as a duplicate: give the container's MQTT user read
access to the discovery prefix, or change the format on the MQTT page (below).

When the read is not complete (the broker refuses the subscription, the
connection of the read drops, its time limit ends it while configs are still
arriving, or it stops at its maximum of 64 MB), the record gets
`"id_format": 0` and the documents are published, but the container announces
no discovery config and removes none over its connection: the orphan sweep and
the removal of deleted or excluded entities wait. An entity excluded or deleted
meanwhile stays on the main HA until the format is decided; the orphan sweep
then removes it (at the earliest five minutes after the start). Undo on the
Cutover page (and turning discovery off), a Move with clear, and an uninstall
still clear the retained configs that carry this identity's exact origin,
through a client of their own, whatever the format. The read is tried again by
every full republish (the one after each connection to the broker, paho's
automatic reconnects included, one every `full_republish_interval_min`
minutes, and **Republish**), but not within a minute of the previous read;
only a new connection of the manager (a restart, saving the MQTT settings)
reads at once. `identity_warning` (the MQTT page, `GET /api/mqtt/status`
with `ids_undecided`) and the preflight say why, and the status names no
`prefix` meanwhile. The Cutover page compares
nothing meanwhile: `GET /api/parity` answers `ids_undecided: true` with the
reason, and `POST /api/cutover/status` says it in `ids_undecided`.

The MQTT page shows the format and where it came from (`id_format` and
`id_format_source` in `GET /api/mqtt/status`: `recorded` by 0.26.0 or older,
`scan` from this identity's retained configs (also for a record of 0.26.0 or
older that its `-` configs overrule), `scan_empty` from a read that
found none of them, `chosen` on the page, `instance`); `mqtt_identity.json`
keeps the source next to the format as `id_format_source`, except for
`recorded`: a record of 0.26.0 or older gets `"id_format": 1` with no
`id_format_source`, and the status derives `recorded` from its absence. Where the read cannot decide (a refused
subscription or a store over the maximum fails every time), the page offers
**Keep hass_<domain>_…** (`1`) if this volume, or the one it was restored from,
announced the plain identity to the main HA with 0.26.0 or older, and **Use
hass_<domain>-…** (`2`) if it never did (`POST /api/mqtt/id_format`
`{"format": 1}`; `id_format_choosable` in the status). A wrong choice makes the
main HA create every entity again, as duplicates.

Where the read decided wrong (an ACL that hides the configs, or a broker that
lost its retained messages, reads as `scan_empty` and gives `-`), **Change id
format** on the MQTT page switches a decided plain identity to the other
format, behind a confirmation (`{"format": 1, "confirm": true}`; without
`confirm` a change is refused; `id_format_changeable` in the status). The
format already in use changes nothing (`"changed": false`): the record, its
source and what is announced stay as they are. Use it
only when the automatic decision was wrong: it re-creates this container's
entities on the main HA. The new format is recorded at once; the discovery
configs of this identity's exact origin in the old format are cleared, then
everything is announced again in the new one, so the main HA deletes the old
entities first. Entity ids stay only if the old entities are gone first, and
areas, names and labels set on the main HA do not follow. Over a live
connection this happens at once; otherwise at the next connection. The file
is read again before any choice. An instance has no choice: it always uses
`-`.

The manager's backups leave `mqtt_identity.json` out, so a restore into
another container takes that container's identity; with it goes the id format.
A restore onto a fresh volume decides the format from the broker, as above: the
configs this identity left retained keep `_`. If the broker lost its retained
configs too, nothing tells and the read gives `-`: change it on the MQTT page
if the volume restored had `_` ids.

What stays: two containers whose plain identities were both published with
0.26.0 or older, of domains like `a` and `a_binary`, keep ids that one can take
for the other's. The Cutover page may list the other's entities as orphans, but
no container deletes another's entities: **Remove orphans** clears a device this
container does not announce only when its retained config on the broker carries
this container's exact origin (the others are skipped and named in `not_ours`),
and the orphan sweep, Undo, a Move and an uninstall only ever take configs of
that exact origin. Only where both announce the very same id do they still
meet: `a`'s `binary_sensor.x` and `a_binary`'s `sensor.x` are both
`hass_a_binary_sensor.x`, and the main HA keeps one entity for that unique id;
an integration `binary_demo` without devices in `a` and `demo` in `a_binary`
share one discovery config, which each overwrites. A Move to an instance
identity ends it, at the cost of new entities on the main HA.

### Two instances of the same integration

Two containers running the same integration on one broker need different
identities: with the same one they take each other's connection (one client id)
and clear each other's retained data. Set `HRI_INSTANCE` in one of them, or in
both, to different values (an HRI Manager instance derives it from its app slug,
`local_hri_<name>`; the manager sets nothing).
`hass_demo` and `hass_demo-garage` share no topic, client id, discovery id or
unique id, so both mirror to one main HA side by side. Their entities there
take the ids they have in each container, so of two with the same id the one
created second gets a suffix (`switch.door_2`).

The container that already published keeps its names (rule 1). To give it the
instance identity on purpose, use **Move to hass_<domain>-<instance>** on the
MQTT page (or `POST /api/mqtt/move_identity` with `{"to": "<identity>"}`,
the identity the page shows, and `"clear": true` for the second choice below),
while MQTT is connected. The main HA creates the entities and devices again
under the new identity; what was customised there (areas, names, labels) does
not follow, and the manager device's entity ids change with the name. What
happens to the old names is your choice:

- **Leave them** (the default): nothing is cleared. The old names are marked
  offline, so the main HA keeps those entities and devices, unavailable, until
  you delete them there; while they exist, a new entity whose id one of them
  holds gets a suffix (`switch.door_2`). HRI forgets the old names: an
  uninstall no longer clears them.
- **And clear hass_<domain> on the broker**: everything retained under the old
  identity is cleared, as a change of integration does, and **the main HA
  deletes those entities and devices**. The clear takes whatever sits under
  the old names with HRI's shape, so **it also deletes the entities of any
  other container that publishes under the same old name** on that broker
  (the case that makes a Move necessary: two containers of one integration).
  Clear only when this container is the only one that published there.

Nothing else moves an identity: removing or changing `HRI_INSTANCE` later keeps
the one recorded. The Move offers whatever the rule gives now, so it also takes
a container back to the plain `hass_<domain>` after `HRI_INSTANCE` was removed.

### Rolling back to a release without instances

A release before the one that brought `HRI_INSTANCE` (0.25.x or older) knows
only `hass_<domain>`. Rolling HRI back to one after a container published under
`hass_<domain>-<instance>` moves it back to the plain name: that release clears
what the recorded instance identity left retained (so the main HA deletes those
entities and devices, with their customisations) and publishes everything
again as `hass_<domain>`, where the main HA creates the entities anew. If
another container publishes the same integration as plain `hass_<domain>` on
that broker, the two then take each other's connection and clear each other's
retained data. Updating again keeps the plain name, the one published last;
Move goes back to the instance identity.

A volume whose plain identity uses `-` ids (`"id_format": 2`: it first
published with a release after 0.26.0) announces `hass_<domain>_` ids once
rolled back to 0.26.0 or older: the main HA takes them for new entities, which
get `_2` entity ids while the others hold theirs. Roll such a volume back with
discovery off. With 0.26.0 itself both sets stay until you delete one on the
main HA. 0.26.0 rewrites `mqtt_identity.json` without `id_format`; after an
update again the container finds its own `-` configs still retained, keeps the
`-` ids, and the orphan sweep (five minutes after the start) clears the `_`
configs 0.26.0 announced: the main HA deletes the `_2` entities and keeps the
original ones. Where 0.26.0 announced the manager device (its config is one
topic in both formats), that config holds its `_` identifier: the first
connection clears it with the other `_` configs, as **Change id format** does,
and announces it again a moment later, so the main HA deletes that device and
creates the manager device again (its customisations do not follow) instead of
keeping it empty next to the new one. That needs the read at the first connection to be complete and
the `-` configs to be still retained; otherwise the volume keeps the `_` ids,
the sweep clears the `-` configs, and the main HA keeps the `_2` entities. Use
**Change id format** (to `2`) on the MQTT page before the sweep to keep the
original ones then.

## Connection

### Protocol

The container connects with MQTT 5, so the broker can announce its largest
packet. A broker that refuses MQTT 5 gets MQTT 3.1.1, logged and on the
timeline. `protocol` in `GET /api/mqtt/status` and the MQTT page show which.

### Subscriptions and `online`

At every connection the container subscribes to `cmd/#`, `call/#` and
`manager/cmd/+`. If the broker refuses (an ACL that allows publishing but not
subscribing), documents still go out but no command, call or manager action
arrives. The refused topics and the reason are logged and put on the timeline
once per connection the container opens (saving the MQTT settings, an identity
change, a restart; not per library reconnect), and shown in `subscribe_error`
and `connect_error` of `GET /api/mqtt/status` and next to *connected* on the
MQTT page. Ending a connection itself clears both. Only a stated refusal is
visible: mosquitto's `acl_file` grants every subscription and silently drops
what the client may not read (its dynamic security plugin refuses).

`status` turns `online` only once the broker has answered the subscription, so
a command sent the moment the device becomes available is received. A refused
subscription still turns it `online`; no answer within 10 seconds turns it
`online` too, reported like a refusal until the answer comes. An `online` never
follows the retained `offline` the container sends when it ends a connection.

The client library logs under
`custom_components.integration_manager.mqtt_publisher.paho` (INFO and up; DEBUG
traces packets with topics and sizes, never the password or a payload).

### Refused login

A refused connection is logged and put on the timeline once: `the broker
refused the login: Not authorized` or `… the login: Bad user name or password`
(as the broker sends it) for wrong credentials, `the broker refused the
connection: <reason>` otherwise. `connect_error` and the MQTT page keep that
reason while the library retries, instead of its `Unspecified error`.

A connection the broker accepts and drops within ten seconds is reported as the
broker closing it (usually a packet over its maximum), not as a TLS problem.

### TLS

Tick `tls` on the MQTT page (usually port 8883). The certificate is verified
against the system CAs, or `ca_certs`, a CA file inside `/config` (for example
`/config/mqtt-ca.pem`). `tls_insecure` skips only the host-name check: anyone
with a certificate from that CA can then pose as the broker and read the
credentials. A failed check shows on the MQTT page with its reason (`TLS
handshake failed: unable to get local issuer certificate`), rechecked at most
once a minute. The foreign-data check and every cleanup connect the same way.
Client certificates are not supported. Without `tls` the password travels
unencrypted ([Security](security.md)).

### Foreign data under the base topic

The container refuses to connect while *foreign* retained data sits under its
base topic (override with `force_base_topic`). A discovery prefix that is the
base topic, or lies under it, is refused when the MQTT settings are saved.

## Limits

| What | Limit | When exceeded |
|---|---|---|
| Document published | broker's announced maximum, else 1 MiB (always 1 MiB on 3.1.1) | skipped, not sent; see below |
| Command or call payload | 256 KB, JSON nested at most 64 levels | refused unread, with the reason |
| Packet the container accepts (MQTT 5 only) | 320 KB (256 KB payload plus topic) | the broker drops it: no answer at all |
| Call result | broker's maximum packet | sent without its response data, `ok: false` and the reason |
| `_id` of a call | 128 bytes as JSON | refused, answer carries the id cut short, call does not run |
| `_id` recovery from a rejected payload | first 256 KB, not parsed | — |
| Masking of a payload | first 4 KB of text (every JSON key still found) | history, status and log line show it cut there |
| Retained data read by the base-topic check and sweeps | 64 MB | the log says how much was unread |
| Unknown manager action name | 40 characters | cut in the answer, its error and the history |
| Calls and commands running at once over MQTT | 50 | see [Concurrency](#concurrency) |

An oversized document is skipped because the broker would close the
connection and the client would replay it on every reconnect. It is named in
the log and on the timeline, counted in `oversized_skipped` and
`last_oversized` of `GET /api/mqtt/status`, and shown on the MQTT page.

MQTT 3.1.1 cannot announce the 320 KB maximum, so any payload is read before it
is refused: limit it on the broker (`max_packet_size` in mosquitto). The scans
(base-topic check, cleanup of stale and excluded documents) use short-lived
connections that announce no maximum, to see retained documents of any size. A
sweep that read less removes less, never something else.

## Entity document

Each document holds the state, the attributes, `last_changed`,
`last_updated`, `last_reported`, and the registry metadata (unique id, name,
device class, unit, icon, category, device).

Access tokens are left out of the attributes at any depth, because they would
open this container's proxy: `access_token`, and any value whose URL carries
`token=` (the `entity_picture` of a camera, image or media player, also inside
a list or nested attribute). A state that is such a URL is published with the
token replaced by `***`.

**Every other attribute and every state is published as the integration sets
it**, under any name, because the main Home Assistant needs it as it is
(`code_format`, `error_code`, a GPIO `pin`). The [masking](#masking) of the
command history and logs does not apply: an integration that puts a password in
a state or attribute publishes it to anyone who may read the base topic.

Every entity of the container's Home Assistant gets a document, except:

- entities of the integrations in `exclude_integrations` (default
  `["integration_manager"]`; set with `POST /api/mqtt/config` as a list or
  comma-separated text). Their services are left out too, see
  [Service calls](#service-calls);
- entities excluded by a rule;
- `zone` entities, which every Home Assistant creates by itself (`zone.home` is
  the container's own home location). One published by 0.17.0 or older is
  removed from the main HA five minutes after the start, like any excluded
  entity.

`entities_total` in `GET /api/mqtt/status` counts the entities with a state
that get a document.

A vacuum's document also has `fan_speed` and `state` at the top level, where
the main HA's MQTT vacuum reads them. That `state` is `null` when it is not one
of the platform's six activities (`unknown`, `unavailable`), which the main HA
would drop and keep showing the old activity. Since that `state` no longer
says `unavailable`, the document also has `availability` (`online` or
`offline`), which the vacuum's availability reads: an unavailable vacuum is
unavailable on the main HA, an unknown one available with no activity.

A `device_tracker` with a latitude and a longitude sends the reset payload on
its state topic, so the main instance places it in **its** zones (a state there
is a location name that overrides zones, and the container's `zone.home` sits
at 0,0). A tracker without coordinates (a router, Bluetooth) sends its own
`home`/`not_home`.

## Commands

- Numeric command topics accept only finite numbers. Text values, notify
  messages and select options are used exactly as sent, spaces included.
- The two bounds of a thermostat range arrive as two commands and become one
  service call: the first waits up to 1 s for the second.
- An alarm panel or lock with a code asks for it on the main HA and sends it
  with the action; a lock's commands carry `{"action": …, "code": …}`.
- A JSON payload on a vacuum's `send_command` topic needs a `command` string;
  its other keys are the parameters (`{"command": "spot_area", "rooms": [1]}`).
  A lone `params` object is used as the parameters. Any other payload is sent
  as the command name.
- The state topic of a switch, light, fan, siren or humidifier takes only
  `ON`/`OFF`, `TRUE`/`FALSE` or `1`/`0` (any case, surrounding spaces ignored).
  Anything else is refused, not read as *off*, with the reason under *recent
  commands* and in the log.
- A fan's oscillation topic takes only `oscillate_on` and `oscillate_off`;
  anything else is refused with the accepted list.
- Action tokens of a cover, valve, lock, alarm panel, vacuum or lawn mower
  match in any case; an unknown one is refused with the accepted tokens.
- Tilting a cover open or closed on the main HA arrives as tilt position 100
  or 0. When the cover here cannot set a tilt position, those become
  `cover.open_cover_tilt` and `cover.close_cover_tilt`. A position in between
  is still sent as a position, which such a cover refuses (the main HA offers
  the slider anyway).
- A command for an entity this container does not publish is refused with the
  reason. This is checked again right before the service call, so an entity
  excluded while its command was on its way is not acted on.

### Retained commands

A command, service call or manager action published with `retain` is never
carried out, because a physical effect must not replay at every reconnect. The
retained message is cleared from the broker as it arrives, and the log names
the topic (it does not appear under *recent commands*).

On an MQTT 3.1.1 broker this holds for a retained command found when the
container subscribes (at every connection). One published while the container
is connected arrives without the retain flag, runs once, and its retained copy
is cleared at the next connection. 3.1.1 has no `noLocal`, so the empty payload
that clears a retained command comes back: the container drops that one echo.
A second empty payload on the same topic, or one more than 30 s later, is a
command again. On MQTT 5 nothing is remembered: an empty payload is always
somebody else's command.

### Masking

What is masked appears as `***` in the command history (`GET
/api/mqtt/commands`), the status document and the log:

- the value sent to a `text` entity in password mode, on any of its command
  topics, with `text.set_value` over MQTT or from the Services page, also
  inside a service error that quotes it (the result sent to the caller keeps
  it), also when the call is refused, and whether it is sent as text or as a
  number; a `text.set_value` payload that does not parse to a JSON object
  cannot tell which entity it is for and is kept as `***` whole;
- values of keys named like a secret, by the names and rule in
  [Security](security.md#mqtt-command-history), also in a rejected command's
  reason where it quotes the payload;
- a service's error message, run through the same rules as the Logs page
  ([Security](security.md)): a password, a `?token=` URL, an `Authorization:
  Bearer …`, or a credential after an auth scheme in what the caller sent
  (`token: Basic …`). An ordinary failure stays readable word for word;
- a manager action's `error` and `note`, by the Logs page rules, in its
  history row, on `manager/result`, in the retained manager document and on
  the timeline.

Of what the container publishes, only its own words are masked (these, and
the health document's reasons and `last_error`), never an entity's state or
attributes ([Entity document](#entity-document)). Masking reads at most 4 KB
([Limits](#limits)), so it never holds up the connection.

## Service calls

Publish a JSON object to `call/<domain>/<service>`: service data plus optional
`entity_id` and an optional `_id`. A call with no data needs `{}`; an empty or
whitespace-only payload is rejected. The result comes back on `result/...`,
with a `response` key for a service that returns response data (the catalog
marks those `"response": "optional"` or `"required"`). A call that fails inside
the container is answered `ok: false` with `internal error (<exception type>)`;
the log names where, never the message, which may quote the data.

`NaN`, `Infinity`, numbers too large to be finite (`1e999`), oversized or
too-deep payloads and JSON that does not parse are rejected. The answer on
`result/...` still carries the `_id` when the outer object names one as a
string, a finite number, `true`, `false` or `null`.

### `_id` and repeats

- An `_id` over 128 bytes is refused ([Limits](#limits)).
- A repeated `_id` within five minutes is answered from memory, never executed
  twice. The latest 1000 are kept. The type counts: `1` and `"1"` differ.
- A call refused before it reached the service (an unknown service, which an
  integration still loading at boot answers for a moment; a target that does
  not exist here; too many calls in progress) does not remember its `_id`, so
  resending it runs. An internal error is remembered.
- While the service still runs, also after a timeout answer, a repeat gets
  `ok: null`, `state: running` and `duplicate: true`; once it ends, the final
  result, flagged `late` when it ended after the timeout.

### Concurrency

At most 50 service calls and commands received over MQTT run at once, a
timed-out call counting until its service returns. Beyond that a call is
answered `too many calls in progress` (a retry with the same `_id` runs once
there is room) and a command is rejected. The Services page and
`POST /api/services/call` have 50 of their own, counted apart.

### Services that cannot be called

| Services | Over MQTT | Services page, `POST /api/services/call` |
|---|---|---|
| `homeassistant`, `shell_command`, `python_script`, `hassio`, `integration_manager` | never | never |
| `persistent_notification`, `notify.persistent_notification`, `recorder`, `logger`, `system_log`, `backup`, `conversation` | no, and not in the catalog | yes |
| any domain in `exclude_integrations` | no, and not in the catalog | yes |

`recorder`, `logger`, `system_log`, `backup` and `conversation` exist here
only when the running integration depends on them. They take no target, so the
published-entity check cannot limit them, and they purge history, change log
levels, write or clear log entries, back up all of `/config`, or run a sentence
against every entity. Any other service that takes no target (an integration's
own, a legacy `notify.<name>`) is callable as registered.

Home Assistant does not record which integration registered a service, so what
an excluded integration registers under another domain (a legacy
`notify.<name>`, a `tts.<engine>_say`, a platform's service) stays callable.
An entity service reaches only published entities, so never its entities.

### Targets

A call reaches only entities the container publishes. Refused: an
`entity_id` of `all`; an entity, group (and its members), area, floor, label or
device that resolves to an excluded or unknown entity; a target that cannot be
read (an id that is not a string). An area, floor, label or device is measured
only against the entity domains the service can act on, since Home Assistant
hands an entity service only its own component's entities: a room that also
holds unpublished entities does not refuse `light.turn_on`. A service that is
not an entity service keeps the strict check.

Entity ids in the service data count too, at any depth: fields ending in
`entity_id` or `entity_ids`, and `group_members`, `snapshot_entities`,
`entities`, `add_entities` and `remove_entities` (a list or a mapping keyed by
entity id). **An entity id under any other field name is not recognised**, so
do not rely on excluding an entity to keep it from such a service. A
`device_id` that is not a Home Assistant device (a hardware address) stays
plain service data.

The Services page and `POST /api/services/call` are not limited to published
entities ([API](api.md)).

## Discovery

Off by default. One retained config per device. Entities of every domain that
has an MQTT platform become native entities with working commands; the rest
(cameras, media players, weather, …) are mirrored as read-only sensors with all
attributes. A mirror is a sensor, so a `config` entity category (usual on
`date`, `time` and `datetime`) is published as `diagnostic`: the main Home
Assistant refuses a sensor that has it.

### Rules

Per-entity rules on the Entities page, or as JSON with `GET/POST
/api/mqtt/rules` (`{"rules": {"<entity id or glob>": {...}}}`), change only what
is published:

| Field | Value | Notes |
|---|---|---|
| `exclude` | true/false | an entity excluded while the container was down is removed from the main HA by the orphan sweep, five minutes after the start ([After a restore](#after-a-restore-import-or-rebuild)) |
| `name` | text | |
| `enabled_by_default` | true/false | reaches an entity the main HA already has only after it restarts |
| `entity_category` | `config` or `diagnostic` | refused when the main HA would not take it for the published platform; reaches an existing entity only after the main HA restarts |
| `device_class` | a device class | refused when the main HA does not take it for a matched entity, or the unit does not fit (`W` with `temperature`) |
| `icon` | `mdi:<name>` | |

A refused `device_class` or `entity_category` comes with the reason, because
the main HA would refuse the whole device config. The main HA refuses a
`sensor` or `binary_sensor` with category `config` ("cannot be added as the
entity category is set to config"), and parity would show it as permanently
missing. When a glob later matches an entity the class or category does not
fit, that entity is announced without it, with a warning in the log (once for
the category), and the rest of the rule applies.

When `integration_manager/mqtt_rules.json` cannot be read (not valid JSON, not
a rules object, a read error, or an invalid rule), which entities it excludes is unknown, so the
container fails closed: it does not open its connection to the broker,
publishes nothing and takes no command (an uninstall cleanup still pending
clears its retained topics on short connections of its own). The main HA
keeps its entities, unavailable after the retained `offline`. `GET
/api/mqtt/status` names the problem in `rules_error` and `connect_error`, the
MQTT page shows it next to the rules, and the log says it. The damaged file
stays where it is. Invalid optional metadata never silently drops an exclusion: an invalid rule
blocks connecting too. Legacy invalid `device_class` values are still ignored while the rest
of their valid rule applies. When the file is not valid JSON, holds no rules object or has an invalid rule, a copy
is kept as `mqtt_rules.json.corrupt-<time>` (the newest 3, mode 600); a file
that cannot be read at all gets none. Rule
changes from the Entities page or `POST /api/mqtt/rules` are refused with the
same reason, so none overwrites the file. Fix the file, or remove it to start
over without rules, then press *Reconnect*, save the MQTT settings or
restart: the rules are read again before connecting.

### Collisions

When two entities of one device would get the same component key
(`image_processing.x` and `image.processing_x`), the second is skipped with a
warning; `discovery_collisions` in `GET /api/mqtt/status` counts them. When two
ask for the same entity id on the main HA (a mirrored `camera.front` becomes
`sensor.camera_front`, next to a real `sensor.camera_front`), both are
announced, the main HA gives one a `_2` suffix, the log names them, and
`discovery_default_id_duplicates` counts them.

### `main_ha_version`

The Home Assistant version that consumes this discovery, for example `2026.4`.
Empty (the default) means current: nothing is left out. Set, discovery leaves
out what that version cannot parse: an unknown device class is dropped from the
component (the entity still arrives), and a domain without an MQTT platform
there is mirrored as a read-only sensor. The main HA ignores an unknown key but
rejects the **whole device's** payload over an unknown platform or device
class: one `radon` sensor takes every entity of its device with it.

`discovery_compat_device_classes_dropped` and
`discovery_compat_platforms_mirrored` in `GET /api/mqtt/status` count what the
last pass left out (the second is a subset of `discovery_mirrored`); the MQTT
page shows both. Each distinct drop is logged once at INFO, and again after the
setting changes.

What each version knows comes from `ha_compat.json`, generated from the
published Home Assistant wheels by `tools/gen_ha_compat.py` (2025.1 to 2026.9,
generated 19 September 2026). A version at or above the newest release in it
filters nothing, and neither does a value that is not a version. One below the
oldest row is filtered as 2025.1 (device discovery needs 2024.11 anyway). See
[What the main Home Assistant needs](#what-the-main-home-assistant-needs).

### States on the main HA

- A light, fan, siren or humidifier whose state is `unknown` stays unknown.
- A sensor with state class `total` carries its `last_reset`, the one reset the
  main HA cannot work out itself; without it a new meter cycle reads as a large
  negative delta and the energy dashboard loses the cycle.
- A `text` that is `unknown` or `unavailable` here shows unavailable there (its
  MQTT platform has no payload for *no value*). A text whose value is the word
  `None`, or empty, shows as it is.
- A button, scene or notify entity has no state but follows the availability
  of the entity behind it.

### Entity and device lifecycle

An entity disabled in the container stays on the main HA with its
customisations, unavailable, also across a switch to another version (which
clears only the documents of entities the new version no longer has). It is
still announced, with `enabled_by_default: false`, which the main HA applies
only when creating an entity. Deleting it, renaming its entity id (the new id
replaces it) or excluding it removes it there, also in the first five minutes
after a start, while entities announced before the start are kept for the
orphan sweep ([After a restore](#after-a-restore-import-or-rebuild)).

When it was the last entity of a device also gone from the container, the
device's config is cleared too, so no empty device waits for the next full
republish. In the first five minutes, entities an earlier process announced
that are still setting up here count for their device: it keeps its config
(with a removal form for the one that went) until the orphan sweep decides.

Renaming a device, or changing its model or parent, reaches the main HA within
seconds. A component is republished when its *shape* changes, not only at the
next full republish (up to an hour). The source's `supported_features` decide
the shape, and its registry entry fills in what the state cannot carry:

- covers, valves, vacuums, lawn mowers, water heaters, locks and update
  entities show only supported features (a valve that cannot stop gets no stop
  button, a lock that cannot open no open button, an update that cannot install
  no install button);
- an alarm panel offers only the modes it can arm;
- a thermostat gets a temperature range, a target humidity and an on/off switch
  only where the source declares them;
- a fan offers the same speed steps.

Home Assistant composes an entity's name from the device's and its own, so the
component carries only what the entity adds: the entity named after its device
carries no name, the others theirs without the device name. Entity ids stay
pinned by `default_entity_id`. An entity mirrored by an older release changes
its friendly name when it is republished.

### What the main HA cannot show

These stay in the container: a water heater's away mode and high/low target
(the rest is mirrored); a light's `transition` and `flash`; installing an
update with a backup; an update's release-notes link that is not an `http://`
or `https://` URL (the main HA would refuse the whole payload, versions
included); a notify message's title (the message arrives); an alarm panel's
`changed_by`; the `device_class`, `supported_features` and `entity_picture`
attributes of an entity mirrored as a sensor (a media player's `tv`).

A text value shows there without leading and trailing spaces (a value sent from
there keeps them). A vacuum command sent from the main HA carries its
parameters only as a mapping.

## Manager device

With discovery on, or `manager_discovery` alone (for example in
[shadow mode](shadow-mode.md)), the main Home Assistant gets a
`hass-remote-integration (<identity>)` device (`hass_<domain>`, see
[Identity](#identity)): whether the integration is up
and its health; update entities for the integration, for Home Assistant in the
container and for hass-remote-integration; sensors for memory, CPU, event-loop
lag (the worst delay of a one-second timer in the last minute), volume usage
and the patch status.

Health is published every minute once Home Assistant in the container has
started. The two health entities go unavailable after three minutes without
one, and during a restart until Home Assistant is up again, so a stuck
container never shows an old `ok`.

Turning `manager_discovery` off (with discovery off) removes the device at the
next connection or health tick, also after a restart: the container records
that it announced it (`integration_manager/mqtt_manager_device.json`). Without
that record it looks for the retained config on the broker once and removes it
only if it is there.

### Manager actions

With `manager_commands` (off by default):

| Action | Rate limit |
|---|---|
| *Install* on the integration update entity | once per 10 minutes |
| *Install* on the Home Assistant update entity | once per 10 minutes |
| *Restart* | — |
| *Back up now* | once per 10 minutes |
| *Check for updates* | once per 5 minutes |

Installing the integration runs the preflight, then installs and starts the
release the way the UI does (backup, smoke test, automatic rollback), and
restarts when the loaded code must be replaced. Installing Home Assistant
(upgrades only) takes a backup, keeps the configuration and restarts; it has no
force and takes every refusal `POST /api/ha/update` makes ([API](api.md)).

The limits survive a restart (if they cannot be saved, the action still runs
and the limit holds until the restart). Only a run spends one: an action
refused by what it called (an install already running) does not. An install
whose requirements failed still counts, since it took its backup. Without the
limit, a release whose smoke test fails (rolled back, still the newest) would
be reinstalled at every press, with a backup and a restart each time.

A restart over MQTT, alone or after an install, waits up to five minutes in all
for a running manager action and for an install, start or backup started from
the UI. If one still runs then, or the restart is refused otherwise, it is
skipped and the result says so: `restart skipped: <action> is still running
(<n> s)` or `restart skipped: another action is still running` (UI). The result
goes out once the restart is under way, so `ok` means the process is going
down.

A refused command (unknown action, wrong payload, `manager_commands` off) gets
`ok: false` and the reason on `manager/result`, and nothing else is published.
While an action runs, any other than a restart is answered `<action> is still
running (<n> s)`; one that never returns stops blocking the others after 30
minutes.

**Anyone who can publish under the base topic can use these actions**, so turn
them on only on a broker with credentials. hass-remote-integration itself is
updated by pulling a new image.

## Stop, uninstall, restore

The identity (`hass_<domain>`, see [Identity](#identity)) belongs to the running
integration.

- **Stop** is not a removal: the whole device, the manager device included,
  goes unavailable on the main HA and keeps its entities and customisations
  until the integration starts again. It also clears the retained service
  catalog; the next start publishes it again.
- **Boot:** the publisher connects while the boot is still reconciling, with
  the identity of whatever ran when the container came up. When the boot
  starts an integration itself (the Environment builder's deferred start, or
  one adopted from its config entries after a restore), the identity is handed
  over when that start is done, before the first entity is published. Nothing
  goes out under the previous `hass_<domain>`.
- **No integration running:** no identity. `GET /api/mqtt/status` shows
  `base_topic` and the other topics as `null`, and so do `base_topic` in its
  `health` and in the `mqtt` part of a `POST /api/run/{start,stop}` answer.
- **Uninstall** clears everything retained under the identity, so the main HA
  removes the entities and devices.

### When the cleanup cannot run

If the broker cannot be reached at the uninstall, or refuses the cleanup, the
integration is still removed, the answer says `retained_cleanup_failed` with
`retained_cleanup_error`, and the timeline records it. The cleanup of that
identity (its documents and discovery configs, nothing else) is kept on disk
and retried every minute while MQTT is enabled, also with no integration
installed. A retained scan that loses its connection, reaches its byte budget,
or ends at its time limit while messages still arrive also keeps the cleanup
pending. No partial scan is acknowledged as a complete cleanup; a later full
scan must finish before its retry record is removed.
If MQTT is disabled at the uninstall, nothing is sent: the identity
last recorded gets the same kept cleanup, the answer says
`retained_cleanup_deferred`, and it runs once MQTT is enabled again.

A kept cleanup belongs to its broker, by host and port only (another user, or
TLS turned on, is the same broker). `retained_cleanup_broker` in the answer and
`broker` in `retained_cleanup_pending` of the MQTT status show it as
`host:port`. It is tried only while the MQTT settings name that broker, never
sent to another, and completes once that broker is configured again
(`retained_cleanup_other_broker` in the answer meanwhile). Its `error` in the
MQTT status says what it waits for: MQTT disabled, the settings to name its
broker again, the next try, or the last error. If that broker is gone for good,
stop the container and delete `mqtt_cleanup_pending.json` (or remove that
broker's entry). A cleanup deferred from an `mqtt_identity.json` written by
0.16.x or older belongs to the broker the settings name at the uninstall.

Pointing the MQTT settings at another broker is the same situation without an
uninstall: the old identity is recorded as pending for the old broker (the log
and the timeline say so) and cleared when the settings name it again. Starting
the same integration again on that broker cancels the pending cleanup: its
documents are live again. `mqtt_identity.json` and `mqtt_cleanup_pending.json`
are not part of backups ([Backups](backups.md)).

### After a restore, import or rebuild

Entities that a restore, an import or a rebuild took away before a restart are
removed from the main HA five minutes after Home Assistant in the container has
started (only entities that exist neither as a state nor in its entity
registry by then). An entity that moved to another device while the
container was down (a clean start gives every device a new id) is dropped
from its old device's config. When every entity in the config of a device no
longer announced is now announced under another device, that config is
cleared at once, before the new configs go out, so the main HA takes them
without waiting. Otherwise it waits for the sweep: a config of a device no
longer announced is cleared then, or, when entities still setting up are left
in it, keeps them with a removal form for every other one. Either way the
devices the entities moved to are announced again a few seconds later.
The sweep also clears the configs with this identity's exact origin in the
[id format](#id-format) it does not use now (left by a rollback to 0.26.0
with discovery on, a restore or a hand edit), except the manager device's,
which is announced again over it.

## What the main Home Assistant needs

Discovery uses the device-based MQTT format. `main_ha_version` handles only
*platforms and device classes*; it cannot restore `default_entity_id` below
2025.10 or make a main instance below 2024.11 subscribe. Measured in
September 2026 on `aarch64` against real instances of each release, with an
integration of 14 entities:

| Main Home Assistant | What happens |
|---|---|
| **2025.10 and newer** | Everything works, with the two exceptions below. Entities get the ids they have in the container, nothing is logged. |
| 2024.11 – 2025.9 | Every entity is created and works (subject to the two exceptions below), but `default_entity_id` is silently dropped, so ids are generated from the device name (`sensor.hri_probe_no_device_probe_demo` instead of `sensor.probe_demo`). Nothing is logged. Anything on the main instance that names the original id points at nothing. |
| Below 2024.11 | Nothing arrives: those releases do not subscribe to `<prefix>/device/+/config`, and no error appears anywhere. |

- **`date`, `time` and `datetime` entities need 2026.5 or newer**, and a
  device class needs the release that introduced it (`radon`: 2026.8). An
  older main instance rejects the *whole device's* payload, logging `value
  must be one of [...] @ data['components'][...]['platform']`. Set
  [`main_ha_version`](#main_ha_version) to the version it runs, or exclude
  those entities from MQTT (Entities page or a rule). The container cannot see
  the main instance's release, so every `date`, `time` and `datetime` entity
  with a state carries a red `HA 2026.5+` tag next to its `disc` tag on the
  **Entities** page (a disabled one, published with `enabled_by_default:
  false`, is listed without it). Excluding the entity removes the tag.
- **Colour temperature on mirrored lights needs 2025.2 or newer**
  (`color_temp_kelvin`).

The Home Assistant *inside the container* is unrelated; its floor is in
[Home Assistant and Python versions](home-assistant-versions.md).
