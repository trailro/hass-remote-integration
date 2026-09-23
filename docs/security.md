# Security: what the container protects, and how

## Security

By default there is **no login**, like many self-hosted appliances on a
trusted LAN. Set `HRI_PASSWORD` (or `HRI_PASSWORD_FILE`, for example a Docker
secret) to require a password:

- a `HRI_PASSWORD_FILE` that cannot be read, or that is empty (a Docker secret
  created but never populated), and a `HRI_PASSWORD` of only spaces or tabs (a
  template that rendered blank), are treated as a password that failed to arrive:
  nothing is accepted until it is fixed, a login attempt says why (`POST
  /api/login` answers `503` with the reason), and the reason
  is in the log and on the timeline;
- the browser gets a session cookie from the login page, valid for 30 days,
  and **log out** in the top bar ends every session of the UI, in all browsers,
  including one opened a moment before (each logout starts a new session
  generation, signed into the cookie and kept across restarts and restores).
  When the volume cannot record the logout (full or read-only), every session
  still ends and the page says so, but only until the container restarts: the
  volume keeps the generation of the last logout it recorded, so at the restart
  the sessions issued between that logout and the failed one are valid again
  (until they expire), and the ones issued after the failed one (a login right
  after it included) end. Log out again once the volume is fixed;
- the key that signs the session cookies is created on the first boot with a
  password, and again after a restore (backups leave it out). When the volume
  cannot take it (full or read-only), the manager comes up anyway with a key
  held in memory: the password is asked for as always, but every session ends
  at the next restart, which tries the write again. The log and the timeline
  say so;
- scripts send the password as `Authorization: Bearer <password>` (the
  scheme in any case);
- a line end at either end of `HRI_PASSWORD` (an `.env` file saved with
  Windows line ends) is not part of the password, since no login form or header
  can carry one; spaces are, so a password may start or end with one. A value
  of nothing but line ends is no password (no login); a value of nothing but
  spaces or tabs is a password that failed to arrive (see above). The file
  of `HRI_PASSWORD_FILE` is read without the spaces and line ends around it,
  once, when the process starts: after changing it (rotating a Docker
  secret) restart for the new password to count;
- after 5 wrong attempts from one address, that address is refused for 15
  minutes (for IPv6, the whole /64 it belongs to); after 30 wrong attempts
  within 5 minutes from all addresses together (for example from a whole IPv6
  range), every password attempt is refused, also the right one, until the
  count drops (at most 5 minutes; logged and in the timeline). Browsers already
  logged in keep working. The counts are held in memory, so a restart (of the
  container, or the process restart the UI offers) clears them;
- changing the password logs every browser out.

The password covers every path on the port, Home Assistant's own included:
anything under `/api/` without a session or `Bearer` header gets `401`. That
includes webhooks (`/api/webhook/<id>`) and other callbacks that a cloud
service or a device on the LAN sends to Home Assistant, and there is no
allowlist. An integration that receives webhooks only works without a password.

The session cookie is named `hri_session_<port>` (a session from an older
version under `hri_session` moves to the new name by itself). Browsers send
cookies to every port of a host name, so any other service on the same IP
address or name receives the cookie and can overwrite it; the port in the name
only keeps two instances on one host from logging each other out. To isolate
the UI from other web apps on the same machine, serve it under its own host
name (a reverse proxy with its own name, see below).

Over plain HTTP the password and the session travel unencrypted, so on a
network you do not trust put the UI behind a reverse proxy with TLS. The status
page served on every boot until Home Assistant runs (the system packages of
`HRI_APT_PACKAGES`, the PyPI lookup, a version install, the requirements, a
scheduled restore, removing unused venvs, or while a failed restore waits for a
retry) is not protected. It shows the phase, which names the Debian packages
`HRI_APT_PACKAGES` asks for, and, for a failed restore, the name of the backup
to restore from; until Home
Assistant starts and while no password is set, it also shows the tail of the
last install log, which the System page shows to anyone without a
password anyway. While a failed restore holds the boot, `/api/` paths answer
`503` with `installing: false` and `restore_failed: true`.

Behind a reverse proxy, note that Home Assistant's HTTP server in the container
is not set up for proxies: it answers `400 Bad Request` to any request that
carries an `X-Forwarded-For` header, so configure the proxy not to send one.
Every request then comes from the proxy's address: five wrong passwords from
anywhere block new logins and `Bearer` scripts behind that proxy for 15 minutes
(browsers already logged in keep working). The server cannot tell that the
proxy speaks TLS, so set `HRI_COOKIE_SECURE=1` to mark the session cookie
`Secure`. An SSH tunnel or a VPN avoids all of this.

Without a password, anyone who can reach the port controls the container. The UI installs code
from any GitHub repository, accepts Python patches and runs service calls, so
access to the port means running arbitrary code inside the container, with
access to its volume, its secrets and every device or network it can reach.
Treat the port like SSH access to that container.

To make the UI reachable only from the Docker host, bind the port to localhost
in your `docker-compose.override.yml` and use an SSH tunnel or a reverse proxy
with authentication for remote access:

```yaml
services:
  hass-remote-integration:
    ports: !override
      - "127.0.0.1:8087:8087"
```

What is in place:

- A host-header guard against DNS rebinding: requests are served for IP
  addresses, `localhost` and local names (`.local`, `.lan`, `.home`,
  `.internal`, `.localdomain`, `.home.arpa`); add other names under *allowed host names* on
  **System**. A name written with its trailing dot (`hri.local.`) counts as the
  same name.
- State-changing requests need JSON or an explicit header, so a web page on
  another origin cannot trigger them; neither can it trigger the expensive
  reads (see *API*).
- Home Assistant's onboarding API (`/api/onboarding…`) answers `403`. An
  integration that depends on `frontend` or `panel_custom` loads it, and while
  no Home Assistant user exists it would let any page create the owner account.
- The **Log files** page lists regular log files only (`*.log` and rotated copies such as
  `*.log.1`); symbolic links and files with more than one hard link are skipped,
  so neither kind of link can put another file of the volume (`secrets.yaml`) on
  the page. Listing them needs `X-Requested-With: fetch`, like reading a tail.
- A `Content-Security-Policy` on every response: scripts only from the
  manager's own static files (no inline script), no plugins, no framing by
  other pages, no `<base>` rewrites. Inline style attributes are allowed, and
  so is the login page's own `<style>` element.
- Secrets (MQTT password, GitHub token, parent HA token) are write-only in the
  UI, stored in files readable only by the owner, and never logged or included
  in the diagnostics zip. The diagnostics zip, the log file tails and
  downloads, the records on the Logs page (message and traceback), the
  inspection of an imported Home Assistant backup, and the error text a failed
  service call (Services page), config or options flow step, config entry
  action, release lookup, backup or Home Assistant import answers with mask
  passwords (also `pass`, `pw`, `pwd`, passphrases, passcodes and PIN codes),
  tokens, credentials, session ids, signatures, WiFi and other
  pre-shared keys (`psk`, `wifi_psk`), `auth` as a word of its own (`auth`,
  `basic_auth`, not `author` or `oauth`), every value named `…key` (`local_key`,
  `noise_psk`, `encryption_key`, Z-Wave `network_key`, `s0`/`s2_*_key` and
  `lr_s2_*_key`, `security_key`, `bindkey`, `aes_key`, `ssl_key`, `?key=` in
  URLs, …) except `translation_key`, `sort_key` and `primary_key`, Bluetooth
  `irk`/`ltk`, whole PEM blocks, PINs, one-time codes, HMAC keys, webhook ids
  and cloudhook URLs, `Authorization` values (`Bearer`, `Basic` and any other
  scheme) and a value named `bearer`, `Cookie`/`Set-Cookie` values and
  credentials in URLs (also a
  password holding `/` or `@`). Masking errs on the side of hiding too much.
  A value the masking finds a name for is masked to the end of its line —
  wrapper, container and auth scheme and all — as a single `***`: the rule no
  longer decides what a value looks like, because the shape nobody had described
  was the one that got printed. Three things end it earlier, and none of them
  can be part of it: a quote the value opened with (an escaped one, `\"` or
  `\'`, and a triple quote count; one that never closes, a line cut short, takes
  the rest of the line with it), a quote or bracket that opened before the name,
  and the next `name=` pair — which `Authorization` and `Cookie` are exempt
  from, because their own value is written as name=value pairs. A name counts
  singular or plural, and the separator may be `=`, `:` or a percent-encoded
  spelling of either. A name ending in `code` is a secret (`user_code`,
  `device_code`), but the codes that report a result stay readable:
  `status_code`, `error_code`, `exit_code`, `return_code`, `reason_code`,
  `http_code` and `response_code`. SECURITY.md has the whole rule.
  The searches on the Logs and Log files pages run on the masked text, so
  looking for part of a key finds nothing: a row that appeared only while the
  search matched the key would let it be read out one character at a time.
  For the same reason, what a search answers besides its rows (the Logs page's
  `cursor` and `truncated`, how far a search on the Log files page reads) and
  how long it takes do not depend on what the masking hides.
  The web server logs every request line to `process.log` and the container
  log. Before a line is written, the search text of the Logs and Log files
  pages, the `file` a tail or a download asks for, and URL parameters named like a credential
  (`access_token`, `authSig`, …) become `***`, so searching for your own secret
  does not write it to disk. A search path counts in any spelling (`/API/logs`,
  `//api/logs`, `/api/logs;x`, `/x/../api/logs`), including the ones the
  server answers with 404 but still logs; lines written by an earlier version are masked where
  they are shown and in the diagnostics zip. A parameter name counts as a
  credential when it holds `token`, `secret`, `password`, `authorization`,
  `hmac`, `psk`, `webhook_id` and the like anywhere, or `pass`, `pw`, `pwd`,
  `pin`, `otp`, `auth`, `sig`, `bearer`, `key`, `code` or `session` as a word of
  its own (`authSig`, `api_key`, `basic_auth`, but not `zipcode`, `keyword`,
  `author` or `design`; `translation_key`, `sort_key` and `primary_key` stay
  readable), and a percent-encoded name counts as its decoded one, also when
  the `=` after it is percent-encoded (`%3D`). The request line, the
  diagnostics zip, the log pages and the MQTT command history mask by the same
  list of names.
  That is all that is masked before a line is written: `process.log` and the
  container log (`docker logs`, and any log driver) otherwise hold what Home
  Assistant and the integration logged, as they logged it — an integration that
  logs `password=…`, an `Authorization` header or a PEM block puts it there.
  The masking of names, headers, URL credentials and keys happens where the
  manager hands a log out: the Logs page, the Log files page (tail and
  download) and the diagnostics zip. The manager's own lines that quote an
  integration's exception (a failed MQTT command, a failed call from the
  Services page) are masked before they are logged. Masking every line on its
  way to the log would put the scrubber on the logging path of the whole
  process, the event loop included, at tens of microseconds a line and
  milliseconds for a long one. Share `docker logs` output with that in mind. A
  `-----BEGIN …-----` line with no END marker masks the
  rest of its own line and the base64-only lines under it, and nothing else.
  A key is recognised from its `-----END …-----` marker or from the shape of
  its own lines, so a search result or a tail that starts in the middle of a
  block is masked too; key material with no marker anywhere in the window and
  no name in front of it (a key cut off mid-write, or bytes pasted into a
  sentence) can still get through, which is why a log with secrets in it should
  not be shared casually.
  Backups contain
  them; the login key and the logout record stay out of backups, so a restore
  never revives a logged-out session. The key of an encrypted Home Assistant
  backup you import is only used for that request.
- A release is downloaded only up to 100 MB and unpacked only up to 300 MB and
  20000 files; symbolic links in the archive are skipped. A requirement in a
  manifest that is a pip option (`--index-url …`, `-e …`), a direct URL
  (`pkg @ https://…`, `pkg @ git+https://…`, `pkg @ file://…`) or not a valid
  requirement blocks the preflight and refuses the install (a release, or a
  dev-mode install from a directory) and the start: it
  would change what gets installed from where, and Home Assistant never counts
  a URL requirement as installed, so it would go to pip again at every boot.
  The environment builder downloads exactly the commit its Check verified.
- An imported Home Assistant backup must be the uncompressed `.tar` Home
  Assistant writes. Its configuration archive may be at most 2 GB, its
  `backup.json` at most 1 MB, and what it extracts at most 2 GB. Reading its
  configuration archive may decompress at most 20 times the archive's size (at
  least 2 GB), skipped members included, and an extended tar header over 1 MB,
  in the backup or in its configuration archive, is refused; neither the
  backup nor its configuration archive may hold more than 100000 files. A `backup.json`
  that is not a regular file or not valid JSON, or a configuration archive that is not a
  regular file, is refused with that reason. Config entries
  with an invalid id are skipped.
- Dangerous service domains are not callable, over MQTT or from the UI. Only
  a call over MQTT is limited to the entities the container publishes: the
  **Services** page and `POST /api/services/call` belong to the admin UI and can
  target any entity, `entity_id: all` and excluded entities included.
- Without `tls` (see *MQTT reference*), the broker connection, the MQTT
  password included, travels unencrypted.

**Do not expose the port to the internet.** Put it behind a reverse proxy with
authentication if you need remote access.

To report a security problem, see [SECURITY.md](../SECURITY.md).

---
