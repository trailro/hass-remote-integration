# Security: what the container protects, and how

To report a problem, see the [Security policy](../SECURITY.md).

## Password and sessions

By default there is **no login**: whoever reaches the port can install code,
patches and service calls, so runs arbitrary code with the container's volume,
secrets, devices and networks. Treat the port like SSH access and **do not
expose it to the internet**.

Set `HRI_PASSWORD`, or `HRI_PASSWORD_FILE` (a Docker secret), to require one:

- An empty or unreadable `HRI_PASSWORD_FILE`, one that is not UTF-8 text,
  or a `HRI_PASSWORD` of only spaces or tabs or with bytes that are not
  UTF-8, is a password that failed to arrive: nothing is accepted until you
  fix it, and `POST /api/login` answers `503` with the reason (also in the
  log and on the timeline).
- Line ends at either end of `HRI_PASSWORD` are not part of it; spaces are.
  Only line ends means no password. `HRI_PASSWORD_FILE` is read once at
  process start, trimmed of spaces and line ends: restart after rotating it.
- Scripts send `Authorization: Bearer <password>` (scheme in any case). A
  `HRI_PASSWORD` that ends with a space or tab cannot be sent that way: HTTP
  drops whitespace at the end of a header value, so the header never
  matches and each try counts toward the lockout. The login form takes it;
  for scripts, choose a password without one.
- Browsers get the cookie `hri_session_<port>`, valid 30 days (an old
  `hri_session` cookie moves over by itself). As the Home Assistant app,
  whose port inside is always 8087, it is `hri_session_<the app's host
  name>`, so two apps on one host keep their sessions apart; ingress needs
  no cookie.
- **Log out** (top bar; not shown through the app's ingress) and a password
  change end every session in every browser, one opened a moment before
  included, across restarts and restores. If the volume cannot record a logout
  (full or read-only), the page says so and it holds only until the next
  restart: then sessions issued between the last recorded logout and this one
  are valid again (until they expire), and later ones end. Log out again once
  the volume is fixed. The record is synced to the disk before the logout
  answers; one that cannot be read at boot (damaged) ends every session issued
  before that boot, at every boot until the next logout writes it again.
- The signing key (`auth_key`) is created on the first boot with a password
  and after a restore. If the volume cannot take it, a key held in memory is
  used (log and timeline say so) and every session ends at the next restart,
  which retries the write.
- Lockout: 5 wrong attempts from one address (IPv6: its /64) block it for 15
  minutes; 30 within 5 minutes from all addresses refuse every attempt, the
  right one too, until the count drops (at most 5 minutes; logged, on the
  timeline). Logged-in browsers keep working. A restart of the container or
  the process clears the counts.

The password covers every path on the port, except the requests the app's
ingress proxies ([below](#home-assistant-ingress-the-app)): anything under
`/api/` without a session or `Bearer` header gets `401`, Home Assistant's
webhooks (`/api/webhook/<id>`) and other callbacks included. There is no
allowlist, so an integration that receives webhooks works only without a
password.

Browsers send cookies to every port of a host name, so another service on
the same IP address or name receives the session cookie and can overwrite it;
the port in its name only keeps two instances apart. Serve the UI under its
own host name (a reverse proxy) to isolate it.

## Network

Over plain HTTP the password and the session travel unencrypted: on an
untrusted network use a reverse proxy with TLS, an SSH tunnel or a VPN.
Behind a reverse proxy:

- do not send `X-Forwarded-For`: Home Assistant here answers it with
  `400 Bad Request`;
- all requests come from the proxy, so five wrong passwords from anywhere
  block new logins and `Bearer` scripts for 15 minutes;
- set `HRI_COOKIE_SECURE=1` (or `true`, `yes`, `on`, any case) to mark the
  cookie `Secure`; any other value leaves it unmarked, with a warning in the
  log for one that is not `0`, `false`, `no` or `off`.

To allow only the Docker host, bind the port to localhost in
`docker-compose.override.yml`:

```yaml
services:
  hass-remote-integration:
    ports: !override
      - "127.0.0.1:8087:8087"
```

The boot status page, served until Home Assistant runs (`HRI_APT_PACKAGES`,
PyPI lookup, version install, requirements, scheduled restore, removing unused
venvs, a failed restore waiting for a retry, a start refused because a newer
version wrote the configuration), has no login. With a password set it says
only that Home Assistant is being installed, is not started, or that its
start was refused; the details are in the container log. With no password
set, or to the app's ingress, it also shows the version, the phase (naming
the `HRI_APT_PACKAGES` packages), the backup a failed restore needs and why a
start was refused, and without a password the tail of the last install log.
Under `/api/` it answers `503` with JSON (only `/api/alive`, the healthcheck,
gets `200` and `{"alive": true}`): `installing` (`false` while a failed
restore or a refused start holds the boot) and `error`; with no password set,
or to the app's ingress, also the version, the phase and `restore_failed`.

## Built-in protections

| Protection | What it does |
|---|---|
| Host guard | Against DNS rebinding: serves only IP addresses, `localhost` and `.local`, `.lan`, `.home`, `.internal`, `.localdomain`, `.home.arpa` names (trailing dot allowed); add others under *allowed host names* on **System**. Not applied to the app's ingress requests (below). |
| Cross-origin | State-changing requests need JSON or an explicit header, the expensive reads `X-Requested-With: fetch` ([API](api.md)); no CORS on the manager's routes. |
| Onboarding | `/api/onboarding…` answers `403`, or an integration that loads `frontend` or `panel_custom` would let any page create the owner account. |
| CSP | Scripts only from the manager's static files (no inline), no plugins, framing only by the same origin (`frame-ancestors 'self'`: Home Assistant's panel, under ingress), no `<base>`; inline style attributes and the login page's `<style>` allowed. |
| Log files | Only regular `*.log` files and rotated copies with one hard link; the listing (needs `X-Requested-With: fetch`), tail, download and zip open with `O_NOFOLLOW`, so no link or swapped path reaches another file. |
| Service calls | Dangerous domains are refused over MQTT and from the UI. Only MQTT calls are limited to published entities; the **Services** page and `POST /api/services/call` reach any entity, `entity_id: all` and excluded ones included. |

## Home Assistant ingress (the app)

As a Home Assistant app ([app](app.md#access)) the UI is also served through
Home Assistant's ingress: the Supervisor proxies the panel to the same port
with the prefix stripped. A request counts as ingress only when both hold:

- the app is running as the app (`HRI_APP`, which HRI sets itself at boot
  from the Supervisor's options; never set it on a Docker install);
- the TCP peer of the connection is the Supervisor, `172.30.32.2`. Headers do
  not count: an `X-Ingress-Path`, `X-Hass-Source` or `X-Remote-User-Name` sent
  from anywhere else changes nothing.

For those requests HRI drops the `X-Forwarded-*` headers before Home
Assistant's forwarded middleware (which would answer `400`), and skips the
host guard (the `Host` is your Home Assistant's name) and the password: Home
Assistant's login is the gate, and `ingress_users` (`HRI_INGRESS_USERS`)
limits it to the user names listed, from the `X-Remote-User-Name` the
Supervisor sets. Ingress is open to every logged-in Home Assistant user,
administrator or not (the panel shows only for administrators, which hides it
and nothing more), so `ingress_users` is what restricts HRI's panel. The
Supervisor drops a client's own `X-Remote-User-Id` and `X-Remote-User-Name`
only when the name is spelled exactly as its own; a copy spelled in another
case (`x-remote-user-name`) replaces its own header on the way to the app.
HRI therefore requires the Supervisor's exact spelling: `X-Remote-User-Id`
exactly once, `X-Remote-User-Name` at most once, no other spelling of either;
anything else gets `403`, with or without `ingress_users`, so the user name
the log shows is the session's. A session the Supervisor opened without a
user (it could not find one) carries neither header: it is served while
`ingress_users` is empty and refused when it is set. A reverse proxy or
single sign-on in front of Home Assistant that injects its own
`X-Remote-User-*` headers in another spelling (lowercase, for example)
therefore gets `403` on HRI's panel; that is intended. A Home Assistant user without a login name (Trusted Networks, for example) reaches HRI with `X-Remote-User-Id` only: it is served while `ingress_users` is empty, and cannot be listed in `ingress_users`, which matches names. Home Assistant's http settings
and trusted proxies are not changed. The boot status page lets the same
requests through. Every page uses relative URLs, so the UI works under the
prefix; `frame-ancestors 'self'` lets Home Assistant frame it.

## Secrets and masking

The MQTT password, GitHub token and parent Home Assistant token are
write-only in the UI, stored mode 600, never logged, and not in the diagnostics
zip (which holds sanitized copies, `settings.json` and `mqtt-config.json`).

Masked: the diagnostics zip, the **Logs** page (message and traceback),
**Log files** tails, downloads and file names, an imported backup's
inspection, and the error text of a failed Services-page call, flow step,
config entry action, release lookup, backup or import, an entity or device
action, and a failed stop or uninstall. On MQTT, the health document's reasons
and `last_error`, and a manager action's `error` and `note`, are masked the
same way. The manager's own log lines quoting an integration's exception are
masked before they are written.
The MQTT command history, status and log follow a rule of their own
([below](#mqtt-command-history)).

| Masked as `***` | Rule |
|---|---|
| Names anywhere | a name containing `password`, `passwd`, `passphrase`, `passcode`, `passkey`, `pincode`, `usercode`, `secret`, `token`, `credential`, `apikey`, `bindkey`, `psk`, `hmac`, `signature`, `authorization`, `webhook_id`, `cloudhook_url` |
| Names as a word | `pass`, `pw`, `pwd`, `pin`, `otp`, `auth`, `sig`, `bearer`, `irk`, `ltk`, `csrk`, `session_id`, `sessionid`, `cookie`, `set-cookie` (`db_pw`, `basic_auth`; not `author`, `oauth`, `bypass`) |
| `…key` | every name ending in `key` (`local_key`, Z-Wave `s2_*_key`, `api-key`) |
| `…code` | `code` as a word (`user_code`, `device_code`) |
| JSON keys | in the zip: any non-empty, non-boolean value under such a key; a block whose own keys name secrets is masked field by field |
| Key material | whole PEM blocks; a BEGIN line without END masks the rest of its line and the base64-only lines under it; a lone base64 line of 40+ characters (not hex, not a lower-case path) |
| No name needed | `Bearer`/`Basic` + a token of 8+ characters (not a plain word); `ghp_…` and `github_pat_…` tokens; the password in `scheme://user:password@host`, `/` and `@` included; credential URL parameters (below) |

A name counts singular or plural, with `=`, `:`, `%3D` or `%3A` and optional
spaces as separator. Its value is masked **to the end of the line** as one
`***`, wrapper and auth scheme included (`password=SecretStr(value='x')`,
`Authorization: Digest username="u", response="…"`), because describing value
shapes always missed one. It ends earlier only at:

- its own closing quote (`"`, `'`, triple, escaped `\"` or `\'`); inside
  quotes `)` closes nothing, and an unclosed quote runs to the end of the line;
- a delimiter closing one opened before the name (the message's quote, the
  JSON object's brace);
- the next top-level `name=` pair, except after `Authorization` and `Cookie`,
  which run to a URL or another masked name.

Left readable on purpose:

- result codes `status_code`, `error_code`, `exit_code`, `return_code`,
  `reason_code`, `http_code`, `response_code`, and `translation_key`,
  `sort_key`, `primary_key`;
- a value with no name in front of it, other than the shapes above;
- a URL after the first `@` a host follows: `rtsp://admin:p@ss/w0rd@host`
  shows `@ss/w0rd`, so `http://u:p@host/users/@me` keeps its path.
  Percent-encode `@` in a URL password;
- a URL password over 1024 characters containing `/` (illegal in a userinfo,
  RFC 3986);
- key material with no marker in the window and no name (a key cut mid-write,
  bytes in a sentence). This is best effort: do not share such logs casually.

Log searches run on the masked text: part of a key finds nothing, and neither
`cursor`, `truncated`, how far a Log files search read nor the timing depends
on what is masked.

### MQTT command history

The MQTT command history (`GET /api/mqtt/commands`), the status document and
the log mask by the *Names anywhere* and *Names as a word* lists above, with
four differences:

- the name must end the key, in the singular (`credentials` aside):
  `password_hint` and `passwords` are masked in a log, not here;
- `key` and `code` count only as a word of their own (`api_key`, `user_code`;
  not `hotkey`, `zipcode`, `code_format`);
- `session_id`, `sessionid`, `cookie` and `set-cookie` are not masked here;
- result codes are not exempt: `status_code` and `error_code` are masked.

`translation_key`, `sort_key` and `primary_key` stay readable. The names count
as JSON keys and also inside a data field's text (`password=x` or
`Authorization: Bearer x` in a `message`); the value becomes `***` in the
quotes it had: one after a bare name stays unquoted (`token=***`), one after a
quoted key gets the key's quotes (`"pin": "***"`), so JSON stays JSON. The
shapes that need no name (a lone `Bearer` token, a password in a URL) apply
only to a service's error message. What else is masked there (password-mode
`text` values) is in the [MQTT reference](mqtt.md#masking).

### Request lines and the raw logs

Every request line goes to `process.log` and the container log. Before it is
written:

- on `/api/logs`, `/api/log_files/tail` and `/api/log_files/download`, in any
  spelling (`/API/logs`, `//api/logs`, `/api/logs;x`, `/x/../api/logs`, also
  when answered `404`), every parameter becomes `***` except `level`, `limit`,
  `since_id`, `lines`, `id` and `prefix` in the form the pages send: the
  search text and the `file` asked for are never written;
- elsewhere, a parameter becomes `***` when its name holds a *Names anywhere*
  entry, `passw`, `pwd` or `cookie`, or has as a word `pass`, `pw`, `pwd`,
  `pin`, `otp`, `auth`, `sig`, `bearer`, `irk`, `ltk`, `csrk`, `key`, `code`,
  `session` or `sessionid`. Words end at `_`, `-`, `.`, a digit or a
  camelCase hump (`authSig`, `api_key`; not `zipcode`, `keyword`, `author`,
  `design`, `translation_key`, `sort_key`, `primary_key`). A percent-encoded
  name counts decoded, also with `%3D`.

Unmasked lines an earlier version wrote are masked where shown and in the zip.

Nothing else is masked on the way in (it would slow the whole process):
`process.log` and `docker logs` hold what Home Assistant and the integration
logged, `password=…`, `Authorization` headers and PEM blocks included. Share
`docker logs` output with care; the log pages and the zip give masked copies.

## Backups

Backups hold secrets (tokens in `settings.json`, the broker password in
`mqtt.json`, `secrets.yaml`, `.storage` credentials): keep them as private as
the volume. The login key and logout record are left out, so a restore never
revives a logged-out session. The key of an encrypted Home Assistant backup
you import serves that request only. See [Files on the volume](files.md).
In the Home Assistant app, a Home Assistant backup of the app holds these
backups (installed app 0.25.2 or newer), and with them past credentials:
password-protect Home Assistant backups, the only way Home Assistant encrypts
the app's part ([app backups](app.md#backups)).

An imported Home Assistant backup must be Home Assistant's uncompressed
`.tar`, with a configuration archive of at most 2 GB and a `backup.json` of at
most 1 MB (regular files, valid JSON), extracting at most 2 GB and decompressing
at most 20 times the archive's size (at least 2 GB, skipped members included).
An extended tar header over 1 MB, or more than 100000 files, in the backup or
its configuration archive is refused. Config entries whose id is not plain
letters and digits are skipped.

## Releases and requirements

A release is downloaded up to 100 MB and unpacked up to 300 MB and 20000
files; symbolic links in it are skipped. A manifest requirement that is a pip
option (`--index-url …`, `-e …`), a direct URL (`pkg @ https://…`,
`pkg @ git+https://…`, `pkg @ file://…`) or invalid blocks the preflight and
refuses the install (release or dev-mode directory) and the start: Home
Assistant never counts a URL requirement as installed, so pip would run at
every boot. The environment builder downloads exactly the commit its Check
verified.

## The parent Home Assistant token

The URL and long-lived token given on **Cutover** only read from your main
Home Assistant over its websocket API: entity and device registries, states,
its configuration and the integration's config entries. The token carries
every right of its user, so create it under a dedicated non-admin user. It is
write-only, stored in `settings.json` (mode 600, in backups), and sent only to
the URL it was saved with: changing the URL without a new token clears it. A
URL with `user@` or `user:password@` is refused.

## MQTT

- Without `tls`, the connection and the MQTT password travel unencrypted.
  With `tls` on and `tls_insecure` off, the certificate must verify and name
  the host.
- Whoever reads the base topic reads every published state and attribute;
  only `access_token` and URLs with `token=` are left out (a token in a state
  URL becomes `***`). Name-based masking does not apply to entity documents;
  the health document's reasons and `last_error`, and a manager action's
  `error` and `note`, are masked as on the Logs page.
- Whoever publishes under the base topic can command the published entities
  and call services (deny list applies), and run manager actions when
  `manager_commands` is on ([MQTT reference](mqtt.md)).
