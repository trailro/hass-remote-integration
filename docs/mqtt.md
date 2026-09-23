# MQTT reference

## MQTT reference

```
hass_<domain>/status                                online | offline (retained, last will)
hass_<domain>/health                                retained JSON, every 60 s
hass_<domain>/<integration>/<domain>/<object_id>    one retained document per entity
hass_<domain>/<integration>/event_stream/<object_id>  events of event entities, not retained
hass_<domain>/services/<domain>                     retained service catalog
hass_<domain>/cmd/<domain>/<object_id>/<field>      commands (used by discovery)
hass_<domain>/call/<domain>/<service>               service call, JSON payload
hass_<domain>/result/<domain>/<service>             call result, not retained
hass_<domain>/manager                               retained JSON, every 60 s: updates, resources
hass_<domain>/manager/cmd/<action>                  manager actions (with manager_commands)
hass_<domain>/manager/result                        outcome of a manager action, not retained
<prefix>/device/hass_<domain>_<device>/config       HA device-based discovery
```

- **Entity document**: state, attributes, `last_changed`, `last_updated`,
  `last_reported`, and the registry metadata (unique id, name, device class,
  unit, icon, category, device). Access tokens are left out of the
  attributes, at any depth: `access_token`, and any value whose URL carries a
  `token=` (the `entity_picture` of a camera, image or media player, also
  inside a list or a nested attribute), which would open this container's
  proxy. A state that is such a URL is published with the token replaced by
  `***`. Every other attribute is published as the integration
  sets it, under any name: an attribute is state the integration chose to show,
  and the main Home Assistant needs it as it is (`code_format`, `error_code`, a
  GPIO `pin`). The secret-name masking of the command history and the logs does
  not apply to entity documents; an integration that puts a password in a state
  attribute publishes it to anyone who may read the base topic. Every entity of
  the container's Home Assistant gets a document, except the entities of the
  integrations listed in
  `exclude_integrations` (default `["integration_manager"]`; set with `POST
  /api/mqtt/config`, as a list or comma-separated text; services whose domain is
  one of them are also left out of the catalog and not callable over MQTT; Home
  Assistant does not record which integration registered a service, so what
  such an integration registers under another domain — a legacy
  `notify.<name>`, a `tts.<engine>_say`, a service of a platform — stays
  callable, while an entity service reaches only published entities and so
  never its entities), entities excluded by a
  rule, and `zone` entities: `zone.home` is the container's own home location
  (which is also why a `device_tracker` with coordinates is mirrored as
  coordinates rather than as an answer — the main Home Assistant's MQTT tracker
  takes whatever arrives on the state topic as a *location name*, and an entity
  that has one never looks at a zone, so this container's verdict used to be
  the last word there while its own `zone.home` sits at 0,0 and made every
  phone `not_home`. The state topic now carries the reset payload whenever the
  document has a latitude and a longitude, so the main instance places the
  device in **its** zones; a tracker without coordinates — a router, a
  Bluetooth one — still sends its own `home`/`not_home`, which is all it has),
  which every Home Assistant creates by itself (one published by 0.17.0 or
  older is removed from the main HA five minutes after the start, like any
  excluded entity). `entities_total` in `GET
  /api/mqtt/status` counts the entities with a state that get a document. A vacuum's document also has `fan_speed` and
  `state` at the top level (copies of the attribute and of the state), because
  the main HA's MQTT vacuum reads them only there. That `state` is `null` when
  the vacuum's state is not one of the six activities that platform knows
  (`unknown` and `unavailable`): it drops anything else and would otherwise
  keep showing the activity it had.
- **Protocol and document size**: the container connects with MQTT 5, so a
  broker can announce the largest packet it accepts. A document over that
  maximum, or over 1 MiB when none is announced, is skipped rather than sent: a
  broker that refuses an oversized packet closes the connection, and the client
  would replay the same document on every reconnect until nothing else gets
  through. A broker that refuses MQTT 5 gets an MQTT 3.1.1 connection, logged
  and on the timeline; 3.1.1 cannot announce a maximum, so only the 1 MiB limit
  applies there. `GET /api/mqtt/status` shows the protocol in `protocol`, and a
  skipped document is named in the log and on the timeline and counted there
  (`oversized_skipped`, `last_oversized`); the MQTT page shows the protocol and
  the last skipped document next to the connection state.
- **Subscriptions**: at every connection the container subscribes to
  `cmd/#`, `call/#` and `manager/cmd/+`. A broker that refuses them (an ACL
  that allows publishing but not subscribing) still gets every document, but
  no command, service call or manager action reaches the container: the
  refused topics and the broker's reason are logged once, put on the timeline,
  and shown in `subscribe_error` and `connect_error` of `GET /api/mqtt/status`
  and next to *connected* on the MQTT page while the connection stays up. Once
  means once per connection the container opens (saving the MQTT settings, an
  identity change, a restart), not once per reconnect of the client library;
  ending a connection itself clears both errors. Only a refusal the broker states can be seen:
  mosquitto's `acl_file` grants every subscription and silently drops what the
  client may not read (its dynamic security plugin refuses it). `status` turns
  `online` only once the broker
  has answered the subscription, so a command the main Home Assistant sends the
  moment the device becomes available is received; a refused subscription still
  turns it `online` (the documents keep flowing), and a broker that has not
  answered within 10 seconds gets `online` anyway, reported like a refusal
  until the answer comes. An `online` never follows the retained `offline` the
  container sends when it ends a connection itself. The MQTT client library's own messages are
  logged under `custom_components.integration_manager.mqtt_publisher.paho`
  (INFO and above; DEBUG gives a packet trace, which names topics and sizes but
  never the password or a payload).
- **Refused login**: a broker that refuses the connection is logged once and
  put on the timeline once — `the broker refused the login: Not authorized`
  or `… the login: Bad user name or password`, whichever the broker sends, for
  a wrong user name or password, and `the broker refused the connection:
  <reason>` for any other refusal;
  `connect_error` of `GET /api/mqtt/status` and the MQTT
  page keep that reason for as long as the client library retries, instead of
  the `Unspecified error` disconnection the library reports after each refusal.
- **Discovery** (off by default): one retained config per device. Entities of
  every domain that has an MQTT platform become native entities with working
  commands; the rest (cameras, media players, weather, …) are mirrored as
  read-only sensors with all attributes. A mirror is a sensor, so a `config`
  entity category from the source entity — which `date`, `time` and `datetime`
  entities usually carry — is published as `diagnostic`: the main Home
  Assistant refuses a sensor that has it outright. Per-entity rules on the Entities page
  or as JSON (`GET/POST /api/mqtt/rules`, `{"rules": {"<entity id or glob>":
  {...}}}`) change only what is published: `exclude` (true/false), `name`,
  `enabled_by_default` (true/false), `entity_category` (`config` or
  `diagnostic`), `device_class` and `icon` (`mdi:<name>`). A `device_class` is
  refused, with the reason, when it is not one the main Home Assistant takes
  for an entity the rule matches, or when that entity's unit does not fit it
  (`W` with `temperature`): the main HA would refuse the whole device config.
  `entity_category` is refused the same way when the main Home Assistant would
  not take it for the platform the entity is published as — it refuses a
  `sensor` or a `binary_sensor` whose category is `config` ("cannot be added as
  the entity category is set to config"), and the entity would never appear
  there while showing as permanently missing in parity. A glob that reaches
  such an entity later is published without the category, and says so once in
  the log.
  An entity a glob rule matches later that the class does not fit is announced
  without it, with a warning in the log, and the rest of the rule applies.
  An entity excluded while the container was down is removed from the main HA at the next connection. When
  two entities of one device would get the same component key
  (`image_processing.x` and `image.processing_x`), the second is skipped with a
  warning in the log; `discovery_collisions` in `GET /api/mqtt/status` counts
  them. When two entities ask for the same entity id on the main HA (a
  mirrored `camera.front` becomes `sensor.camera_front`, next to a real
  `sensor.camera_front`), both are announced, the main HA gives one a `_2`
  suffix, and the log names them; `discovery_default_id_duplicates` counts
  them. `main_ha_version` says which Home Assistant consumes this discovery,
  for example `2026.4`. Empty (the default) means "assume it is current":
  nothing is left out and the payload is exactly what it would be without the
  setting. Set, discovery leaves out what that version cannot parse — a device
  class it does not know is dropped from the component (the entity still
  arrives, without that class) and a domain whose MQTT platform it does not
  have is mirrored as a read-only sensor, the way an unmappable domain always
  was. This matters because the main Home Assistant ignores a key it does not
  know but rejects the **whole device's** payload over an unknown platform or
  an unknown device class: one `radon` sensor takes every other entity of its
  device with it. `discovery_compat_device_classes_dropped` and
  `discovery_compat_platforms_mirrored` in `GET /api/mqtt/status` count what
  the setting left out in the last pass, the MQTT page shows both, and each
  distinct drop is logged once at INFO, and again after the setting changes. What each version knows comes from
  `ha_compat.json`, generated from the published Home Assistant wheels by
  `tools/gen_ha_compat.py` (2025.1 to 2026.9, generated 19 September 2026); a
  version at or above the newest release in that table filters nothing, and
  neither does a value that is not a version. One *below* the table's oldest
  row is filtered as if it were 2025.1 — there is no older data, and such a
  main Home Assistant is below the 2024.11 that device discovery needs anyway.
  `discovery_compat_platforms_mirrored` counts a subset of `discovery_mirrored`:
  a mirrored platform is a mirror like any other. A light, fan, siren or humidifier whose state is `unknown` stays
  unknown on the main HA. A sensor whose state class is `total` carries its
  `last_reset` as well: that is the one state class whose reset the main Home
  Assistant cannot work out for itself, and without it a meter that starts a new
  cycle is read there as a large negative delta and the energy dashboard loses
  the cycle. A `text` entity whose state is `unknown` or
  `unavailable` here shows unavailable on the main HA rather than a value: its
  MQTT platform takes every payload as the text, so there is no payload that
  means *no value* there. A text whose value really is the word `None`, or
  empty, is shown as it is. An entity disabled in the container stays on the
  main HA with its customisations and shows unavailable there; it is still
  announced, with `enabled_by_default: false`, which the main HA applies only
  when it creates an entity. Deleting the entity removes it there, and so do
  renaming its entity id (the new id replaces it) and excluding it, also in the
  first five minutes after a start, while entities announced before the start
  are still kept for the orphan sweep (see *Stop, uninstall, restore*); when
  it was the last entity of a device that is gone from the container too, the device's
  discovery config is cleared as well (unless entities carried from the previous
  process are still setting up, see below), so no empty device is left on the main HA
  until the next full republish. In those first five minutes the entities an
  earlier process announced and that have not finished setting up here count as
  entities of their device: the device keeps its config (with a removal form for
  the one that went) until the orphan sweep decides, so they are not taken off
  the main HA and brought back a moment later. Renaming a device, or changing
  its model or its parent, reaches the main HA within a few seconds instead of
  waiting for that republish. A button, scene or notify entity has no state to mirror but still
  follows the availability of the entity behind it, so it shows unavailable on
  the main HA while that entity is.
  An integration named `call`, `cmd`, `result`, `services`, `manager`,
  `health` or `status` publishes its documents under
  `<name>-integration/<domain>/<object_id>`, so they never land on the
  command, call or result topics.
- **Commands**: numeric command topics accept only finite numbers. Text
  values, notify messages and select options are used exactly as sent, spaces
  included. The value sent to a `text` entity in password mode, on any of its
  command topics, with `text.set_value` over MQTT or from the Services page,
  shows as `***` in the command history, the status and the log, also inside a
  service error that quotes it (the result sent back to the caller keeps it).
  Only what is sent is masked: the entity's state is its value, and it is
  published in the entity's retained document and mirrored on the main Home
  Assistant as it is, readable by anyone who may read the base topic, like any
  other state (see the note on state attributes above). What a service *says back* is masked too:
  an exception is free-form text, so the command history, `GET
  /api/mqtt/commands`, the status document and the log line run it through the
  same rules the Logs page uses — a password, a `?token=` URL or an
  `Authorization: Bearer …` in an integration's error message comes out `***`,
  and so does a credential written after an auth scheme in what the caller
  sent (`"Authorization: Bearer …"`, `token: Basic …` in a call's data),
  while an ordinary failure stays readable word for word. The two bounds of a thermostat range change arrive as two
  commands and become one service call: the first waits up to 1 s for the
  second. An alarm panel with a code asks for it on the main HA and sends it
  with the action, and so does a lock: its commands carry
  `{"action": …, "code": …}`, so a code-protected lock can be operated from
  there at all — the source used to receive the bare action, without the code
  the operator had typed, and refuse it. A JSON payload on a vacuum's `send_command` topic needs a
  `command` string; its other keys are the command's parameters (`{"command":
  "spot_area", "rooms": [1]}`, the shape the main HA sends; a lone `params`
  object, as 0.17.0 took it, is used as the parameters); any other payload is
  sent as the command name. The state topic of a switch, light, fan, siren or
  humidifier takes only `ON`/`OFF`, `TRUE`/`FALSE` or `1`/`0` (any case,
  surrounding spaces ignored); any other payload is refused rather than read
  as *off*, with the reason under *recent commands* and in the log. A fan's
  oscillation topic takes `oscillate_on` and `oscillate_off` and nothing else:
  any other payload is refused with the accepted list rather than read as "stop
  oscillating". The action
  tokens of a cover, valve, lock, alarm panel, vacuum or lawn mower match in any
  case; an unknown one is refused with the tokens that are accepted. Tilting a
  cover open or closed on the main HA arrives as tilt position 100 or 0, which
  is what its MQTT cover sends; when the cover here cannot set a tilt position,
  those two become `cover.open_cover_tilt` and `cover.close_cover_tilt` instead
  (and so does a tilt position of 100 or 0 sent from there, the only tilt such a
  cover has). A position in between is still sent as a position, which such a
  cover refuses: the main HA offers the slider anyway, because its MQTT cover
  takes every tilt feature from the tilt topic. A command
  larger than 256 KB, or nested deeper than 64 levels, is refused unread, with
  the reason in the same two places. A command for an entity this container
  does not publish is refused with the reason, and that is checked again right
  before the service call rather than only as the command arrives: an entity
  excluded while its command was on its way is not acted on.
  A command, service call or manager action
  published with `retain` is never carried out, because a physical effect must
  not replay at every reconnect; the retained message is cleared from the broker
  as it arrives, and the log names the topic (it does not appear under *recent
  commands*). On a broker that
  speaks only MQTT 3.1.1 this holds for a retained command found when the
  container subscribes (at every connection); one published while the container
  is already connected reaches it without the retain flag, runs once, and its
  retained copy is cleared at the next connection. A 3.1.1 subscription cannot
  ask the broker to keep the container's own publications away from it (there
  is no `noLocal` before MQTT 5), so the empty payload that clears a retained
  command comes straight back; the container remembers the topics it has just
  cleared and drops that one echo, which would otherwise have blanked a text
  entity or sent an empty notification. A second empty payload on the same
  topic, or one arriving more than 30 s later, is a command again and is
  treated as one. On MQTT 5 nothing is remembered at all: the subscription
  keeps the container's own publications away from it, so an empty payload
  there is always somebody else's command.
- **What the main HA cannot show**: its MQTT platforms have no place for some
  of what an entity has here. A water heater's away mode and its high/low
  target (the rest is mirrored), a light's `transition` and `flash` (the basic
  MQTT light schema has no place for them),
  installing an update with a backup, an update entity's release-notes link when
  it is not an `http://` or `https://` URL (an integration that serves its notes
  from `/local` or `/api` reports a relative one, which the main HA's MQTT
  update refuses — and it refuses the whole rendered payload with it, so the
  entity would lose its versions as well), the title of a notify message (the
  message arrives), who changed an alarm panel (`changed_by`), and the
  `device_class`, `supported_features` and `entity_picture` attributes of an
  entity mirrored as a sensor (a media player's `tv`) stay in the container.
  An entity's name on the main Home Assistant is composed by Home Assistant
  itself from the device's name and the entity's own, so the component carries
  only what the entity adds: the one entity named after its device carries no
  name at all, and the others carry theirs without the device's name in front.
  Before this, a "Hall Lamp" on a "Hall Lamp" device read "Hall Lamp Hall Lamp"
  there. Entity ids are unaffected — `default_entity_id` still pins them — but
  the *friendly name* of an entity already mirrored changes when this release
  republishes it.
  A text value shows on the main HA without its leading and trailing spaces
  (Home Assistant strips what a template renders; a value sent from there
  keeps them). A vacuum command sent from the main HA carries its parameters
  only as a mapping (the main HA drops a list). A category set with an MQTT rule
  reaches an entity the main HA already has only after the main HA restarts,
  like `enabled_by_default`. A component is republished when its *shape*
  changes, not only its value: an entity that was `unavailable` when its
  config went out — with no attributes to read — used to keep a cover without
  its position, a fan without its speed or an alarm without its code box until
  the next full republish, up to an hour later. What the source declares in
  `supported_features` decides the shape now, its registry entry fills in what
  the state cannot carry, and a component that comes out different is
  announced again. Covers, valves, vacuums, lawn mowers, water heaters, locks
  and update entities show the features the entity supports here — a valve that
  cannot be stopped gets no stop button, a lock that cannot be opened gets no
  open button, an update entity that cannot install gets no install button — an
  alarm panel offers only the modes it can arm, a thermostat gets a temperature
  range, a target humidity and an on/off switch only where the source declares
  them, and a fan offers the same speed steps.
- **Service calls**: publish a JSON object to `call/<domain>/<service>` (service
  data plus optional `entity_id`, and an optional `_id`); the result comes back
  on `result/...`, with a `response` key for a service that returns response
  data (the catalog marks those `"response": "optional"` or `"required"`, from
  what the integration registered). A result over the broker's maximum packet
  size is answered without its response data, with `ok: false` and the reason,
  so the caller still gets an answer. So is a call that fails inside the
  container: `ok: false` with `internal error (<exception type>)`, and the log
  names where (never the message, which may quote the data). An `_id` is at most
  128 bytes as JSON: a larger one is refused with an answer carrying the id cut
  short, and the call does not run — the id is kept in the duplicate map, in the
  command history and echoed in every result and every `/api/mqtt/commands`
  poll, so it is capped in one place rather than truncated differently in three.
  A repeated `_id` within five minutes is answered from memory
  and never executed twice (the latest 1000 `_id`s are kept); the comparison
  keeps the type, so `1` and `"1"` are two different calls. A call refused
  before it reached the service — an unknown service (an integration still
  loading at boot answers that for a moment), a target that does not exist here,
  or too many calls in progress — does not remember its `_id`, so the same call
  sent again runs instead of being handed the refusal for five minutes. An
  internal error is still remembered: a repeat gets that answer rather than
  being sent down the same broken path again. While the service
  still runs, also after the call was answered with a timeout, a repeat is
  answered `ok: null` with `state: running` and `duplicate: true`; once it
  ends, a repeat gets its final result (flagged `late` when it ended after the
  timeout). At most 50 service
  calls and commands received over MQTT run at once, a timed-out call counting
  until its service returns: beyond that a call is answered `too many calls in
  progress` (a retry with the same `_id` runs once there is room) and a command
  is rejected. The Services page and `POST /api/services/call` have 50 of their
  own, counted apart. `homeassistant`, `shell_command`, `python_script`,
  `hassio` and `integration_manager` are never callable.
  `persistent_notification` and `notify.persistent_notification` are not
  callable over MQTT and are left out of the MQTT service catalog; the Services
  page can still call them. So are `recorder`, `logger`, `system_log`, `backup`
  and `conversation`, which only exist here when the running integration
  depends on them: they take no target, so the published-entity check below
  never applies to them, and they purge history, change log levels, write or
  clear log entries, take a full backup of `/config`, or run a sentence against
  every entity here, published or not. Any other service that takes no target
  (an integration's own, a legacy `notify.<name>`) is callable as registered.
  `NaN`, `Infinity`, numbers too large to be finite
  (`1e999`), payloads larger than 256 KB, JSON nested deeper than 64 levels
  and JSON that does not parse are rejected, with an answer on `result/...`
  that still carries the `_id` when the outer object names one as a string, a
  finite number, `true`, `false` or `null` (read from at most the first 256 KB,
  without parsing the payload). Over MQTT 5 the container tells the broker the
  largest packet it takes (320 KB: the 256 KB payload maximum plus room for the
  topic), so a broker does not send it anything larger: such a call or command
  is dropped by the broker and gets no answer at all, instead of being read
  into memory whole and then refused. A broker that speaks only MQTT 3.1.1
  cannot be told, and there a payload of any size is read before it is refused
  — limit it on the broker (`max_packet_size` in mosquitto). The scans (the
  base-topic check, the cleanup of stale and excluded documents) use
  short-lived connections that announce no maximum: they have to see retained
  documents of any size, a large foreign one included. Values of keys ending in
  `code`, `key`, `pin`, `otp` or `auth` as a word of their own (`code`, `user_code`,
  `api_key`, `user_pin`, `basic_auth`, not `zipcode`, `code_format`, `spin`,
  `author` or `oauth`), or in `usercode`, `passcode`, `pincode`, `password`,
  `passwd`, `passphrase`, `secret`, `token`, `apikey`, `passkey`, `bindkey`,
  `credential`, `credentials` or `psk` (`access_token`, `api_token`,
  `wifi_psk`, not `token_type`) are masked in the command
  history, the status and the log, and so is a rejected command's reason
  where it quotes the payload; `translation_key`, `sort_key` and
  `primary_key` stay readable. The masking reads at most the first 4 KB of a
  payload's text (every key of a JSON payload is still found), so a longer
  payload shows cut there in the history, the status and a rejected command's
  log line, and masking can never hold up the MQTT connection. A call reaches only entities
  the container publishes: an `entity_id` of `all`, or an entity, group (and
  its members), area, floor, label or device that resolves to an excluded or
  unknown entity, is refused, and so is a target that cannot be read (an id
  that is not a string). An area, floor, label or device is measured only against the
  entity domains the service can act on: Home Assistant hands an entity service
  its own component's entities and nothing else, so a room or device that also holds
  entities this container does not publish is no reason to refuse
  `light.turn_on` for it, while a service that is not an entity service keeps
  the strict check. Entity ids in the service data count too: fields
  ending in `entity_id` or `entity_ids`, `group_members`,
  `snapshot_entities`, `entities`, `add_entities` and `remove_entities` (a
  list or a mapping keyed by entity id),
  at any depth; an entity id in a field with another name is not recognised,
  so do not rely on excluding an entity to keep it from a service that takes
  it under a different name. A `device_id` that is not a
  Home Assistant device (a hardware address a service takes as data) stays plain
  service data. A call needs a JSON object, `{}` when it has no data: an empty
  or whitespace-only payload is rejected.
- **Manager device**: with discovery on, or with `manager_discovery` alone (for
  example while running in shadow mode), the main Home Assistant gets a
  `hass-remote-integration (hass_<domain>)` device. It shows whether the
  integration is up and its health, has update entities for the integration,
  for Home Assistant in the container and for hass-remote-integration itself,
  and sensors for memory, CPU, event-loop lag (the worst delay of a
  one-second timer in the last minute, which is how an integration that blocks
  the loop shows up), volume usage and the patch status. Health is published
  every minute once Home Assistant in the container has started; the two health
  entities go unavailable when three minutes pass without one, so a stuck
  container never keeps showing an old `ok`. They also stay unavailable during a
  restart until Home Assistant in the container has started again.
  Turning `manager_discovery` off (with discovery off) removes the device from
  the main Home Assistant at the next connection or health tick, also after a
  restart: the container keeps a record of having announced it
  (`integration_manager/mqtt_manager_device.json`). Without that record (a new
  install, or an upgrade from a version that kept none) it looks for the
  retained config on the broker once and removes it only if it is there, so the
  main Home Assistant is no longer sent a removal for a device it never had
  (which it logged as `No device components to cleanup` at every connection).
- **Manager actions** (`manager_commands`, off by default): *Install* on the
  integration and Home Assistant update entities (each at most every 10
  minutes), plus *Restart*, *Back up now* (at most every 10 minutes) and *Check
  for updates* (every 5 minutes) buttons. Installing the integration runs the
  preflight, then installs and starts the release the way the UI does (backup,
  smoke test, automatic rollback) and restarts when the loaded code has to be
  replaced; installing Home Assistant (upgrades only) takes a backup, keeps the
  configuration and restarts. The limits survive a restart (if they cannot be
  saved, the action still runs
  and the limit holds until the restart), and only a run spends one: an action
  refused by what it called — an install was already running — did nothing, so
  the next press is not made to wait for it. An install whose start failed
  because its requirements did not install still counts as a run: it took its
  backup. A release whose smoke test fails is rolled back and stays the newest
  known one, so without the limit every press would install it again, a backup
  and a restart each time. A restart asked for over MQTT, on its own or
  after an install, waits up to five minutes in all for a running manager action
  (an install, a backup, a check for updates) and for an install, start or
  backup started from the UI to finish; if one is still running then, or if the
  restart is refused for another reason, the restart is skipped and the result
  says so (`restart skipped: <action> is still running (<n> s)` for a manager action, `restart skipped: another action is still running` for one started from the UI). The result goes out once the restart is really under way, so an `ok`
  on `manager/result` means the process is going down and not only that the
  command was accepted. A refused command (an unknown action, a wrong payload,
  or `manager_commands` off) gets `ok: false` and the reason on
  `manager/result`, and nothing else is published; an unknown action's name is
  cut to 40 characters in the answer, its error and the history. While an action runs, any other
  action than a restart is answered `<action> is still running (<n> s)`; one
  that never returns stops holding the others after 30 minutes. Anyone who can publish under the base topic can use
  them, so turn this on only on a broker with credentials.
  hass-remote-integration itself is updated by pulling a new image.
- **Stop, uninstall, restore**: the identity (`hass_<domain>`) belongs to the
  running integration. *Stop* is not a removal: the whole device, the manager
  device included, goes unavailable on the main Home Assistant and keeps its
  entities with their customisations until the integration starts again.
  *Stop* also clears the retained service catalog, so the main Home Assistant
  is not left with services it cannot call; the next start publishes it again.
  The publisher connects while the boot is still reconciling, so it starts with
  the identity of whatever ran when the container came up. When the boot itself
  starts an integration — the Environment builder's deferred start, or one
  adopted from its config entries after a restore — the identity is handed over
  as soon as that start is done, before Home Assistant sets the integration up
  and its first entity is published. Nothing goes out under the previous
  `hass_<domain>`, and MQTT no longer stays disconnected with "no integration
  is running" after a boot that started the first one.
  While no integration runs there is no identity: `GET /api/mqtt/status`
  shows `base_topic` and the other topics as `null`, and so do `base_topic` in
  its `health` and in the `mqtt` part of a `POST /api/run/{start,stop}` answer.
  *Uninstall* clears everything retained under that identity, so the main Home
  Assistant removes the entities and devices. If the broker cannot be reached
  then (or refuses the cleanup), the integration is still removed here, the
  answer says so (`retained_cleanup_failed` with `retained_cleanup_error`) and
  the timeline records it; the cleanup of that identity (its documents and its
  discovery configs, nothing else) is kept on disk and retried every minute
  while MQTT is enabled, also with no integration installed, until the broker
  takes it. If MQTT is disabled at the
  uninstall, nothing is sent: an identity the container published before
  (the one it recorded last) gets the same kept cleanup, the answer says
  `retained_cleanup_deferred`, and it runs once MQTT is enabled again. A kept
  cleanup belongs to the broker it is for (its host and port: another user, or
  TLS turned on, is the same broker holding the same data;
  `retained_cleanup_broker` in the answer and `broker` in
  `retained_cleanup_pending` of the MQTT status show its `host:port`): it is
  tried only while the
  MQTT settings name that broker, is never sent to another one, and completes
  once that broker is configured again (`retained_cleanup_other_broker` in
  the answer while it is not). Its `error` in the MQTT status says what it
  waits for now: MQTT disabled, the MQTT settings to name its broker again, the
  next try, or the error of the last one. If that broker is gone for good,
  stop the container and delete `mqtt_cleanup_pending.json` (or remove its entry for
  that broker). An `mqtt_identity.json` written by 0.16.x or older names no
  broker: a cleanup deferred from it belongs to the broker the MQTT settings
  name at the uninstall.
  Pointing the MQTT settings at another broker is the same situation without an
  uninstall: what the container published is still retained on the broker it
  published to, and no client built here reaches it. That identity is recorded
  as pending for that broker — the log and the timeline say so — instead of
  being swept on the new one and forgotten, and it is cleared when the settings
  name it again. Host and port decide: a different user, or TLS turned on, is
  the same broker holding the same retained data.
  Neither file is part of backups, so a restore never
  forgets a cleanup the broker still needs or brings back an old one. Starting the same integration again on that broker before then cancels
  it: its documents are live again.
  Entities that a restore, an
  import or a rebuild took away before a restart are removed there five
  minutes after Home Assistant in the container has started (only entities
  that exist neither as a state nor in its entity registry by then). The timeline, the resource history and
  the change reports are not part of backups, so a restore does not roll them
  back.
- Before connecting, the container checks that no *foreign* retained data sits
  under its base topic, and refuses to connect if there is (override with
  `force_base_topic`). A discovery prefix that is the base topic, or lies under
  it, is refused when the MQTT settings are saved. Reading the broker's
  retained messages — for this check and for the cleanup sweeps — stops at
  64 MB, so a broker holding a very large retained store cannot grow this
  container's memory; the log says how much was left unread. A sweep that read
  less removes less, never something else.
- **TLS**: tick `tls` on the MQTT page (brokers usually take TLS on port
  8883). The broker's certificate is verified against the system CAs, or
  against `ca_certs`, a CA file inside `/config` (for example
  `/config/mqtt-ca.pem`). `tls_insecure` skips only the check that the
  certificate names the host: anyone holding a certificate from that CA can
  then pose as the broker and read the credentials. A certificate that fails
  the check shows on the MQTT page with its reason (`TLS handshake failed:
  unable to get local issuer certificate`), checked again at most once a
  minute while the connection keeps failing. A connection the broker accepts and
  then drops within ten seconds is reported as the broker closing it (a packet
  over its maximum is the usual cause), not as a TLS problem: that hint only
  fits a connection that was never accepted. The check for foreign
  retained data and every cleanup connect the same way. Client certificates
  are not supported.

### What the main Home Assistant needs

Discovery uses the device-based MQTT format, and some of what it publishes only
newer Home Assistant understands; `main_ha_version` makes the container leave
out the *platforms and device classes* the version you name does not have —
that and nothing else. It cannot restore `default_entity_id` on a main instance
below 2025.10, and it cannot make one below 2024.11 subscribe at all. Measured in September 2026 on `aarch64`,
against real instances of each release, with the container publishing an
integration of 14 entities:

| Main Home Assistant | What happens |
|---|---|
| **2025.10 and newer** | Everything works, with the two exceptions below. Entities get the ids they have in the container, nothing is logged. |
| 2024.11 – 2025.9 | Every entity is created and works (subject to the two exceptions below), but `default_entity_id` is silently dropped, so ids are generated from the device name (`sensor.hri_probe_no_device_probe_demo` instead of `sensor.probe_demo`). Nothing says so in any log. Anything on the main instance that names the original id — an automation, a script, a dashboard card — points at nothing. |
| Below 2024.11 | Nothing arrives at all: those releases do not subscribe to `<prefix>/device/+/config`, so no entity is created and no error appears anywhere. |

Two details on top of that:

- **`date`, `time` and `datetime` entities need 2026.5 or newer**, and a
  device class needs the release that introduced it (`radon`: 2026.8). An older
  main instance rejects the *whole device's* discovery payload over one of
  them — not just that entity — with `value must be one of [...] @
  data['components'][...]['platform']` in its log, so the device's other
  entities disappear with it. Set `main_ha_version` on the MQTT page to the
  version that instance runs and discovery leaves those values out by itself:
  the three domains arrive as read-only sensors, an unknown device class is
  dropped, and everything else is unchanged. Without the setting the old way
  still works — exclude those entities from MQTT (the Entities page, or a rule)
  and the rest of the device comes back. The container cannot see which release
  the main instance runs, so it cannot decide this for you; what it can do is
  point at the entities it applies to, and every `date`, `time` and `datetime`
  entity that has a state (a disabled one, published with
  `enabled_by_default: false`, is listed without the tag) carries a red
  `HA 2026.5+` tag on the **Entities** page,
  next to its `disc` tag. The tag goes away when the entity is excluded, which
  is also the fix.
- **Colour temperature on mirrored lights needs 2025.2 or newer**
  (`color_temp_kelvin`).

The Home Assistant *inside the container* is unrelated to this: it is the one
the manager installs, and its own floor is in *Python versions*.

---
