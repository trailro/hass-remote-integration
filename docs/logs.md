# Logs and log files

The **Logs** page (the process log), the **Log files** page (files the
integration writes), their limits, and splitting lines into columns. Both
pages mask secrets before anything is shown, searched or downloaded; the rules
are in [security.md](security.md).

## Logs

**Logs** shows everything Home Assistant and the integration log, with
filters, a search that runs once typing pauses, and a live follow. Each line
carries its date and time. The list holds the newest 200 lines; following
live drops the oldest. A live follow keeps advancing even when a batch of new
lines matched only inside masked values or a level or logger filter matched
nothing, and a follower that fell behind reads on at once.

**Log levels.** Loggers listed in the registry's `quiet_loggers` start at
WARNING; raise one at runtime while you investigate. The root logger cannot be
set, since it would silence or flood every logger at once: raise the
integration's own logger instead.

No single line can end the log: text that is not valid UTF-8 (a file name an
`OSError` brings into a message) is stored with those characters escaped, and
a handler that raises costs only that line. When more than 50000 lines wait to
be written (a blocked output), newer lines are dropped and a warning says how
many.

## Log files

**Log files** appears in the menu only when the integration writes files of
its own, such as traffic dumps or debug logs. They are found through:

- the integration's config entries: any setting ending in `.log`, with its
  rotated copies;
- `*.log` files and their rotated copies (`*.log.1`, `*.log.2026-09-10`) in the
  registry's `log_dir` and in the config root.

Symbolic links are never listed, nor a file with more than one hard link (a
`secrets.yaml` linked under a `*.log` name would look like a log). Rotation by
rename or copy leaves one link, so no log is lost to this.

File names are masked too. Two files whose names mask to the same text are
listed separately and each opens its own file. After a restart the page
selects the same file again by name, and says so when several files share it.

| Limit | Value |
|---|---|
| Tail | the last 32 MB of a file |
| Search | stops after 20000 lines holding something the masking looks at (a word such as `token` or `key`); *lines read* says how far it got |
| One line | its first 1 MiB, ending in `[... N more bytes of this line not shown]`; *Download file* has all of it |

**Download file**, next to the tail controls, saves the selected file whole,
masked line by line as it is sent (nothing is held in memory). A key block
stays masked to its end, however far past its `BEGIN` marker and even with no
`END` marker. The file is saved under its masked name, never its real one; a
name the mask cut the extension off gets `.log` back. A file over 32 MB is
sent from its end (the newest lines): the name then says `-last-32MiB`, and the
answer carries `X-Log-Truncated: 33554432`. The button fetches with the header
the endpoint requires, which a plain link cannot send.

## Formatting

By default every line is shown whole. The **Formatting** box at the bottom of
**Log files** splits lines into columns. A format is a JSON object:

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
named group. Since compiling writes each counted repeat out, with no time
limit, the pattern may hold at most 10000 elements with every `{n}` and
`{m,n}` written out `n` times (`[0-9]{4}` is a few, `(?:[0-9]{2}:){100}` a few
hundred, `(?:a{1000}){1000}` a million and is refused). A field of any length is `.*` or `[^ ]+`, which costs nothing. A
stored format this check refuses is ignored: the page shows whole lines and
says why.

The format is stored in `integration_manager/settings.json`, so it survives
image updates and is part of backups. The filter box searches the whole line
with secrets already masked, hidden groups included. Matching has a time
limit: when a pattern is too slow for the lines on screen, the rest are shown
whole and the page says so.
