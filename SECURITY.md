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
  changed or after a logout, or guessing faster than the lockout allows;

- a way around the protections that do exist: the Host header guard against
  DNS rebinding, the JSON requirement for state-changing requests, the absence
  of CORS on the manager's routes;
- secrets (MQTT password, GitHub token, the main Home Assistant's token,
  backup encryption keys) exposed through the API, the logs, the timeline, the
  diagnostics zip or the UI;
- service calls over MQTT or from the UI that get past the deny list, or MQTT
  commands and service calls that reach entities the container does not
  publish through the target (entity, group, device, area, floor, label) or
  through the entity fields the README lists; an entity id passed in a
  service field with another name is a documented limit, not a bypass;
- path traversal or unsafe archive handling in backups, restores, imports,
  patches or log files;
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
- **Other services on the same host name see the session cookie.** Browsers
  send cookies to every port of a host, so a web app on another port of the
  same IP address or name receives `hri_session_<port>` and can overwrite it.
  The port in the cookie name only separates instances of this project. Serve
  the UI under its own host name (a reverse proxy) when other web apps share
  the machine; a report that relies on a hostile app on the same host name is
  not a vulnerability in this project.
- **GitHub Actions are pinned to commit SHAs** and updated by Dependabot; the
  Python packages the manager adds next to Home Assistant are listed in
  `requirements.txt` with a lower bound, most with an upper bound too (`regex`
  is date-versioned and has only a floor), and resolved against Home
  Assistant's constraints.
