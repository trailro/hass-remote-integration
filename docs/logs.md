# Logs and log files

## Logs and log files

**Logs** shows the process log: everything Home Assistant and the integration
log, with filters and a live follow. Each line carries its date and time, and
the list holds the newest 200 lines (following live drops the oldest). Loggers listed in the registry's
`quiet_loggers` start at WARNING; raise one at runtime while you investigate.
A live follow keeps advancing even when a whole batch of new lines matched only
inside masked values, or a level or logger filter matched nothing, and a
follower that fell behind reads on at once while more lines are waiting. No
single line can end the log: a message carrying text that is not valid UTF-8 —
a file name from a volume another system wrote, which an `OSError` brings into
the message — is stored with those characters escaped rather than taking the
writer down with it, and a handler that raises costs that one line, not the
rest of the session. The
search runs once typing pauses.
The root logger is not one of them: a level set there would silence or flood
every logger at once, including the line that records the change, so it is
refused; raise the integration's own logger instead.
When more than 50000 lines wait to be written (a blocked output), newer lines
are dropped and a warning says how many.

**Log files** shows files the integration writes itself, such as traffic dumps
or debug logs. It appears in the menu only when there are any. The files are
found through the integration's config entries (any setting ending in `.log`,
with its rotated copies), and the `*.log` files and their rotated copies
(`*.log.1`, `*.log.2026-09-10`) in the registry's `log_dir` and in the config
root. Symbolic links are never listed, and neither is a file with more than one
hard link: a hard link is a second name for the same file, so nothing about the
path tells a log apart from a `secrets.yaml` linked under a `*.log` name.
Rotation by rename or by copy leaves one link, so nothing the integration writes
is lost by it. File names are masked like everything else on the page, and two
files whose names mask to the same text are still listed separately and each
opens its own file; after a restart the page selects the same file again by
its name, and says so when several files share that name. A tail reads at most
the last 32 MB of a file, and a search also stops after 20000 lines that hold
something the masking looks at (a word such as `token` or `key`); *lines read*
says how far it got. A line longer than 1 MiB (a log that stopped writing
newlines) shows its first 1 MiB and ends in `[... N more bytes of this line
not shown]`; *Download file* has all of it.

**Download file**, next to the tail controls, saves the selected file whole.
It is masked exactly as the table is, line by line as it is sent, so nothing
is held in memory. A key block stays masked to its end even when the download
has long passed its `BEGIN` marker, and even when the log never closed it with
an `END` marker. The file is saved under its masked name, never its real one;
a name the mask cut the extension off gets `.log` back, so it opens as a log.
A file larger than 32 MB is sent from its end, the newest lines, which is the
tail's budget and is there for the same reason: masking costs per line, and
the page hands the whole answer to the browser at once. The name of the saved
file then says `-last-32MiB`, and the answer carries
`X-Log-Truncated: 33554432`. The button fetches with the header the endpoint
requires, which a plain link cannot send.

By default every line is shown whole. The **Formatting** box at the bottom of
the page splits lines into columns. A format is a JSON object:

| Key | Required | Meaning |
|---|---|---|
| `pattern` | yes | Python regular expression matched at the start of each line. Each named group `(?P<name>...)` becomes a column, in order. Lines that do not match are shown whole. |
| `hide` | no | Group names captured but not shown |
| `dim` | no | Group names shown in a muted colour |
| `color_by` | no | Group whose value picks the row colour |
| `colors` | no | Map from a `color_by` value to `ok`, `warn`, `bad`, `accent` or `muted` |

Inside JSON every backslash is written twice. For lines like
`2026-01-01 12:00:00.123 WARNING (MainThread) [custom_components.demo] text`:

```json
{
  "pattern": "^(?P<time>\\S+ \\S+) (?P<level>[A-Z]+) \\((?P<thread>[^)]*)\\) \\[(?P<logger>[^\\]]+)\\] (?P<message>.*)$",
  "hide": ["thread"],
  "dim": ["time", "logger"],
  "color_by": "level",
  "colors": {"WARNING": "warn", "ERROR": "bad", "CRITICAL": "bad", "DEBUG": "muted"}
}
```

The format is checked on save: the pattern must compile and have at least one
named group. Counted repeats are limited, because compiling writes each one out
and has no time limit: with every `{n}` and `{m,n}` written out `n` times, the
pattern may hold at most 10000 elements (`[0-9]{4}` is a few, `(?:[0-9]{2}:){100}`
a few hundred, `(?:a{1000}){1000}` a million and is refused). A field of any
length is `.*` or `[^ ]+`, which costs nothing. A stored format that this check
refuses (saved by an earlier version) is ignored: the Log files page shows whole
lines and says why. The format is stored in
`integration_manager/settings.json`, so it
survives image updates and is part of backups. The filter box searches the
whole line with secrets already masked, hidden groups included. Matching has a time limit: when a
pattern is too slow for the lines on screen, the remaining lines are shown
whole and the page says so.
