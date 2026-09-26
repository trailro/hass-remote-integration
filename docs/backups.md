# Backups and restore

What a backup holds, how a restore is applied or refused, pruning,
deletion and uploads, and importing a Home Assistant backup.

## Taking backups

Backups are taken before every start that changes something, every Home
Assistant version change, every restore and before replacing the integration;
optionally daily. On **System** you create, download, upload, delete and
restore them. The label is kept as typed; the file name uses it in plain
ASCII (accents dropped, spaces as `-`, other alphabets left out: `înainte de
update` gives `…-inainte-de-update.zip`). Older backups named with such
letters can still be downloaded, restored and deleted.

## What a backup holds

**A backup holds secrets:** `integration_manager/settings.json` (the GitHub and
parent Home Assistant tokens), `integration_manager/mqtt.json` (the broker
password), `secrets.yaml` and the credentials in `.storage` (config entries,
authentication). Keep downloaded backups as private as the volume. Backups,
restored files and uploads are created readable by the container user only
(umask 077).

A backup holds at most 100000 files: taking a larger one fails, and an upload
or restore of one is refused (counted from the archive's directory before it
is read) and listed without its details. It takes regular files only and never
reads outside `/config`. A named pipe, socket, device or symbolic link to a
directory (`custom_components` itself included) is skipped with a line in the
log. A symbolic link to a file is stored as that file only when the target is
a file a backup holds anyway (inside `/config`, in the backed-up trees, not
excluded); a link out of the volume, to the login key or to another backup is
skipped with a line in the log.

Not in a backup, so a restore never rolls them back:

- the timeline, resource history, change reports and last known release
  versions (`events.jsonl`, `resource_history.json`, `change_reports.json`,
  `latest_versions.json`);
- the login key and the logout record (`auth_key`, `auth_revoked`);
- `.storage/core.uuid`, the installation id of Home Assistant in the
  container;
- `.storage/http`, the port Home Assistant was set up with (it would pin a
  foreign `HRI_PORT`; one restored from an older archive is dropped at the
  next boot);
- `.storage/tmp…`, a store file Home Assistant is writing at that moment;
- `.storage/*.pre-import` (and `*.pre-import.done`), originals an import set
  aside;
- `mqtt_identity.json`, `mqtt_cleanup_pending.json` and
  `mqtt_undiscover.json`, since the broker is outside the volume;
- `app-watchdog-enabled`, the record that HRI turned the app's Watchdog on
  ([the Watchdog](app.md#restarts-and-the-watchdog)), so a restore never
  turns it on again;
- what the boot rebuilds or is only in flight: the Home Assistant venvs
  (`venv-*`), logs (`*.log`, `*.log.*`), caches (`__pycache__`, `*.pyc`,
  `deps`, `tts`), the backups themselves (a backup never holds another one;
  the app's Home Assistant backup does hold them, see
  [app backups](app.md#backups)), `integration_manager/*.tmp`,
  staging folders, a scheduled restore and its record, `pre-restore-*`, an
  import's `import.tar` and `import-extracted`, and the cached HACS list
  (`hacs_catalog.json`).

A backup of the Docker volume made with other tools can leave out `venv-*`
the same way: each is about 800 MB, and the boot installs the one it needs
again (with internet access). The app's Supervisor backups already do this,
and keep `backups/` ([app backups](app.md#backups)).

## Restoring

A restore is applied at the next restart and can be partial (only `.storage`,
only the manager state, …). Restoring the YAML part also removes root `*.yaml`
/ `*.yml` files not in the backup, so a `secrets.yaml` created later is gone.
Symbolic links inside the restored trees are replaced with real files and
directories, never written through.

A restore is refused, or does not start, when:

- the backup's file names are not in their plain form (`./`, `//`, `..`);
- it brings back `.storage` without the manager part, or the reverse, from a
  backup made while another integration ran (or none): both integrations would
  run under the wrong identity. Restore `.storage` + manager, or everything;
- a Home Assistant version change, full rollback, install, start, stop or
  import is being prepared or running (try again; this also refuses *Cancel
  restore*). A version change is refused in turn while a restore or full
  rollback is being scheduled or a restore is being cancelled;
- `.storage`, `custom_components` or `integration_manager`, of the parts being
  restored, is itself a symbolic link (also when putting the previous
  configuration back);
- its pre-restore backup cannot be recorded in the schedule.

**Home Assistant version.** Each backup records the version it was made on
(the *HA* column). It matters only when `.storage` is restored:

- made on an older version: keep the running Home Assistant (the default; the
  configuration is migrated forward) or go back to the backup's version;
- made on a newer version: only restored together with a switch to it;
- no version, or not a version number (`unknown`): `.storage` is restored only
  after a confirmation ("restore anyway", `"force": true` in the API body). A
  scheduled one without that confirmation in its schedule (an edited
  `restore-pending.json`, or one a manager older than 0.14.0 wrote and never
  booted since) is dropped at the boot with a message.

A switch installs the version at the restart if its venv is gone (only the
current and previous are kept), and brings the current configuration back
from a backup if that version does not start. See
[Downgrading](home-assistant-versions.md#downgrading).

**Cancelling.** *Cancel restore* cancels a restore scheduled by hand. One that
belongs to a scheduled Home Assistant version change is only cancelled with
that change (choose the running version under Home Assistant). A full
rollback's restore cannot be cancelled: restart to finish the rollback, or
start the version it left to undo it.

**Failures.** A restore that fails (a full disk, a file that cannot be
written) puts the previous configuration back, is not retried, and records
`failed`; if even that cannot be written, the next boot records it. A restore
cut off halfway (`docker stop`, power loss, Ctrl-C) stays scheduled: the next
boot applies it again from the same pre-restore backup, or puts that backup
back. If putting it back fails too, Home Assistant is not started: the manager
port shows a status page naming the pre-restore backup (with a password set,
the container log names it), the restore is retried
every 5 minutes, and that backup cannot be pruned or deleted. Deleting
`integration_manager/restore-pending.json` ends the wait and starts Home
Assistant on the configuration as it is. A scheduled restore whose
`integration_manager/restore-pending-*.zip` was deleted by hand is dropped at
the next boot as failed, and no longer protects its backup or holds up a
version change.

## Pruning, deletion and uploads

Pruning keeps the newest backups by the date they were made (never later than
the file's own date) and never removes the backup it runs after. Uploaded
backups and pre-restore copies (`<time>-pre-restore.zip`) are left alone for 7
days, and a pre-restore copy cannot be deleted in that week: it is the only
way back once the restore succeeded. A pre-restore copy whose name carries no
valid date is never pruned and cannot be deleted from the UI: remove it by
hand if you do not need it. Protected backups still count: with
*keep 5*, a backup made by hand is one of the 5, not a sixth. The backup a
restore came from is pruned and deleted like any other once that restore is
over (applied, put back or dropped).

An upload never replaces a backup: a taken name gets a `-2`, `-3`, … suffix
(a long name is shortened to fit). An upload that cannot be written (a full
volume), including a Home Assistant backup uploaded for an import, answers
with the reason and leaves no partial file.

## Import from a Home Assistant backup

*Import* on **System** is described in [Configure
it](../README.md#2-configure-it); the size limits of the upload are in
[Security](security.md#backups).

A store file goes with the longest domain it is named after: `foo_bar_tokens`
comes with `foo_bar`, never with `foo`.

When an import replaces a store file the volume already had, the original is
kept as `.storage/<store>.pre-import` until the import is done; a restart in
the middle puts it back. The import is done once its config entry is in
`.storage/core.config_entries`: the manager writes that file at once, then
removes the set-aside original, then deletes the extracted backup.

`POST /api/import/apply` (*Import*) and `/apply_all` (*Import all*) answer
`alignment`: `entities` and `devices` aligned when the entry had just set up,
and `pending_entities` and `pending_devices`, the map entries still waiting
then. Entities created later are aligned as they appear and not counted, so a
small `entities` with a large `pending_entities` is normal for an integration
that adds entities after setup.
