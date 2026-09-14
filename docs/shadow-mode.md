# Shadow mode beside your main Home Assistant

Shadow mode means running this container next to a main Home Assistant that
still runs the same integration, for as long as you like, before switching
over. Both instances see the same devices, only one of them acts on them, and
the main Home Assistant does not see the container's entities until you enable
discovery.

## Sharing the data source

Two instances can only run side by side if both can reach the integration's
data source:

- **Network devices and cloud services** usually accept several clients: point
  both instances at them.
- **A serial or USB device** can be opened by one process at a time. Put a
  small serial-to-TCP bridge in front of it that fans the byte stream out to
  every connected client, and configure both instances to use the bridge as
  their serial port (typically a `socket://<host>:<port>` URL):

```
          USB / serial
device  ------------------>  bridge on the host  --tcp-->  main Home Assistant
                             (fan-out)           --tcp-->  this container
```

  The bridge must not drop the older client when a new one connects. On hosts
  where passing USB devices into containers is awkward (macOS, some NAS
  systems) the bridge also solves that: the container only needs TCP.

## Only one side acts

Reading is safe to share; **acting is not**. Two instances sending commands to
the same devices, polling them in parallel or answering on their behalf will
confuse them. Until the cutover, keep the container passive: many
integrations have an option for it (read-only mode, sending disabled, polling
off). Commands and services that would reach the devices then do nothing in
the container, which is what you want while the main HA is in charge.

If the integration has no such option, run the shadow only for comparison
periods you watch, or against a test device.

## Working around integration quirks

A shared data source sometimes exposes behaviour the integration never met on
a local port: a transport that never closes its connection on reload, a device
detection that cannot probe a network port. Fix those without forking the
integration with a **patch** on the Integration page: a small `*.py` module or
unified diff applied every time the integration starts, and retired
automatically by an `# applies-to:` header once upstream ships the fix. See
*Patches* in the README.

After a Stop/Start in the container, check that the data source does not show
one connection more than before: a growing count means the integration leaks
connections on unload.

## What to expect while passive

- Values the devices report on their own match the main HA right away.
- Values that only arrive when someone asks for them stay `unknown` in the
  container until it is allowed to act.
- Timestamps in log files the integration writes may be in UTC.

## Keeping the main Home Assistant untouched

- Keep **discovery off** while both instances run. Otherwise the main HA
  receives a second copy of every entity. If it happens anyway, *Undo* on the
  Cutover page removes them.
- Entity ids: a fresh install may name entities differently from your main HA
  (newer integration versions can change their defaults). *Import settings
  from a Home Assistant backup* on the System page aligns ids, names and
  disabled flags by unique id, so after the cutover the main HA keeps the
  entity ids your automations use.
- The Cutover page compares both sides entity by entity, by discovery unique
  id, and needs only read access to the main HA (URL and a long-lived token).

## Cutover checklist

1. **Cutover page**: the main HA is configured, the comparison shows the
   expected entities as missing on the main HA and no orphans, health is `ok`.
2. **Main HA**: disable or remove the integration.
3. **Container**: turn off the passive options of the integration (for
   example re-enable sending) and reload its entry.
4. **Cutover page**: *Enable discovery*, then watch until every entity exists
   on the main HA.
5. Automations on the main HA keep working: same entity ids and unique ids.
   Commands reach the container over `hass_<domain>/cmd/...`, service calls
   over `hass_<domain>/call/...`.
6. Something wrong? *Undo* on the Cutover page (discovery off, configs
   removed), make the container passive again, and re-enable the integration
   on the main HA.
