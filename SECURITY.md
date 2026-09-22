# Security policy

## Supported versions

Security fixes go into the latest release. Please check that the problem still
exists there before reporting it.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately
instead: on this repository's **Security** tab, choose **Report a
vulnerability**. Include the version, the steps to reproduce it and what an
attacker gains. Reports are answered as soon as possible, and fixed problems
are credited in the release notes unless you prefer otherwise.

## What counts as a vulnerability

Without `HRI_PASSWORD` the web UI and API have no login (see *Security* in the
README): anyone who can reach the port can install code and run it in the
container. That alone is not a vulnerability, and neither is a password read
from plain HTTP traffic. These are:

- with a password set: any way to use the UI or the API without it, such as a
  path the check misses, a forged or replayed session after the password
  changed or after a logout (other than the sessions a logout the volume could
  not record gives back at a restart, a documented limit), or guessing faster than the lockout allows; also
  while `HRI_PASSWORD_FILE` is empty or unreadable, or `HRI_PASSWORD` holds only
  spaces or tabs, which must refuse every password;

- a way around the protections that do exist: the Host header guard against
  DNS rebinding, the JSON requirement for state-changing requests and the
  `X-Requested-With: fetch` requirement on the requests the README lists, the absence
  of CORS on the manager's routes;
- secrets (MQTT password, GitHub token, the main Home Assistant's token,
  backup encryption keys) exposed through the API, the logs, the timeline, the
  diagnostics zip, the UI or what is published over MQTT (an entity's
  `access_token`, or a URL carrying a token in a document, in what `GET /api/entities` answers for a
  published or an excluded entity alike, and in the error an integration's own exception puts in the
  command history, the log or the answer to a request from the UI); the log
  searches run on the masked text and a
  search for key material returns nothing by design, but redaction of material
  that carries no marker and no name in front of it is best effort — a report
  needs a case where something the scrubber does name comes out unmasked. A
  named value is masked from the separator to the **end of the line**: the
  scrubber no longer decides what a value looks like, because four review
  rounds each found a shape nobody had described yet — a `)` inside a quoted
  password, a tuple on a line the logger cut, a triple-quoted value, a plural
  name, a `=` that arrived percent-encoded — and each time the shape nobody had
  described was the one that got printed. Over-masking is the deliberate trade:
  a name the scrubber knows takes the rest of its line with it, wrapper,
  container, auth scheme and all (`password=SecretStr(value = 'x')`,
  `passwords=['x']`, `Authorization: Digest username="u", response="…"`), as a
  single `***`. The name matches singular or plural, and the separator may be
  `=`, `:` or their percent-encoded spellings. Three things end the value
  earlier, and none can be part of it: a quote it opened with (a quote that
  never closes takes the rest of the line, and inside quotes a `)` closes
  nothing), a delimiter that closes one opened *before* the name (the quote of
  the message a token is written inside, the brace of the JSON object it sits
  in), and the next top-level `name=` pair — from which `Authorization` and
  `Cookie` are exempt, because their own value is written as name=value pairs.
  Two things are deliberately not masked: a value with **no name in front of
  it** (reading a bare identifier as a wrapper once made the scrubber print a
  token it had been masking), and a URL password over 1024 characters that also
  contains `/`, which is not legal in a userinfo (RFC 3986). A name ending in
  `code` is a secret (`user_code`, `device_code`); the codes that report a
  result are not (`status_code`, `error_code`, `exit_code`, `return_code`,
  `reason_code`, `http_code`, `response_code`). `rtsp://admin:p@ss/w0rd@host` still shows
  `@ss/w0rd`: the rule stops at the first `@` a host follows, because
  `http://u:p@host/users/@me` is the same text and its path has to stay
  readable — percent-encode an `@` in a URL password;
- the text of a log search (in any spelling of its path), or a credential in a
  request URL, written to
  `process.log` or the container log; a log search answer (rows, `cursor`,
  truncation, the lines a Log files search read) that differs between a right
  and a wrong guess of a masked value;
- service calls over MQTT or from the UI that get past the deny list, or MQTT
  commands and service calls over MQTT that reach entities the container does
  not publish through the target (entity, group, device, area, floor, label) or
  through the entity fields the README lists; an entity id passed in a
  service field with another name is a documented limit, not a bypass, and so
  is a call from the Services page or `POST /api/services/call` (the admin UI)
  that targets an entity the container does not publish;
- with MQTT `tls` on and `tls_insecure` off: a connection to a broker whose
  certificate is not verified, or that does not name the host;
- path traversal or unsafe archive handling in backups, restores, imports,
  patches or log files, including a log file listing, tail or download that reaches a file
  other than a log through a symbolic or hard link, or a backup that reads a file
  outside `/config` through a symbolic link — the listing, the tail, the
  download and the diagnostics zip share one opener, which opens with
  `O_NOFOLLOW` and then checks the opened file is a regular file with a single
  name, so a path swapped between the listing and the request is refused;
- anything that lets a page on another origin make the manager do something.

Problems in Home Assistant itself or in the integrations you run belong to
those projects.

## Design choices that are not vulnerabilities

- **The image runs as root inside the container.** The integration needs the
  hardware it talks to (serial and USB devices whose group differs from host to
  host), and existing volumes are owned by root. A non-root user would break
  those installs for a small gain in a single-purpose container. Keep the
  container unprivileged (no `--privileged`), pass only the devices it needs,
  and keep the volume private to Docker.
- **`HRI_APT_PACKAGES` installs Debian packages as root inside the
  container.** They come from Debian's own repositories, through apt, at boot;
  the names are validated, never reach a shell and are taken as exact package
  names (never as a pattern over the archive), but a package's maintainer
  scripts run as root like any `apt-get install`. Only the operator sets this —
  it is part of the container's environment, and nothing in the UI, the API or
  an integration can change it.
- **Other services on the same host name see the session cookie.** Browsers
  send cookies to every port of a host, so a web app on another port of the
  same IP address or name receives `hri_session_<port>` and can overwrite it.
  The port in the cookie name only separates instances of this project. Serve
  the UI under its own host name (a reverse proxy) when other web apps share
  the machine; a report that relies on a hostile app on the same host name is
  not a vulnerability in this project.
- **GitHub Actions are pinned to commit SHAs and the base image to a digest**,
  both updated by Dependabot; the
  Python packages the manager adds next to Home Assistant are listed in
  `requirements.txt` with a lower bound, most with an upper bound too (`regex`
  is date-versioned and has only a floor), and resolved against Home
  Assistant's constraints.
- **State attributes are mirrored as the integration sets them.** Only
  `access_token` and URLs carrying a `token=` are left out of entity documents,
  at any depth in the attributes (they open this container's proxy); a token in
  the state itself is masked as `***`. An attribute named `password`, `pin` or
  `api_key` is published like any other: filtering by name would drop real data
  (`error_code`, `zip_code`, a GPIO `pin`) that the main Home Assistant shows
  today. A secret in a state attribute is a bug of that integration.
