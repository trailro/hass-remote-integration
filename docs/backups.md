# Backups and restore

## Backups

Taken automatically before every start that changes something, before every
Home Assistant version change, before a restore and before replacing the
integration; optionally daily. On **System**
you can create, download, upload, delete and restore them. The label you give
a backup is kept as typed; its file name uses the label in plain ASCII (accents
dropped, spaces as `-`: `înainte de update` gives `…-inainte-de-update.zip`,
letters of other alphabets are left out). A backup an older version named with
such letters can still be downloaded, restored and deleted. A restore is
applied at the next restart, can be partial (only `.storage`, only the manager
state, …), and is rolled back if it fails halfway. A partial restore that
brings back `.storage` without the manager part, or the manager part without
`.storage`, is refused when the backup was made while another integration ran
(or none): the config entries of one integration under the manager of another
would run both and publish them under the wrong identity. Restore `.storage` +
manager, or everything. *Cancel restore* cancels a
restore scheduled by hand; a restore that belongs to a scheduled Home Assistant
version change is cancelled together with that change (choose the running
version under Home Assistant), and cancelling it on its own is refused; a full
rollback's restore is refused too (restart to finish the rollback, or start the
version it left to undo it). A
restore and *Cancel restore* are refused (try again) while a Home Assistant
version change, a full rollback, an install, a start or stop, or an import is
being prepared or running, and a version change is refused while a restore or a
full rollback is being scheduled or a restore is being cancelled.
Restoring the YAML part also
removes root `*.yaml` / `*.yml` files that are not in the backup, so a file
created after it (a `secrets.yaml`, for example) does not survive the restore.
A restore that fails (a full disk, a file that cannot be written) puts the
previous configuration back and is not retried: the schedule is dropped and
the outcome is `failed` (if even the outcome cannot be written, the next boot
records it and still does not try again). A restore whose pre-restore backup
cannot be recorded in the schedule does not start. A restore cut off halfway
(`docker stop`, a power loss, Ctrl-C) stays scheduled, and the next boot
applies it again from the same pre-restore backup, or puts that backup back
if it fails again. If even putting the configuration back fails, Home
Assistant is not started on the half-restored configuration: the manager port
shows a status page naming the pre-restore backup, the restore is retried
every 5 minutes until it applies or is put back, and that backup is kept from
pruning and cannot be deleted. Deleting `integration_manager/restore-pending.json`
ends the wait and starts Home Assistant on the configuration as it is. A
restore replaces symbolic links inside the trees it restores with real files
and directories instead of writing through them, and does not start when
`.storage`, `custom_components` or `integration_manager`, of the parts being
restored, is itself a symbolic link; putting the previous configuration back
after a failed restore follows the same rule. A scheduled restore whose copy
of the backup is gone from the volume (`integration_manager/restore-pending-*.zip`
deleted by hand) is dropped at the next boot and recorded as failed, so it
neither protects its backup nor holds up a version change. A backup holds at
most 100000 files: taking a larger one fails, and an upload or restore of one
is refused (counted from the archive's directory before it is read, and not
listed with its details). Backups, restored files and
uploads are created readable by the container user only (umask 077).
A backup holds secrets: `integration_manager/settings.json` (the GitHub token
and the parent Home Assistant token), `integration_manager/mqtt.json` (the
broker password), `secrets.yaml` and the credentials Home Assistant keeps in
`.storage` (config entries, authentication). Keep downloaded backups as private
as the volume itself.
A backup takes regular files only: a named pipe, socket or device in the backed-up
trees is skipped with a line in the log, and so is a symbolic link to a directory
(`custom_components` itself included). A symbolic link to a file is stored as that
file only when it points at a file a backup holds anyway (inside `/config`, in the
backed-up trees, not excluded); a link out of the volume, to the login key or to
another backup is skipped with a line in the log. What a backup reads is never
outside `/config`, and a special file never holds it up.
Automatic pruning keeps the newest backups by the date they were made (never
later than the file's own date), never removes the backup it runs after, and
leaves uploaded backups, and every copy taken before a restore (`<time>-pre-restore.zip`), alone for their
first 7 days; while that week lasts such a copy is also refused for deletion, since
it is the only way back once the restore has succeeded and its schedule is gone.
The backups pruning leaves alone still count toward the number kept: with
*keep 5*, a backup made by hand is one of the 5 newest, not a sixth. The backup a
restore came from is pruned and can be deleted like any other once that restore
is over (applied, failed and put back, or dropped at the boot). An upload never replaces
an existing backup: a name already taken gets a `-2`, `-3`, … suffix (a long name is
shortened to make room for it). An upload that cannot be written (a full volume)
answers with the reason and leaves no partial file behind; so does the upload of a
Home Assistant backup for an import.
A restore never rolls back the record of what happened: the timeline, the
resource history, the change reports and the last known release versions are
not part of backups, and neither are the login key and the logout record, the
port Home Assistant was set up with (`.storage/http`, which would pin a foreign
port when the backup comes from a container on another `HRI_PORT`; a restored
one from an older archive is dropped at the next boot), a store file Home
Assistant is writing at that moment (`.storage/tmp…`) or an original an import
set aside (`.storage/*.pre-import`, and `*.pre-import.done` once the import
completed). Nor are the records of what the broker holds (`mqtt_identity.json`,
`mqtt_cleanup_pending.json`): the broker is outside the volume, so an older copy
would forget retained data still there or clear data published since. A backup whose file
names are not in their plain form (`./`, `//`, `..`) is refused.

Every backup records the Home Assistant version it was made on (the *HA* column),
and Home Assistant only migrates a configuration forward. Restoring a backup
made on an older version asks what to do: keep the running Home Assistant (the
default: the configuration is migrated forward when it starts) or go back to the
version the backup was made on, for exactly the state of the backup. A backup
made on a newer version can only be restored together with a switch to that
version. A backup that does not record its version (or records something that
is not a version number, such as `unknown`) is only restored with
`.storage` after a confirmation ("restore anyway", `"force": true` in the API
body), since it may come from a newer version. A scheduled restore of such a
backup whose schedule does not record that confirmation (an edited
`restore-pending.json`, or one a manager older than 0.14.0 wrote and never
booted since) is dropped at the boot with a message instead of applied. The version only matters when `.storage` is restored: a partial restore
without it never changes Home Assistant. A switch installs the version at the restart if its venv is no longer
on the volume (only the current and the previous one are kept), takes a backup
of the current configuration first, and brings it back if that version does
not start.
