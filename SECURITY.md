# Security policy

How the container protects itself, and how to run it safely, is in
[Security: what the container protects, and how](docs/security.md).

## Supported versions

Security fixes go into the latest release. Check that the problem still exists
there before reporting it.

## Reporting a vulnerability

Do not open a public issue. Report it privately: on this repository's
**Security** tab, choose **Report a vulnerability**. Include the version, the
steps to reproduce and what an attacker gains. Reports are answered as soon as
possible, and fixes are credited in the release notes unless you prefer
otherwise.

## What counts as a vulnerability

Without `HRI_PASSWORD` there is no login: anyone who can reach the port can
install and run code in the container. That alone is not a vulnerability, and
neither is a password read from plain HTTP traffic. These are:

- with a password set, any use of the UI or API without it: a path the check
  misses, a session forged or replayed after a password change or a logout,
  guessing faster than the lockout allows, or any password accepted while
  `HRI_PASSWORD_FILE` is empty or unreadable or `HRI_PASSWORD` holds only
  spaces or tabs. The sessions that a logout the volume could not record gives
  back at a restart are a documented limit;
- a way around the Host header guard, the JSON requirement for state-changing
  requests, the `X-Requested-With: fetch` requirement on the requests
  [API](docs/api.md) lists, or the absence of CORS on the manager's routes;
- a secret (MQTT password, GitHub token, main Home Assistant token, backup
  encryption key) exposed through the API, the logs, the timeline, the
  diagnostics zip, the UI or MQTT. That includes an entity's `access_token` or a
  URL carrying a token, in an entity document or in `GET /api/entities` for a
  published or excluded entity, and in an integration's exception text in the
  command history, the log or an answer to the UI. The masking rules and their
  deliberate gaps are in
  [Secrets and masking](docs/security.md#secrets-and-masking): a report needs a
  value those rules name that comes out unmasked;
- the text of a log search (any spelling of its path), or a credential in a
  request URL, written to `process.log` or the container log; a log search
  answer (rows, `cursor`, truncation, the lines a Log files search read) that
  differs between a right and a wrong guess of a masked value;
- a service call over MQTT or from the UI that gets past the deny list; an MQTT
  command or MQTT service call that reaches an entity the container does not
  publish, through the target (entity, group, device, area, floor, label) or
  the entity fields [MQTT reference](docs/mqtt.md) lists. Not a bypass: an
  entity id in a service field with another name, or a call from the Services
  page or `POST /api/services/call` (the admin UI) to an unpublished entity;
- with MQTT `tls` on and `tls_insecure` off, a connection to a broker whose
  certificate is not verified or does not name the host;
- path traversal or unsafe archive handling in backups, restores, imports,
  patches or log files: a log file listing, tail or download that reaches a
  non-log file through a symbolic or hard link or a path swapped after the
  listing, or a backup that reads a file outside `/config` through a symbolic
  link;
- anything that lets a page on another origin make the manager do something.

Problems in Home Assistant itself or in the integrations you run belong to
those projects.

## Design choices that are not vulnerabilities

- **The image runs as root inside the container.** The integration needs its
  hardware (serial and USB devices whose group differs by host), and existing
  volumes are owned by root. Keep the container unprivileged (no
  `--privileged`), pass only the devices it needs, and keep the volume private
  to Docker.
- **`HRI_APT_PACKAGES` installs Debian packages as root.** They come from
  Debian's repositories through apt at boot; names are validated, never reach a
  shell and are exact package names, never patterns, but maintainer scripts run
  as root like any `apt-get install`. Only the operator sets it, in the
  container's environment; nothing in the UI, the API or an integration can.
- **Other services on the same host name see the session cookie**, since
  browsers send cookies to every port. A report that relies on a hostile app on
  the same host name is out of scope; see
  [Password and sessions](docs/security.md#password-and-sessions).
- **GitHub Actions are pinned to commit SHAs and the base image to a digest**,
  both updated by Dependabot. The Python packages the manager adds next to Home
  Assistant are in `requirements.txt` with a lower bound, most with an upper
  bound too (`regex` is date-versioned and has only a floor), resolved against
  Home Assistant's constraints.
- **State attributes are published as the integration sets them.** Only
  `access_token` and URLs carrying `token=` are left out, at any depth, and a
  token in the state is masked. Filtering by name (`password`, `pin`,
  `api_key`) would drop real data (`error_code`, `zip_code`, a GPIO `pin`). A
  secret in a state attribute is a bug of that integration; see
  [MQTT reference](docs/mqtt.md).
