# Shadow mode beside your main Home Assistant

Run this container next to a main Home Assistant that still runs the same
integration, for as long as you like, then switch over. Both see the same
devices, only one acts on them, and the main HA sees none of the container's
entities until you enable discovery.

## Sharing the data source

Both instances must reach the integration's data source:

- **Network devices and cloud services** usually accept several clients: point
  both instances at them.
- **A serial or USB device** opens for one process at a time. Put a
  serial-to-TCP bridge in front that fans the byte stream out to every client,
  and point both instances at it (typically a `socket://<host>:<port>` URL):

```
          USB / serial
device  ------------------>  bridge on the host  --tcp-->  main Home Assistant
                             (fan-out)           --tcp-->  this container
```

  The bridge must not drop the older client when a new one connects. It also
  helps where passing USB into containers is awkward (macOS, some NAS): the
  container only needs TCP.

## Only one side acts

Reading is safe to share; **acting is not**: two instances sending commands,
polling in parallel or answering for the devices will confuse them. Until the
cutover, keep the container passive (read-only mode, sending disabled, polling
off). If the integration has no such option, run the shadow only for
comparison periods you watch, or against a test device.

While passive:

- values the devices report on their own match the main HA right away;
- values that only arrive when asked for stay `unknown` in the container;
- timestamps in log files the integration writes may be in UTC.

## Working around integration quirks

A shared data source can expose behaviour the integration never met on a local
port (a transport that never closes on reload, a device detection that cannot
probe a network port). Fix it without forking with a **patch** on the
Integration page, retired by an `# applies-to:` header once upstream ships the
fix; see [Patches](../README.md#patches).

After a Stop/Start in the container, check that the data source shows no
extra connection: a growing count means the integration leaks them on unload.

## Keeping the main Home Assistant untouched

- Keep **discovery off** while both run, or the main HA gets a second copy of
  every entity; *Undo* on the Cutover page removes them.
- A fresh install may name entities differently from your main HA. *Import
  settings from a Home Assistant backup* on the System page aligns ids, names
  and disabled flags by unique id, so the main HA keeps the entity ids your
  automations use after the cutover.
- The Cutover page compares the MQTT entities this container announces with
  the MQTT entities on the main HA, by discovery unique id. The main HA's own
  entities of the integration are not part of it: compare those (entity ids,
  states) by hand before the cutover.
- The page only reads from the main HA (URL and long-lived token, both
  optional), but the token carries every right of its user: create it under a
  dedicated user without admin rights.
- *Enable discovery* reads the main HA's config entries and entity registry to
  make sure the integration is gone there. A main HA that answers the
  config-entry query with an error (an older version) is only checked for
  whether the integration is still loaded. A check that cannot run (the main
  HA unreachable, its registry unreadable) refuses the enable. With no main HA
  configured the enable goes ahead unchecked. `force: true` on
  `POST /api/cutover/enable` skips the checks on the main HA; the answer
  (`checked`, `forced`) and the timeline say which happened
  ([api.md](api.md), Cutover).

## Cutover checklist

1. **Cutover page**: the main HA is configured, the comparison shows the
   expected entities as missing on the main HA and no orphans, health is `ok`.
2. **Main HA**: remove the integration (delete its config entries). Disabling
   is not enough: a disabled entry keeps its entity ids registered, so the
   MQTT entities would get `_2` ids (the Cutover page refuses then). Removed
   entities stay in Home Assistant's deleted-entity list for 30 days, so
   adding the integration there again after an *Undo* brings their ids and
   settings back.
3. **Container**: turn off the integration's passive options (for example
   re-enable sending) and reload its entry.
4. **Cutover page**: *Enable discovery*, then watch until every entity exists
   on the main HA.
5. Automations on the main HA keep working: the entity ids are the same. The
   unique ids are new (`hass_<domain>_<entity id>`), so areas, labels and
   custom names set on the removed entities have to be set again. Commands
   reach the container over `hass_<domain>/cmd/...`, service calls over
   `hass_<domain>/call/...`.
6. Something wrong? *Undo* on the Cutover page (discovery off, configs
   removed), make the container passive again, and add the integration again
   on the main HA.
