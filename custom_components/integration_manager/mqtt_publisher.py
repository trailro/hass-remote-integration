"""Publish every entity of this headless HA to MQTT, with full metadata.

Topic layout (retained JSON unless noted):
  <base>/status                                  "online" | "offline" (LWT)
  <base>/<integration>/<domain>/<object_id>      one document per entity (an integration named like one of
                                                 our own segments, e.g. "call", uses "<name>-integration")
  <base>/<integration>/event_stream/<object_id>  event entities, NOT retained
  <base>/services/<domain>                       service catalog per domain
  <base>/cmd/<domain>/<object_id>/<field>        entity commands (subscribed)
  <base>/call/<domain>/<service>                 any service call, JSON payload (subscribed)
  <base>/result/<domain>/<service>               call outcome, NOT retained
  <base>/health                                  health verdict of the running integration
  <base>/manager                                 versions, updates, resources (manager_device.py)
  <base>/manager/cmd/<action>                    manager actions, with manager_commands (subscribed)
  <base>/manager/result                          action outcome, NOT retained

The document carries the live state, all attributes, timestamps and the
registry metadata (unique_id, names, device_class, unit, icon, category,
area, device block) plus ``integration`` = the entity's platform, i.e. the
integration's domain.  Removed entities get an empty retained payload so the topic
is cleared on the broker.

paho-mqtt is used directly (no HA mqtt integration) so the container stays
minimal; publishing is thread-safe, reconnects are paho's job, and a
full republish runs on every (re)connect and hourly; the periodic pass
only re-sends documents whose content changed.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import secrets
import socket
import ssl
import re
import math
import logging
import os
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import MISSING, asdict, dataclass, field
from datetime import timedelta
from typing import Any

import logbuffer
import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.subscribeoptions import SubscribeOptions

from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import (
    EVENT_COMPONENT_LOADED,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_SERVICE_REGISTERED,
    EVENT_SERVICE_REMOVED,
    EVENT_STATE_CHANGED,
)
from homeassistant.core import CoreState, Event, HomeAssistant, State, SupportsResponse, callback, valid_entity_id
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM, async_get_platforms
from homeassistant.const import __version__ as ha_version_str
from homeassistant.helpers.event import async_track_time_interval

from . import discovery as disc
from . import events, writer
from jsonio import read_json, write_json

from .mqtt_rules import MqttRules, matches
from .services_catalog import service_rows

TLS_CHECK_INTERVAL_S = 60  # at most one diagnostic handshake per minute while paho keeps failing to connect
# "online" waits for the SUBACK, so a command the main HA sends at the availability flip is not lost; a broker that never
# answers the SUBSCRIBE gets it after this long anyway (state mirroring works without the subscription)
SUBACK_WAIT_S = 10
DROP_AFTER_CONNECT_S = 10  # a disconnect within this of a CONNACK: the broker dropped us, the settings are not the problem

_LOGGER = logging.getLogger(__name__)
# paho's own log (a callback that raised, a socket error): its lines name topics, flags and sizes, never the password
# or a payload.  INFO: the packet trace at DEBUG stays off with the manager's debug log, until this logger is raised
_PAHO_LOGGER = logging.getLogger(__name__ + ".paho")
_PAHO_LOGGER.setLevel(logging.INFO)
ORPHAN_SWEEP_DELAY_S = 300  # after HA started: integrations still adding entities (a device slow to answer) have had time
HEALTH_INTERVAL_S = disc.HEALTH_INTERVAL_S  # the health entities on the main HA expire after three missed publications
CLEANUP_RETRY_S = 60  # how often the removal an uninstall could not finish (the broker unreachable) is tried again
REPUBLISH_BATCH = 200          # documents per batch before yielding to the event loop
REPUBLISH_BATCH_PAUSE_S = 0.02
HEALTH_GRACE_S = 900  # after a (re)start, at most this long before unavailable / silent entities count
HEALTH_STALE_S = 900  # no state written by the integration's entities (last_reported, value changed or not) for this long = degraded

CONFIG_FILE = "integration_manager/mqtt.json"
# Entities Home Assistant creates by itself in every instance, not by the integration or by the user: zone.home is the
# container's own home location (its core configuration), and the main Home Assistant has its own.  Never published.
NEVER_PUBLISHED_INTEGRATIONS = frozenset({"zone"})
# A generic service call always answers: a service that blocks (e.g. an RF
# request that cannot be sent in read-only mode) is reported as a timeout.
def _call_timeout() -> int:
    """HRI_CALL_TIMEOUT read at import: a typo ("60s") must not break the setup of this component on every boot,
    and 0 or less would time every call out at once."""
    raw = os.environ.get("HRI_CALL_TIMEOUT", "60")
    try:
        value = int(raw.strip())
    except ValueError:
        _LOGGER.warning("HRI_CALL_TIMEOUT=%r is not a whole number of seconds: using 60", raw)
        return 60
    if value < 1:
        _LOGGER.warning("HRI_CALL_TIMEOUT=%r is below 1 second: using 1", raw)
        return 1
    return value


CALL_TIMEOUT_S = _call_timeout()
# MQTT climate publishes the two bounds of a range change on separate topics, right after each other: the
# first half waits this long for the second, so both go out as one set_temperature call
RANGE_PAIR_WAIT_S = 1.0
HISTORY_MAX = 200      # commands and calls remembered (in memory)
DEDUP_WINDOW_S = 300   # a call repeating an _id seen this recently is answered from history, not run again
CALLS_REMEMBERED = 1000  # _ids kept for that answer; beyond it the oldest is forgotten
CALLS_IN_FLIGHT_MAX = 50  # service calls and commands whose service has not returned yet (timed-out ones included)
# An _id larger than this is refused with an answer: it is kept in the dedup map for DEDUP_WINDOW_S, in the
# command history and echoed in every result, so an unbounded one costs memory in three places at once.
CALL_ID_MAX_BYTES = 128
# A clear of a retained command published by this process comes back to it on an MQTT 3.1.1 session (no
# noLocal): the topic is remembered this long, and at most this many at a time.
CLEARED_ECHO_WINDOW_S = 30.0
CLEARED_ECHO_MAX = 64
# Never callable over MQTT (anyone with broker credentials could otherwise
# stop this instance or run arbitrary commands); the catalog hides them too.
CALL_DENY_DOMAINS = frozenset({"homeassistant", "shell_command", "python_script", "hassio", "integration_manager"})
# Over MQTT only (the Services page is the operator's): dismiss_all from anyone with broker credentials would erase
# the manager's own smoke-test and HA-change notifications, and create could plant fake ones.  The system services an
# integration's dependencies can bring along take no target, so the published-entity check never refuses them:
# recorder (purge deletes history, disable stops it), logger (log levels), system_log (writes and clears log
# entries), backup (a full backup of /config each call) and conversation (process runs a sentence against every
# entity here, published or not).  None is loaded unless the running integration depends on it.
MQTT_CALL_DENY_DOMAINS = CALL_DENY_DOMAINS | {"persistent_notification", "recorder", "logger", "system_log", "backup", "conversation"}
# notify.persistent_notification creates the same notifications as persistent_notification.create
MQTT_CALL_DENY_SERVICES = frozenset({("notify", "persistent_notification")})
# A call payload is scanned before it is parsed: a huge or deeply nested one is refused with an answer
# (json.loads would raise RecursionError, and everything that walks the data after it could too).
CALL_MAX_BYTES = 256 * 1024
CALL_MAX_DEPTH = 64
# The largest packet the main connection takes from the broker, announced in its CONNECT (MQTT 5's Maximum Packet
# Size).  All it subscribes to is commands, calls and manager actions, each refused over CALL_MAX_BYTES; unannounced,
# paho reads a packet of any length the protocol allows (256 MiB) into memory first, and mosquitto forwards whatever it
# accepted (max_packet_size 0 by default).  A broker drops a larger message for this client instead of sending it (MQTT 5
# 3.1.2.11.4), so a call that large gets no answer.  The headroom covers the longest topic and the properties.  Only
# the main connection announces it: the scan clients read retained data of any size by design (a foreign document too
# large to reach them would make the base-topic probe pass), and an MQTT 3.1.1 connection cannot announce anything.
INBOUND_MAX_PACKET = CALL_MAX_BYTES + 64 * 1024

# A packet over the broker's maximum makes the broker close the connection, and paho replays the queued
# QoS 1 message on every automatic reconnect: one oversized document loops the bridge and stops everything
# else, a new client (Reconnect) being the only way out.  1 MiB is what EMQX and HiveMQ accept by default
# (mosquitto is far more generous); the maximum a broker announces wins over it.  Only MQTT 5 announces one,
# which is why every client speaks MQTT 5 and drops to 3.1.1 only for a broker that refuses it.
MANAGER_RESULT_WAIT_S = 5  # the executor may be wedged: the action must not wait on the broker forever
STOP_JOIN_S = 5  # how long stopping a client waits for its network thread before closing the socket under it
# A retained scan holds everything the broker sends under the scanned prefix in memory at once, and what sits there
# is not this process's to choose: a broker with a very large retained store (or one publishing large retained
# payloads under it) would otherwise grow this container for as long as the scan's time budget lasts.  Past this,
# further topics are left unread - the same outcome a scan cut short by _collect_quiet's maximum already has, and a
# sweep that reads less removes less, never the wrong thing.  Our own documents are a few KB each.
RETAINED_SCAN_MAX_BYTES = 64 * 1024 * 1024
PUBLISH_MAX_BYTES = 1024 * 1024
# paho 2.1 ignores the receive maximum an MQTT 5 broker announces and keeps up to this many QoS 1 messages
# unacknowledged; a broker announcing less (HiveMQ: 10) may close the connection over it
PAHO_INFLIGHT = 20
PUBLISH_OVERHEAD_BYTES = 32  # fixed header, topic length, packet id and properties, on top of topic + payload
# Topic segments of our own under the base topic: an integration with one of these names gets its documents under
# "<name>-integration" ("-" is never part of an integration domain), otherwise an integration called "call" would
# publish its documents where live subscribers take them as service calls.
RESERVED_TOPIC_SEGMENTS = frozenset({"call", "cmd", "result", "services", "manager", "health", "status"})
# Names of secrets, shared with the diagnostics masker (the zip, the Logs and Log files pages), so what the command
# history, the status and the log hide is hidden there too.  These end a longer name as they are (access_token,
# old_password, wifi_psk, user_credentials) ...
# the shared credential list (logbuffer), which the request-line, dict and text rules of diagnostics use too: a list
# of its own here had drifted (pass, pw and bearer were masked in a log line and printed in the command history).
# Here a name must END in one of these, so "credentials" is spelled out: "credential" anywhere covers it elsewhere.
SECRET_NAME_ENDINGS = tuple(dict.fromkeys((*logbuffer.CREDENTIAL_NAMES, "credentials")))
# ... and these only as a word of their own (pin, user_pin, otp, basic_auth, db_pw; not spin, author, oauth or passed)
SECRET_NAME_WORDS = logbuffer.CREDENTIAL_WORDS
# A key names a secret when it ends in one of these: code, key and the words above as a word of their own (user_code,
# api_key, not zipcode or hotkey), the endings anywhere.  Not: translation/sort/primary_key.
_SECRET_NAME = (r"(?!(?:translation|sort|primary)_key\b)"
                r"(?:(?:[A-Za-z0-9_-]*[_-])?(?:code|key|" + "|".join(SECRET_NAME_WORDS) + ")"
                r"|[A-Za-z0-9_-]*(?:" + "|".join(SECRET_NAME_ENDINGS) + "))")
# The text rule runs on paho's network thread, over text anyone who may publish under the base topic writes: it must
# stay linear whatever that text is.  A quoted value ends at its closing quote; an escaped quote inside it (\" or \')
# does not end it.  A key inside a JSON string (a service value that is itself JSON) has its quotes escaped
# (\"code\": \"1234\"): the match starts at the name, after however many backslashes, and that value ends at the same run
# of backslashes and quote it opened with.  A longer run is a quote escaped inside it (\\\"); a shorter one, or that
# quote alone, ends the string around it.  A value never closed (a text the history cut) is masked to the end.  Every repeat
# is possessive and its branches start on different characters, so the match never fails and nothing is read twice
# (the lookaheads read at most the run they stand at).  One exception: an unquoted value that is an HTTP auth scheme
# ("Authorization: Bearer <token>", "token: Basic <credentials>") takes the credential after it, which a value ending at
# the first space would print; when no credential follows, the scheme word and its spaces are read a second time as a
# plain value, once per name: still linear.
_AUTH_SCHEMES = ("bearer", "basic", "digest", "token", "negotiate", "ntlm", "hoba", "mutual")
_CODE_VALUE = re.compile(
    r"""((?<![A-Za-z0-9_-])""" + _SECRET_NAME + r"""(?:\\*+["'])?\s*+[:=]\s*+)"""
    r"""(?:(\\++)(["'])(?:[^\\"']++|(?!\3)["']|\\++(?!\3)|(?!\2\3)(?=\2)\\++\3)*+(?:\2\3)?"""
    r"""|"(?:[^"\\]++|\\[\s\S])*+"?|'(?:[^'\\]++|\\[\s\S])*+'?"""
    r"""|(?:""" + "|".join(_AUTH_SCHEMES) + r""")[ \t]++[^,}\s]++|[^,}\s]++)""",
    re.IGNORECASE)
_CODE_VALUE_CUT = _CODE_VALUE  # the rule for a text cut to MASK_SCAN_CHARS: a value the cut left open is one never closed
# what the text rule reads of a history row, a status line or a log line (shown cut to a few hundred characters)
MASK_SCAN_CHARS = 4096
_SECRET_KEY = re.compile(_SECRET_NAME, re.IGNORECASE)
_JSON_STRING = re.compile(r'"(?:[^"\\]+|\\.)*"?')
_JSON_BRACKET = re.compile(r"[\[\]{}]")
# service data fields that name entities besides the target (media_player.join, scene.apply/create, group.set, ...)
_ENTITY_LIST_KEYS = frozenset({"group_members", "snapshot_entities", "entities", "add_entities", "remove_entities"})


def password_text(hass: HomeAssistant, entity_id: str) -> bool:
    """A text entity in password mode, read like discovery reads it: its state attributes, else its registry
    capabilities (an entity without a state yet)."""
    state = hass.states.get(entity_id)
    if state is not None:
        attrs = state.attributes
    else:
        entry = er.async_get(hass).async_get(entity_id)
        attrs = (entry.capabilities or {}) if entry is not None else {}
    return attrs.get("mode") == "password"


def password_value(hass: HomeAssistant, domain: str, service: str, data: dict[str, Any],
                   reachable: Callable[[], Iterable[str]]) -> str | None:
    """The value of a text.set_value call that may reach a text entity in password mode: one named in entity_id, or,
    for a target by area, device, floor or label, any entity of `reachable` in that mode.  The MQTT calls and the
    Services page mask it with this, so what is sent to such an entity never shows in a history or a log."""
    value = data.get("value")
    if (domain, service) != ("text", "set_value") or value is None or value == "":
        return None
    value = value if isinstance(value, str) else str(value)  # text's schema turns a number into the text it quotes
    ids = data.get("entity_id")
    named = [ids] if isinstance(ids, str) else ids if isinstance(ids, list) else []
    candidates = {p.strip().lower() for x in named if isinstance(x, str) for p in x.split(",")}
    if any(k in data for k in ("area_id", "device_id", "floor_id", "label_id")):
        candidates |= {e for e in reachable() if e.startswith("text.")}
    return value if any(password_text(hass, e) for e in candidates if e.startswith("text.")) else None


def _mask_codes(text: str, limit: int | None = None) -> str:
    """Alarm and lock codes, PINs, passwords and tokens stay out of the command history, the status and the log.
    JSON is masked on its parsed keys, which the text rule cannot see when they are written with escapes
    ("\\u0063ode"); the text rule then covers what is not JSON and secrets written inside string values.
    With a limit, the text rule reads no more than that many characters: what comes back is masked and cut there."""
    if _payload_problem(text) is None and text.lstrip()[:1] in ("{", "["):
        try:
            masked, changed = _masked(json.loads(text), limit)
        except (ValueError, RecursionError):
            pass
        else:
            if changed:
                text = json.dumps(masked, ensure_ascii=False)
    return _mask_text(text, limit)


def _mask_text(text: str, limit: int | None = None) -> str:
    """The text rule of _mask_codes alone: for text whose JSON keys are masked already, or that does not parse."""
    rule = _CODE_VALUE
    if limit is not None and len(text) > limit:
        text, rule = text[:limit], _CODE_VALUE_CUT
    return rule.sub(lambda m: m.group(1) + (f'{m.group(2)}{m.group(3)}***{m.group(2)}{m.group(3)}' if m.group(2) else '"***"'), text)


def _masked(value: Any, limit: int | None = None) -> tuple[Any, bool]:
    """(the parsed value with the value of every secret key masked, whether any was)"""
    if isinstance(value, dict):
        out, changed = {}, False
        for key, item in value.items():
            if _SECRET_KEY.fullmatch(key):
                out[key], changed = "***", True
            else:
                out[key], sub = _masked(item, limit)
                changed = changed or sub
        return out, changed
    if isinstance(value, list):
        items = [_masked(item, limit) for item in value]
        return [item for item, _ in items], any(sub for _, sub in items)
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        masked = _mask_codes(value, limit)  # a JSON document sent as a string value: its keys are keys too
        return masked, masked != value
    return value, False


def _payload_problem(payload: str) -> str | None:
    """Why a call payload is not parsed at all: too large, or nested deeper than CALL_MAX_DEPTH (brackets inside strings don't count)."""
    if len(payload) > CALL_MAX_BYTES or len(payload.encode(errors="replace")) > CALL_MAX_BYTES:
        return f"larger than {CALL_MAX_BYTES // 1024} KB"
    depth = 0
    for m in _JSON_BRACKET.finditer(_JSON_STRING.sub("", payload)):
        depth += 1 if m.group() in "[{" else -1
        if depth > CALL_MAX_DEPTH:
            return f"nested deeper than {CALL_MAX_DEPTH} levels"
    return None


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a number a service accepts")
    return value


def _loads_call(payload: str) -> Any:
    if problem := _payload_problem(payload):
        raise ValueError(problem)
    return json.loads(payload, parse_constant=_no_constant, parse_float=_finite_float)


_ID_TOKEN = re.compile(r'"(?:[^"\\]++|\\[\s\S])*+"?|[{}\[\]:]')
_ID_SCALAR = re.compile(r'\s*+(?:("(?:[^"\\]++|\\[\s\S])*+")|(-?\d++(?:\.\d++)?(?:[eE][+-]?\d++)?)|(true|false|null))')


def _refused_call_id(payload: str) -> Any:
    """The _id of a call payload that is refused without being parsed, or that does not parse (too large, too deep,
    NaN, a number too large, broken JSON), so the answer still carries it: the value of the last "_id" key of the outer
    object when that is a string, a finite number, true, false or null, else None.  Read, never parsed: one pass over at
    most CALL_MAX_BYTES characters with possessive repeats, linear whatever the payload holds (paho's thread)."""
    text = payload[:CALL_MAX_BYTES]
    if not text.lstrip().startswith("{") or ("_id" not in text and "\\u" not in text):
        return None
    depth, prev, found = 0, None, None
    for m in _ID_TOKEN.finditer(text):
        tok = m.group()
        if tok in ("{", "["):
            depth += 1
        elif tok in ("}", "]"):
            depth -= 1
            if depth <= 0:
                break  # the outer object ended
        elif tok == ":":
            if depth == 1 and prev is not None and (prev == '"_id"' or prev[0] == '"' and "\\" in prev and _json_string(prev) == "_id"):
                if v := _ID_SCALAR.match(text, m.end()):
                    found = _scalar_id(v)
        prev = tok
    return found


def _json_string(token: str) -> str | None:
    try:
        return json.loads(token)
    except ValueError:
        return None


def _scalar_id(match: re.Match[str]) -> Any:
    string, number, word = match.groups()
    try:
        if string is not None:
            return json.loads(string)
        if number is not None:
            return _finite_float(number) if any(c in number for c in ".eE") else int(number)
    except ValueError:  # an invalid escape, 1e999, more digits than int() reads
        return None
    return {"true": True, "false": False}.get(word)


def _entity_ids_in(value: Any) -> set[str]:
    """Entity ids named in service data outside the target: values of keys ending in entity_id/entity_ids, and of
    group_members, snapshot_entities, entities, add_entities and remove_entities (a list, a comma-separated string, or a mapping keyed by entity id),
    at any depth.  Only what looks like an entity id counts; ids in other fields are not recognised."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).lower()
            if name.endswith(("entity_id", "entity_ids")) or name in _ENTITY_LIST_KEYS:
                names = list(item) if isinstance(item, dict) else [item] if isinstance(item, str) else item if isinstance(item, list) else []
                found.update(p.strip().lower() for x in names if isinstance(x, str) for p in x.split(","))
            found |= _entity_ids_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= _entity_ids_in(item)
    return {e for e in found if valid_entity_id(e)}


def _call_key(domain: str, service: str, call_id: Any) -> str:
    """The dedup key keeps the type of the id: 1 and "1" are two different calls."""
    return f"{domain}.{service}:{json.dumps(call_id, sort_keys=True, default=str)}"


def _scrubbed(text: str) -> str:
    """A service's own exception text, masked the way a log line is.

    `recent_commands` masks what the UI and the API show, and the Logs page and the diagnostics zip
    scrub what they display - but the record that reaches the container's own stdout goes through none
    of them, and `docker logs` output is what ends up in an issue.  Deferred import: diagnostics imports
    this module.
    """
    try:
        from .diagnostics import scrub_text

        return scrub_text(text)
    except Exception:  # noqa: BLE001 - a log line must never be the reason something fails
        return text


def _call_id_problem(call_id: Any) -> str | None:
    """Why this _id is refused, None when it fits.  It is held for DEDUP_WINDOW_S in the dedup map, kept in the
    command history and echoed in every result and every /api/mqtt/commands poll, so its size is capped once,
    here, rather than truncated differently in each of the three."""
    if call_id is None:
        return None
    size = len(json.dumps(call_id, default=str, ensure_ascii=False).encode("utf-8", "replace"))
    if size > CALL_ID_MAX_BYTES:
        return f"_id is {size} bytes: at most {CALL_ID_MAX_BYTES} are accepted"
    return None


def _short_call_id(call_id: Any) -> Any:
    """An oversized _id cut down to something that can be answered with and logged: enough for the sender to
    recognise its own call, never enough to be worth storing."""
    text = call_id if isinstance(call_id, str) else json.dumps(call_id, default=str, ensure_ascii=False)
    return text[:CALL_ID_MAX_BYTES // 2] + "…"


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number a service accepts")


def _default_host() -> str:
    """The broker a fresh install offers: the official broker app's name when running as a Home Assistant app
    (entrypoint.py apply_app_options sets HRI_APP), the compose service name otherwise.  A default only: a host
    in mqtt.json always wins."""
    return "core-mosquitto" if os.environ.get("HRI_APP") else "mosquitto"


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = field(default_factory=_default_host)
    port: int = 1883
    username: str = ""
    password: str = ""
    force_base_topic: bool = False  # connect even if foreign retained data sits under the base topic
    republish_interval_s: int = 300          # incremental pass: only documents whose content changed
    full_republish_interval_min: int = 60   # everything, no matter what (retained docs re-asserted)
    qos: int = 0
    exclude_integrations: list[str] = field(default_factory=lambda: ["integration_manager"])
    # HA MQTT discovery for the consuming Home Assistant (device-based format).
    # Off by default on purpose: turn it on only once the integration is
    # fully configured here, otherwise the consumer mirrors half-done state
    # (entities you are still renaming or deleting stay there as zombies).
    discovery_enabled: bool = False
    discovery_prefix: str = "homeassistant"
    # The Home Assistant version that consumes this discovery, e.g. "2026.4".
    # Empty (the default) means "assume it is current": nothing is filtered and
    # the payload is exactly what it always was.  Set, it makes discovery leave
    # out what that version cannot parse - a platform or a device class it does
    # not know fails its validation and costs the WHOLE device its entities
    # there.  Discovery is one-way and the birth message carries no version, so
    # the container cannot find this out: the operator declares it.
    main_ha_version: str = ""
    # The manager as a device on the consuming HA (health, updates, resources)
    # even while entity discovery is off, e.g. in shadow mode; manager_commands
    # lets that HA install updates, restart and back up through it.
    manager_discovery: bool = False
    manager_commands: bool = False
    # TLS to the broker (usually port 8883): ca_certs is a CA file under the config directory, empty = the system CAs;
    # tls_insecure skips the check that the certificate names the host (the certificate is still verified)
    tls: bool = False
    ca_certs: str = ""
    tls_insecure: bool = False


# Every numeric setting, with the range it is usable in.  An interval has no upper bound of its own,
# and a big enough one makes timedelta(seconds=...) raise OverflowError while the republish timer is
# armed - that happens in async_start(), before the views that could correct the value are registered,
# so the manager would never come up again once such a value reached mqtt.json.
INT_BOUNDS = {
    "port": (1, 65535),
    "qos": (0, 2),
    "republish_interval_s": (30, 86400),        # at most a day between incremental passes
    "full_republish_interval_min": (5, 10080),  # at most a week between full ones
}


def _bounded(name: str, value: Any) -> int:
    """The setting as an int inside its range; raises for anything that is not a number - including an
    infinity, which json.load happily reads from ``1e999`` or ``Infinity`` and int() then refuses with
    OverflowError rather than ValueError, so every caller has to catch that one too."""
    low, high = INT_BOUNDS[name]
    return min(high, max(low, int(value)))


def _notification_count(hass: HomeAssistant) -> int:
    """Persistent notifications the integration raised (a headless HA shows
    them nowhere else): part of the health document for the parent."""
    try:
        from homeassistant.components import persistent_notification as pn

        return len(pn._async_get_or_create_notifications(hass))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return 0


def platform_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """The integration an entity belongs to: its registry entry, or, for an
    entity without a unique_id (never in the registry, typical of YAML
    platforms), the entity platform that added it."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        return entry.platform
    # keyed by integration name, not by entity domain: look through all of them
    for platforms in (hass.data.get(DATA_ENTITY_PLATFORM) or {}).values():
        for platform in platforms:
            if entity_id in platform.entities:
                return platform.platform_name
    return None


# A camera, image or media player carries the access token of this container's proxy (/api/camera_proxy/...?token=)
# in access_token and in its picture URLs: a credential for this container, and on a camera a new value every five
# minutes, which would rewrite the retained document each time.  Neither is published, at any depth: an attribute
# key access_token and a string carrying such a URL are left out of the dict or list that holds them.  The state
# cannot be left out, so a token in it is masked instead.
_TOKEN_URL = re.compile(r"[?&](?:access_)?token=", re.IGNORECASE)
_TOKEN_VALUE = re.compile(r"([?&](?:access_)?token=)[^&#\s]*", re.IGNORECASE)
_LEFT_OUT = object()


def _without_tokens(value: Any) -> Any:
    """value with every token-carrying string and access_token key taken out (_LEFT_OUT for such a string itself);
    the very same object when there is none, so an entity without a token costs one walk and no copy."""
    if isinstance(value, str):
        return _LEFT_OUT if _TOKEN_URL.search(value) else value
    if isinstance(value, dict):
        out, changed = {}, False
        for k, v in value.items():
            kept = _LEFT_OUT if k == "access_token" else _without_tokens(v)
            if kept is _LEFT_OUT:
                changed = True
            else:
                out[k] = kept
                changed = changed or kept is not v
        return out if changed else value
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_without_tokens(v) for v in value]
        if all(a is b for a, b in zip(items, value)):
            return value
        return [v for v in items if v is not _LEFT_OUT]
    return value


def _published_attributes(attributes: Any) -> dict[str, Any]:
    kept = _without_tokens(attributes)
    return dict(kept) if kept is attributes else kept


def _published_state(state: str) -> str:
    return _TOKEN_VALUE.sub(r"\1***", state) if _TOKEN_URL.search(state) else state


def _comp_key(entity_id: str) -> str:
    return entity_id.replace(".", "_", 1)


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def _clean_json(value: Any) -> Any:
    """JSON-safe copy: keys as strings (json.dumps fails on mixed or tuple
    keys), NaN/Infinity as null (json.dumps would write invalid JSON),
    sets and tuples as lists, everything else through _json_default."""
    if isinstance(value, dict):
        return {str(k): _clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean_json(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return _clean_json(_json_default(value))


def _dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(_clean_json(value), allow_nan=False, **kwargs)


_SERVICE_NAME = re.compile(r"[a-z0-9_]+")

# how Home Assistant registers an entity service (helpers/service.py): the handler is a partial of one of these
_ENTITY_SERVICE_CALLS = frozenset({"entity_service_call", "batched_entity_service_call"})


class MqttPublisher:
    _stopping = False  # Home Assistant is stopping: no client may be created any more
    _identity_sweep_due = False  # the sweep of an identity that changed while disconnected failed: retried after a connect
    # the last CONNACK was a refusal: paho follows it with a disconnection ("Unspecified error"), which must not replace the reason
    _refused = False
    # manager device discovery topics announced to the main HA (kept on disk), None before a record exists (a new install, or
    # one upgraded from a version that kept none).  Replaced as a whole, never changed in place
    _manager_announced: frozenset[str] | None = None
    # (base topic, host, port, tls, username) -> {base, prefix, broker, error, since}: uninstalled identities whose retained
    # data that broker did not take, retried by a timer (no identity is needed) while the settings name that broker, and
    # never sent to another one.  Replaced as a whole, never changed in place, under the lock
    _cleanup_pending: dict[tuple, dict[str, Any]] = {}
    _cleanup_pending_lock = threading.Lock()
    _cleanup_retrying = False
    _in_flight = 0  # service calls and commands whose service task has not finished
    # replaced by a dict of its own in __init__ and in _note_cleared, which never mutates this one in place
    _cleared_cmds: dict[str, float] = {}
    _connected_at = 0.0  # monotonic time of the last CONNACK: a drop right after one is not a TLS problem
    _broker_max_packet = 0  # maximum packet size the broker announced (MQTT 5 only), 0 = none announced
    # what the broker at _learned_for taught this process: it refused MQTT 5, it announced a receive maximum below
    # paho's window.  Forgotten when the host, port or TLS setting changes: that may be another broker.
    _learned_for = ""
    _mqtt311 = False
    _receive_max = 0
    # the session the last CONNACK opened speaks MQTT 5: its subscription carries noLocal, so nothing this
    # process publishes comes back to it.  False until one says otherwise (the safe side: 3.1.1 echoes)
    _session_v5 = False
    _health_since: str | None = None  # when the verdict last published first took that value
    # _calls is iterated and changed on paho's thread and changed on the loop (a call refused for the in-flight cap)
    _calls_lock = threading.Lock()
    _subscribing: list[Any] | None = None  # [client, mid, topics, online announced] of the SUBSCRIBE waiting for its SUBACK
    _subscribing_lock = threading.Lock()  # the SUBACK (paho thread) and its overdue timer (loop) race for it
    _last_subscribe_error = ""  # the refusal already logged: repeated only after a subscription succeeded

    def __init__(self, hass: HomeAssistant, key_provider=None, health_provider=None, rules_provider=None) -> None:
        self._health_provider = health_provider
        # settings.health_for(domain): stale seconds, mode, unavailable share
        self._rules_provider = rules_provider or (lambda domain: {"stale_s": HEALTH_STALE_S, "mode": "periodic", "unavailable_pct": 50,
                                                                   "stale_basis": "reported"})
        self._pending_clears: set[str] = set()  # topics we could not clear while disconnected
        self._blocks: dict[str, dict[str, Any]] = {}  # discovery_id -> last published device block
        self._probed_ok: set[str] = set()  # "host:port/base" namespaces probed clean by this process
        self._tls_checked_at, self._tls_error = 0.0, ""  # the last diagnostic handshake after a failed connect
        self._last_disconnect = ""  # the reason already logged: paho retries forever
        self._oversized_warned: set[str] = set()  # topics already reported as too big, forgotten on every connect
        self._last_hash: dict[str, str] = {}  # topic -> content hash of the last published document (minus timestamps)
        self._last_full = 0.0
        self._moving = False  # identity move in progress: nothing may be published under the old names
        self.rules = MqttRules(hass.config.path("integration_manager", "mqtt_rules.json"))
        self.rules.components = self._rule_components
        self._republish_unsub = None
        self._health_last: dict[str, Any] = {}
        self._started_at = time.time()
        self.hass = hass
        self.path = hass.config.path(CONFIG_FILE)
        self.config = self._load()
        # instance identity (hass_<active domain>) comes from the installer
        self._key_provider = key_provider or (lambda: "hass_remote")
        self._live_base: str | None = None      # base topic the current connection uses
        self._live_prefix: str | None = None
        self._client: mqtt.Client | None = None
        self._last_refusal = ""  # the last CONNACK refusal logged: paho retries for ever, the reason rarely changes
        self._connected = False
        self._lock = threading.Lock()
        self._conn_lock = asyncio.Lock()  # reconnects never overlap: two paho clients with one client id kick each other off forever
        self.stats: dict[str, Any] = {
            "connected": False,
            "connect_error": "",
            "subscribe_error": "",  # the broker refused (or never acknowledged) the command subscriptions: set while connected
            "protocol": None,  # "MQTT 5", or "MQTT 3.1.1" for a broker that refused 5: only 5 announces a maximum packet size
            "published": 0,
            "cleared": 0,
            "oversized_skipped": 0,
            "last_oversized": None,
            "last_publish": None,
            "last_full_republish": None,
            "health_state": None,
            "unchanged_skipped": 0,
            "last_incremental_republish": None,
            "entities_last_incremental": 0,
            "health_published": None,
            "entities_last_run": 0,
            "discovery_devices": 0,
            "discovery_components": 0,
            "discovery_mirrored": 0,
            "discovery_disabled": 0,
            "discovery_collisions": 0,
            "discovery_default_id_duplicates": 0,
            # left out because main_ha_version is older than what the entity carries
            "discovery_compat_device_classes_dropped": 0,
            "discovery_compat_platforms_mirrored": 0,
            "services_published": 0,
            "commands": 0,
            "last_command": None,
            "calls": 0,
            "last_call": None,
        }
        self._compat_warned: set[str] = set()  # each device class / platform left out for main_ha_version is logged once
        # discovery_id -> {entity_id: component}; what we last published per device
        self._discovery_map: dict[str, dict[str, dict[str, Any]]] = {}
        self._services_published: set[str] = set()
        self._services_timer: asyncio.TimerHandle | None = None
        # newest last: {id, kind, what, data, received, finished, duration_ms, state, error, result}
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=HISTORY_MAX)
        # idempotency: _id -> the canonical call record (state, result) for DEDUP_WINDOW_S,
        # independent of the visual history (which commands can push out)
        self._calls: dict[str, dict[str, Any]] = {}
        # command topics whose retained payload this process just cleared -> when (paho's thread only).  An MQTT
        # 3.1.1 session has no noLocal, so the broker sends that clear straight back as a live empty payload.
        self._cleared_cmds: dict[str, float] = {}
        self._range_pending: dict[str, dict[str, Any]] = {}  # entity_id -> the first half of a range change, waiting for the second
        self._default_id_warned: set[str] = set()  # entities whose default_entity_id another entity asked for first, warned once each
        self._collision_warned: set[str] = set()  # entities skipped for a component key clash, warned once each
        self._registry_timer: asyncio.TimerHandle | None = None
        # entity_id -> document topic last published (the registry entry is
        # already gone when the remove event fires, so recompute is wrong)
        self._topics: dict[str, str] = {}
        self.manager = None  # ManagerDevice (manager_device.py), set by __init__
        self._manager_absent_sent = False  # this connection already told the consumer there is no manager device
        self._resync_excluded = False  # integrations were excluded while disconnected: sweep the broker at the next connect
        self._undiscover_due = False  # discovery was turned off: remove the announced entities at the next full republish
        self._last_event: dict[str, str] = {}  # event entity -> the occurrence (state = its time) last emitted or seen
        self._saved: dict[str, Any] | None = None  # the mqtt.json this process last queued: the base of the next save
        # once per process, after HA started: what this process never published (so the in-memory discovery
        # map cannot compute removal forms for it) but is still retained, e.g. entities a restore took away
        self._orphan_sweep_due = True
        # discovery_id -> {component key: component} announced by an earlier process, read before this one
        # publishes: a new process knows only the entities it has, and its first config would silently drop
        # the others from the retained config, leaving the orphan sweep nothing to send removal forms for
        self._boot_components: dict[str, dict[str, dict[str, Any]]] | None = None
        # (discovery_id, component key) this process sent the removal form for (a rename, a delete, an exclusion) while
        # the sweep is due: never carried again, or the next config of that device brings the entity back on the consumer
        self._boot_removed: set[tuple[str, str]] = set()
        self._health_soon_handle: asyncio.TimerHandle | None = None
        self._health_announced: str | None = None  # the verdict last published (and put in the timeline)

    # ----- identity --------------------------------------------------------

    @property
    def base_topic(self) -> str:
        return self._live_base or self._key_provider() or "hass_none"

    @property
    def wanted_base_topic(self) -> str | None:
        return self._key_provider()

    @property
    def prefix(self) -> str:
        return self._live_prefix or ((self._key_provider() or "hass_none") + "_")

    @property
    def client_id(self) -> str:
        return self.base_topic

    # ----- config ----------------------------------------------------------

    def _load(self) -> MqttConfig:
        """Blocking.  No file is a fresh volume: the defaults, silently.  A file that cannot be read or is not a JSON
        object (a hand edit, a damaged volume) gives the defaults too, with a warning in the log and on the timeline:
        the manager must come up to let the settings be saved again."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return MqttConfig()
        except (OSError, ValueError) as err:
            return self._unusable_config(f"cannot be read ({type(err).__name__}: {err})")
        if not isinstance(data, dict):
            return self._unusable_config(f"is not a JSON object ({type(data).__name__})")
        known = {k: v for k, v in data.items() if k in MqttConfig.__dataclass_fields__}
        return MqttConfig(**self._sane(known))

    def _unusable_config(self, why: str) -> MqttConfig:
        message = f"{CONFIG_FILE} {why}: MQTT uses the default settings (disabled) until they are saved again"
        _LOGGER.warning("MQTT: %s", message)
        events.emit("mqtt", message)
        return MqttConfig()

    def _sane(self, data: dict[str, Any]) -> dict[str, Any]:
        """A numeric setting on disk that is out of range (or not a number at all) falls back to its
        default, warned: it was written by an older version or by hand, and the boot must not die on it.
        So does a switch that is not true/false, a text that is not a string and an exclusion list that is
        not a list of domains."""
        out = dict(data)
        for name, spec in MqttConfig.__dataclass_fields__.items():
            if name not in out or name in INT_BOUNDS:
                continue
            value = out[name]
            if spec.type in ("bool", bool):
                usable = isinstance(value, bool)
            elif name == "exclude_integrations":
                usable = isinstance(value, list) and all(isinstance(x, str) for x in value)
            else:
                usable = isinstance(value, str)
            if not usable:
                default = spec.default_factory() if spec.default_factory is not MISSING else spec.default
                # the type only: the value may be the password
                _LOGGER.warning("MQTT: %s in %s is not usable (%s): using the default", name, self.path, type(value).__name__)
                out[name] = default
        for name in INT_BOUNDS:
            if name not in out:
                continue
            try:
                value = _bounded(name, out[name])
                # an int only: 1.0 and true compare equal to 1, and paho takes neither (qos << 1 raises for every
                # publish, getaddrinfo refuses a float port) while the setting looked accepted
                if value == out[name] and type(out[name]) is int:
                    continue
            except (TypeError, ValueError, OverflowError):
                pass
            default = MqttConfig.__dataclass_fields__[name].default
            _LOGGER.warning("MQTT: %s=%r in %s is not usable: using %s", name, out[name], self.path, default)
            out[name] = default
        if out.get("main_ha_version") and disc.parse_ha_version(out["main_ha_version"]) is None:
            _LOGGER.warning("MQTT: main_ha_version=%r in %s is not a Home Assistant version: assuming the main "
                            "Home Assistant is current (nothing is left out of discovery)", out["main_ha_version"], self.path)
            out["main_ha_version"] = ""
        if out.get("ca_certs"):
            # a restored or hand-edited file is held to the directory a save allows (a file missing for now stays:
            # the connection names it, and the next save must not drop the setting); without it a private broker
            # certificate no longer verifies, which fails the connection instead of trusting anything
            try:
                out["ca_certs"] = self._ca_certs_path(str(out["ca_certs"]).strip(), must_exist=False)
            except ValueError as err:
                _LOGGER.warning("MQTT: %s in %s: using the system CAs", err, self.path)
                out["ca_certs"] = ""
        return out

    async def async_save(self, updates: dict[str, Any]) -> MqttConfig:
        """Validate types strictly: a null/NaN from the form would be stored
        and crash async_start() on the next boot, before the UI exists.
        Validated on the loop (two saves, the MQTT form and a cutover, see each
        other in order), written by the ordered writer; a write error reaches
        the caller.  Written to disk only: async_reconnect() adopts it, so it
        can still compare the old topics against the new ones and clear them."""
        new = asdict(self._validated(updates))
        self._saved = new
        try:
            await writer.async_write(self.path, new, mode=0o600)
        except BaseException:
            if self._saved is new:
                self._saved = None  # not on disk: the file is the base again
            raise
        return MqttConfig(**new)

    def _validated(self, updates: dict[str, Any]) -> MqttConfig:
        """Starts from the last save this process queued, else from what is on
        disk: a save not adopted yet (waiting for a reconnect) must not be
        undone by the next one."""
        current = asdict(self.config)
        if self._saved is not None:
            current.update(self._saved)
        else:
            try:  # once per process: a few hundred bytes, on the loop
                with open(self.path, encoding="utf-8") as fh:
                    on_disk = json.load(fh)
                if isinstance(on_disk, dict):
                    current.update(self._sane({k: v for k, v in on_disk.items() if k in current}))
            except (OSError, ValueError):
                pass  # no file yet (or unreadable): the running config is the base
        for k, v in updates.items():
            if k not in current or k in ("base_topic", "client_id"):
                continue  # derived from the running integration, never stored from the UI
            if k == "password" and v == "":
                continue  # blank in the UI means "keep"
            if k in ("enabled", "discovery_enabled", "force_base_topic", "manager_discovery", "manager_commands", "tls", "tls_insecure"):
                if not isinstance(v, bool):
                    raise ValueError(f"{k} must be true or false")
            elif k == "ca_certs":
                if not isinstance(v, str):
                    raise ValueError("ca_certs must be a string")
                v = self._ca_certs_path(v.strip())
            elif k == "main_ha_version":
                if not isinstance(v, str):
                    raise ValueError("main_ha_version must be a string")
                v = v.strip()
                if v and disc.parse_ha_version(v) is None:
                    raise ValueError("main_ha_version must be a Home Assistant version like 2026.8, or empty")
            elif k in INT_BOUNDS:
                try:
                    v = int(v)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError(f"{k} must be an integer") from None
                if k == "port" and not 1 <= v <= 65535:
                    raise ValueError("port out of range")
                if k == "qos" and v not in (0, 1, 2):
                    raise ValueError("qos must be 0, 1 or 2")
                # an interval has no wrong value, only an unusable one: clamped, the way the
                # minimum has always been, so the form can never store one the timer cannot use
                v = _bounded(k, v)
            elif k == "exclude_integrations":
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise ValueError("exclude_integrations must be a list of domains")
            elif not isinstance(v, str):
                raise ValueError(f"{k} must be a string")
            elif k in ("base_topic", "discovery_prefix", "client_id", "host") and not v.strip():
                raise ValueError(f"{k} must not be empty")
            elif k in ("base_topic", "discovery_prefix") and any(ch in v for ch in "+#"):
                raise ValueError(f"{k} must not contain MQTT wildcards")
            if k in ("host", "discovery_prefix"):
                v = v.strip()
            if k == "discovery_prefix" and (base := self.wanted_base_topic) and (v == base or v.startswith(base + "/")):
                # the connect would find the configs there and refuse the base topic as carrying foreign data
                raise ValueError(f"discovery_prefix must not be the base topic {base} or under it")
            current[k] = v
        return MqttConfig(**current)

    def _ca_certs_path(self, value: str, must_exist: bool = True) -> str:
        """"" (the system CAs) or the absolute path of an existing file inside the config directory."""
        if not value:
            return ""
        config_dir = self.hass.config.config_dir
        path = os.path.normpath(value if os.path.isabs(value) else os.path.join(config_dir, value))
        root = os.path.realpath(config_dir)
        real = os.path.realpath(path)
        if os.path.commonpath([root, real]) != root:
            raise ValueError(f"ca_certs must be a file under {config_dir}")
        if must_exist and not os.path.isfile(real):
            raise ValueError(f"ca_certs: {path} is not a file")
        return path

    def _broker_traits(self) -> tuple[bool, int]:
        """(refused MQTT 5, receive maximum to keep to) for the configured broker."""
        key = f"{self.config.host}:{self.config.port}/{self.config.tls}"
        if key != self._learned_for:
            self._learned_for, self._mqtt311, self._receive_max = key, False, 0
        return self._mqtt311, self._receive_max

    def _learned_mqtt311(self, client: mqtt.Client, reason_code: Any) -> bool:
        """A refused MQTT 5 connection: what a 3.1.1 broker answers (CONNACK code 1) to a protocol it does not speak.
        Recorded, so the next client of this broker speaks 3.1.1."""
        if getattr(client, "protocol", None) != mqtt.MQTTv5 or str(reason_code) != "Unsupported protocol version":
            return False
        self._broker_traits()
        if not self._mqtt311:
            self._mqtt311 = True
            where = f"{self.config.host}:{self.config.port}"
            _LOGGER.warning("MQTT: %s refused MQTT 5: using MQTT 3.1.1, which cannot announce a maximum packet size", where)
            events.emit("mqtt", f"{where} refused MQTT 5: using MQTT 3.1.1")
        return True

    def _learned_receive_max(self, client: mqtt.Client, properties: Any) -> bool:
        """The broker announced a receive maximum below the client's window: recorded, the client must be replaced
        (paho cannot shrink the window of an open connection)."""
        announced = getattr(properties, "ReceiveMaximum", None)
        if getattr(client, "protocol", None) != mqtt.MQTTv5 or not isinstance(announced, int) or announced < 1:
            return False
        if announced >= client.max_inflight_messages:
            return False
        self._broker_traits()
        self._receive_max = announced
        _LOGGER.info("MQTT: the broker accepts %s unacknowledged messages at a time: reconnecting with that window", announced)
        return True

    @staticmethod
    def _connect_options(client: mqtt.Client, inbound_max: int = 0) -> dict[str, Any]:
        """inbound_max: the Maximum Packet Size the client announces (MQTT 5 only), 0 for none."""
        if getattr(client, "protocol", None) != mqtt.MQTTv5:
            return {}
        # MQTT 5 has no clean_session; clean_start on every connect (paho's default is the first only) is the same thing
        options: dict[str, Any] = {"clean_start": True}
        if inbound_max:
            props = Properties(PacketTypes.CONNECT)
            props.MaximumPacketSize = inbound_max
            options["properties"] = props  # paho sends it again on every automatic reconnect
        return options

    def _new_client(self, client_id: str) -> mqtt.Client:
        """A paho client with the configured credentials and TLS: the connection, and every scan, probe and cleanup."""
        mqtt311, receive_max = self._broker_traits()
        if mqtt311:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311, clean_session=True)
        else:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv5)
            c.max_inflight_messages_set(min(receive_max or PAHO_INFLIGHT, PAHO_INFLIGHT))
        if self.config.username:
            c.username_pw_set(self.config.username, self.config.password or None)
        if self.config.tls:
            c.tls_set(ca_certs=self.config.ca_certs or None)
            if self.config.tls_insecure:
                c.tls_insecure_set(True)
        c.enable_logger(_PAHO_LOGGER)
        return c

    def public_config(self) -> dict[str, Any]:
        d = asdict(self.config)
        d["password"] = "***" if d["password"] else ""
        d["base_topic"] = self.wanted_base_topic
        d["client_id"] = self.wanted_base_topic
        d["derived"] = ["base_topic", "client_id"]
        return d

    # ----- lifecycle -------------------------------------------------------

    async def async_start(self) -> None:
        self._undiscover_due = await self.hass.async_add_executor_job(self._read_undiscover_due)
        self._cleanup_pending = await self.hass.async_add_executor_job(self._read_cleanup_pending)
        self._manager_announced = await self.hass.async_add_executor_job(self._read_manager_announced)
        # listeners for the life of the process: the publisher is never set up twice
        self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state)
        self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry)
        # A device renamed (or removed) here changes the device block of its
        # discovery config only; the entity registry stays silent, so nothing
        # else would refresh discovery before the hourly full republish.
        self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._on_device_registry)
        self._arm_republish_timer()
        async_track_time_interval(self.hass, self._on_health_timer, timedelta(seconds=HEALTH_INTERVAL_S))
        async_track_time_interval(self.hass, self._on_cleanup_timer, timedelta(seconds=CLEANUP_RETRY_S))
        # the verdict follows the integration at once (its entry loading at boot, a
        # failed setup, a reload), not only at the next timer tick a minute later
        async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._on_entry_changed)
        self.hass.bus.async_listen(EVENT_COMPONENT_LOADED, self._on_component_loaded)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started)
        # Integrations register their services after we connect (the
        # integration loads later in the boot); refresh the catalog, debounced.
        for ev in (EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED):
            self.hass.bus.async_listen(ev, self._on_service_event)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)
        if self.config.enabled:
            # in the background: a broker that hangs, or the sweep of an identity that changed while
            # disconnected, must not hold up the setup of this component (and with it the boot)
            self.hass.async_create_background_task(self._async_first_connect(), "integration_manager MQTT connect")

    async def _async_first_connect(self) -> None:
        async with self._conn_lock:
            try:
                await self.hass.async_add_executor_job(self._connect)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("MQTT connect failed: %s", err)
                self.stats["connect_error"] = f"{type(err).__name__}: {err}"

    async def async_reload_config(self) -> None:
        """Adopt mqtt.json without reconnecting, for the settings that need none (discovery on/off).  The whole file
        is adopted: a broker, TLS or credential change in it (a hand edit, a form save whose reconnect has not run
        yet) is what status() and the pending cleanups name from here on, while the connection keeps the broker it
        has until the next reconnect.  Under the connection lock: a reconnect in flight would otherwise assign the
        config it loaded before this change and re-announce what Undo just cleared."""
        async with self._conn_lock:
            new = await self.hass.async_add_executor_job(self._load)
            if self.config.discovery_enabled and not new.discovery_enabled:
                self._set_undiscover_due(True)  # e.g. Undo on Cutover: its own cleanup clears this once the broker confirmed
            # the file may carry a save still waiting for its reconnect (the MQTT form, then a cutover): an exclusion
            # adopted here would otherwise never be compared again, and its entities stay published and commandable
            self._drop_newly_excluded(new)
            self.config = new

    def _drop_newly_excluded(self, new: MqttConfig) -> None:
        """Integrations excluded by `new`: their retained documents and discovery components go, and their
        entities stop taking commands (_topics is what a command or a call may reach)."""
        newly_excluded = set(new.exclude_integrations) - set(self.config.exclude_integrations)
        if not newly_excluded:
            return
        for eid, topic in list(self._topics.items()):
            if (self._integration_of(eid) or "unregistered") not in newly_excluded:
                continue
            if self._connected:
                if self.config.discovery_enabled:
                    self._remove_component(eid)  # removal first, then the empty document
                self._clear(eid)
            else:
                # cleared at the next connect: _publish_state never touches an excluded entity again
                self._topics.pop(eid, None)
                self._last_hash.pop(topic, None)
                self._pending_clears.add(topic)
        if not self._connected:
            self._resync_excluded = True  # after a restart this process does not know every topic of theirs

    def _undiscover_file(self) -> str:
        return self.hass.config.path("integration_manager", "mqtt_undiscover.json")

    def _set_undiscover_due(self, due: bool) -> None:
        """Kept on disk: a restart before the broker confirmed the cleanup must not forget it.  Written by the ordered
        writer, off the loop: "due" and "done" recorded moments apart land in that order, which a file removed here
        and written there would not guarantee.  A failed write is logged by the writer."""
        if not due and not self._undiscover_due:
            return  # nothing recorded: the file is absent or already says so
        self._undiscover_due = due
        writer.write_nowait(self._undiscover_file(), {"due": due, "base": self.base_topic, "prefix": self.config.discovery_prefix},
                            fsync=False)

    def _read_undiscover_due(self) -> bool:
        """Blocking.  A file without "due" was written by an older version, which removed it once done."""
        data = read_json(self._undiscover_file(), None)
        return isinstance(data, dict) and data.get("due", True) is not False

    def undiscover_done(self) -> None:
        """The retained discovery configs were cleared (Cutover Undo)."""
        self._set_undiscover_due(False)

    def _arm_republish_timer(self) -> None:
        if self._republish_unsub is not None:
            self._republish_unsub()
        self._republish_unsub = async_track_time_interval(
            self.hass, self._on_timer, timedelta(seconds=_bounded("republish_interval_s", self.config.republish_interval_s)))
        self._republish_interval = self.config.republish_interval_s

    async def async_reconnect(self) -> None:
        async with self._conn_lock:
            await self._async_reconnect_locked()

    async def _async_reconnect_locked(self) -> None:
        """Reload the config file and reconnect.  If the instance identity
        (running integration) or the discovery prefix changed since we
        connected, everything we own under the old names is cleared first
        (a stop is not a move: the consumer keeps its entities)."""
        new = await self.hass.async_add_executor_job(self._load)
        # A stop (wanted identity None) is NOT a move: the consumer keeps its
        # entities, marked unavailable by the retained "offline"; clearing
        # would delete them there with every customisation.  Uninstall clears.
        moved = self._connected and ((self.wanted_base_topic is not None and self.wanted_base_topic != self._live_base)
                                     or new.discovery_prefix != self.config.discovery_prefix)
        if self.wanted_base_topic != getattr(self, "_last_wanted", self.wanted_base_topic):
            self._started_at = time.time()  # health grace restarts with a new identity
            self._pending_clears.clear()
        self._last_wanted = self.wanted_base_topic
        if not moved:
            self._drop_newly_excluded(new)
        for t in ("_registry_timer", "_services_timer"):
            h = getattr(self, t, None)
            if h is not None:
                h.cancel()
                setattr(self, t, None)
        swept = True  # a failed sweep is retried at the next connect
        if moved:
            self._moving = True  # state events keep arriving: nothing goes out under the old names
            # The consumer would keep every retained doc/config under the old
            # names (and reject the new ones as duplicate unique_ids).
            # the throwaway sweep finds everything retained under the old names,
            # what this process published included; forget the bookkeeping
            for m in (self._topics, self._last_hash, self._discovery_map, self._blocks):
                m.clear()
            self._services_published.clear()
            # a new discovery prefix alone moves only the discovery configs: the documents stay where they are
            base_moved = self.wanted_base_topic is not None and self.wanted_base_topic != self._live_base
            cleared = await self.hass.async_add_executor_job(self._clear_retained_under, self.base_topic, self.config.discovery_prefix, base_moved)
            swept = cleared is not None
            _LOGGER.info("MQTT: cleared %s retained topics left under the old names by earlier runs", cleared)
        if moved and swept:
            # the live move handled it: the next connect must not sweep the old names a second time (a change
            # while disconnected is left to _connect, which compares the recorded names with the new ones)
            await self.hass.async_add_executor_job(self._remember_identity, self.wanted_base_topic, new.discovery_prefix)
        if self.config.discovery_enabled and not new.discovery_enabled:
            # off means off: without this the consumer keeps every entity, and whatever changes here
            # meanwhile (a disabled or deleted entity) stays there as a zombie; like Undo on Cutover
            self._set_undiscover_due(True)
        if self._connected and self.wanted_base_topic is None and not moved:
            # a stop: the retained verdict must not keep saying "ok" for an integration that no longer runs,
            # and the retained catalog must not keep advertising services the consumer can no longer call
            self._publish(self._health_topic(), _dumps(self.build_health()), qos=1)
            self._clear_services_catalog()
        # no retained "offline" on a status topic we just cleared
        await self.hass.async_add_executor_job(self._disconnect, not moved)
        self._moving = False
        if new.main_ha_version != self.config.main_ha_version:
            self._compat_warned.clear()  # a new declared version must say again what it leaves out
        self.config = new
        if getattr(self, "_republish_interval", None) != new.republish_interval_s:
            self._arm_republish_timer()
        if self.rules.problem:  # fixed or removed since: read again before anything is published
            await self.hass.async_add_executor_job(self.rules.load)
        if self.config.enabled:
            await self.hass.async_add_executor_job(self._connect)
        self.publish_health()  # status() shows the new identity's verdict right away

    def _is_ours(self, topic: str, payload: bytes, base_topic: str) -> bool:
        """Only what this tool publishes may be cleared: never someone else's
        retained data that happens to share a prefix."""
        if not payload:
            return False
        if topic == f"{base_topic}/status":
            return payload in (b"online", b"offline")
        if topic.startswith((f"{base_topic}/cmd/", f"{base_topic}/call/", f"{base_topic}/result/", f"{base_topic}/manager/cmd/")):
            return True  # a consumer that retained a command must not block our connect
        try:
            doc = json.loads(payload)
        except ValueError:
            return False
        if not isinstance(doc, dict):
            return False
        if topic == f"{base_topic}/health":
            return "updated_at" in doc and "base_topic" in doc
        if topic == f"{base_topic}/manager":
            return "updated_at" in doc and "manager_version" in doc
        if topic.startswith(base_topic + "/"):
            return ("published_at" in doc and "integration" in doc) or "call_topic" in doc
        # exact origin of THIS identity: instance hass_a must not clear hass_a_b's configs
        origin_name = str((doc.get("origin") or {}).get("name", ""))
        return origin_name == disc.origin(base_topic + "_")["name"]

    def probe_foreign(self, base_topic: str) -> dict[str, Any]:
        """Blocking: what sits retained under <base>/# that is NOT ours."""
        try:
            found = self._retained_scan("probe", [(f"{base_topic}/#", 1)], min_s=2.0)
        except Exception as err:  # noqa: BLE001
            return {"error": f"{type(err).__name__}: {err}", "foreign": [], "ours": 0}
        foreign = [t for t, p in found.items() if not self._is_ours(t, p, base_topic)]
        return {"foreign": sorted(foreign)[:20], "foreign_count": len(foreign), "ours": len(found) - len(foreign)}

    def _retained_scan(self, suffix: str, topics: list[tuple[str, int]], min_s: float = 2.0) -> dict[str, bytes]:
        """Blocking: a throwaway client that collects every retained message
        under `topics` until the burst goes quiet, or until it holds
        RETAINED_SCAN_MAX_BYTES; returns {topic: payload}.  The network
        thread is always stopped, whatever happens."""
        found: dict[str, bytes] = {}
        budget = [0, 0]  # bytes held, messages left unread once the budget was spent (paho's thread only)

        def keep(_cl, _u, m) -> None:
            if not m.retain or not m.payload:
                return
            if budget[0] >= RETAINED_SCAN_MAX_BYTES:
                budget[1] += 1
                return  # nothing new is added, so _collect_quiet sees the burst go quiet and ends the scan
            budget[0] += len(m.payload) + len(m.topic)
            found[m.topic] = m.payload

        deadline = time.monotonic() + 5
        c = self._throwaway_client(suffix, "scan", deadline, keep)
        try:
            granted: list[Any] = []
            c.on_subscribe = lambda cl, u, mid, codes, props=None: granted.append(codes)
            c.subscribe(topics)
            while not granted and time.monotonic() < deadline:
                time.sleep(0.05)
            if not granted or any(getattr(g, "is_failure", False) for g in granted[0]):
                raise RuntimeError("the broker refused the subscription (ACL?)")
            self._collect_quiet(c, found, min_s=min_s)
        finally:
            self._stop_client(c)
        if budget[1]:
            _LOGGER.warning("MQTT: the %s scan stopped at %s retained topics (%s bytes, its maximum): at least %s further "
                            "retained messages under %s were left unread",
                            suffix, len(found), budget[0], budget[1], ", ".join(t for t, _q in topics))
        return found

    def _throwaway_client(self, suffix: str, what: str, deadline: float, on_message: Any = None) -> mqtt.Client:
        """Blocking: a client of its own, connected (CONNACK received) with its network loop running, else RuntimeError.
        A broker that refuses MQTT 5, or keeps fewer unacknowledged messages than the client would send, gets a
        second client that suits it; the first one never subscribed or published anything."""
        for _attempt in range(2):
            c = self._new_client(f"{self.client_id}-{suffix}-{secrets.token_hex(3)}")
            ack: dict[str, Any] = {"rc": None, "props": None}
            c.on_connect = lambda cl, u, flags, rc, props=None: ack.update(rc=rc, props=props)
            c.on_message = on_message
            c.connect(self.config.host, self.config.port, keepalive=30, **self._connect_options(c))
            c.loop_start()
            # is_connected() is false until the broker's CONNACK: a slow (remote, TLS) broker is not a lost one
            while ack["rc"] is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if ack["rc"] is not None and (self._learned_mqtt311(c, ack["rc"]) if ack["rc"] != 0 else self._learned_receive_max(c, ack["props"])):
                # stopped beside the second attempt, not before it: the stop takes a second on a broker that answers
                # (paho's select), up to 2 x STOP_JOIN_S on one that does not, and the deadline is the second
                # handshake's; that client never subscribed or published, and its own stop stays bounded
                threading.Thread(target=self._stop_client, args=(c,), name="hri-mqtt-stop-refused", daemon=True).start()
                continue
            if ack["rc"] is None or ack["rc"] != 0:
                self._stop_client(c)
                raise RuntimeError(f"the broker did not accept the {what} connection ({ack['rc']})")
            return c
        raise RuntimeError(f"the broker did not accept the {what} connection (refused twice)")

    @staticmethod
    def _stop_client(c: mqtt.Client) -> None:
        """Blocking, bounded: the client's network thread is gone when this returns, whatever the broker does.
        loop_stop() alone joins a thread that ends only once no QoS 1 message waits for its acknowledgement, which a
        live broker that stopped acknowledging never gives: the DISCONNECT goes first (paho closes the socket once
        it is written), and a thread still running after STOP_JOIN_S (the broker does not even read) has its socket
        closed under it.  Nothing it still reads is handed on (see _disconnect)."""
        c.on_message = None
        try:
            c.disconnect()
        except Exception:  # noqa: BLE001
            pass

        def stop() -> None:
            try:
                c.loop_stop()
            except Exception:  # noqa: BLE001 - the thread ended between paho's check and its join
                pass

        stopper = threading.Thread(target=stop, name="hri-mqtt-stop", daemon=True)
        stopper.start()
        stopper.join(STOP_JOIN_S)
        if stopper.is_alive():
            try:
                sock = c.socket()
                if sock is not None:
                    sock.close()
            except Exception:  # noqa: BLE001
                pass
            stopper.join(STOP_JOIN_S)

    def _clear_topics(self, suffix: str, topics: list[str]) -> None:
        """Blocking: an empty retained payload to each topic from a throwaway
        client (QoS 1, awaited)."""
        if not topics:
            return
        # one budget for the whole sweep, not 5 s per topic: a broker that stops acknowledging
        # would otherwise hold the reconnect lock (or an uninstall) for hours
        deadline = time.monotonic() + min(120.0, 15.0 + 0.02 * len(topics))
        c = self._throwaway_client(f"{suffix}-clear", "cleanup", deadline)
        try:
            infos = [c.publish(t, "", qos=1, retain=True) for t in topics]
            while time.monotonic() < deadline and c.is_connected() and not all(i.is_published() for i in infos):
                time.sleep(0.1)
            unconfirmed = sum(1 for i in infos if not i.is_published())
            if unconfirmed:
                raise RuntimeError(f"the broker did not confirm {unconfirmed} of {len(infos)} cleared topics")
        finally:
            self._stop_client(c)

    @staticmethod
    def _collect_quiet(c: mqtt.Client, found: dict[str, bytes], min_s: float = 2.0, quiet_s: float = 1.0, max_s: float = 15.0) -> None:
        """Wait for the retained burst: at least min_s, then until nothing new
        arrived for quiet_s, capped at max_s (busy brokers with thousands of
        retained configs need more than a fixed 3 s)."""
        t0 = time.time()
        last_n, last_change = -1, t0
        while True:
            time.sleep(0.25)
            now = time.time()
            if len(found) != last_n:
                last_n, last_change = len(found), now
            if now - t0 >= min_s and now - last_change >= quiet_s:
                return
            if now - t0 >= max_s:
                return

    def _identity_file(self) -> str:
        return self.hass.config.path("integration_manager", "mqtt_identity.json")

    def _remember_identity(self, base: str | None, prefix: str) -> None:
        """Blocking: the names retained data was last published under, and the broker it went to."""
        if not base:
            return
        try:
            write_json(self._identity_file(), {"base": base, "prefix": prefix, "broker": self._broker_identity()}, fsync=False)
        except OSError:
            pass

    def _clear_retained_under(self, base_topic: str, discovery_prefix: str, docs: bool = True) -> int | None:
        """Blocking: every retained topic of ours under <base>/# (unless
        ``docs`` is False) plus the discovery configs carrying our origin
        under <prefix>/device/+/config get an empty retained payload."""
        return self._clear_retained_checked(base_topic, discovery_prefix, docs)[0]

    def _clear_retained_checked(self, base_topic: str, discovery_prefix: str, docs: bool = True, warn: bool = True) -> tuple[int | None, str]:
        """Blocking: _clear_retained_under, with the reason when it was not done."""
        key = self._pending_key(base_topic, self._broker_identity())  # the broker the scan reaches
        try:
            topics = [(f"{discovery_prefix}/device/+/config", 1)] + ([(f"{base_topic}/#", 1)] if docs else [])
            found = self._retained_scan("cleanup", topics)
            ours = [t for t, p in found.items() if self._is_ours(t, p, base_topic)]
            self._clear_topics("cleanup", ours)
        except Exception as err:  # noqa: BLE001
            (_LOGGER.warning if warn else _LOGGER.debug)("retained cleanup under %s failed: %s", base_topic, err)
            return None, f"{type(err).__name__}: {err}"  # not done: the identity must not be recorded as moved
        skipped = len(found) - len(ours)
        if skipped:
            _LOGGER.info("MQTT: left %s retained topics under %s alone (not ours)", skipped, base_topic)
        if docs and (self._cleanup_pending.get(key) or {}).get("prefix") == discovery_prefix:
            self._set_cleanup_pending(key, None)  # whoever swept it on its broker (an identity sweep too): nothing left to retry
        return len(ours), ""

    def _cleanup_pending_file(self) -> str:
        return self.hass.config.path("integration_manager", "mqtt_cleanup_pending.json")

    def _broker_identity(self) -> dict[str, Any]:
        """The broker the settings name, as far as a pending removal tells brokers apart (never the password)."""
        return {"host": self.config.host, "port": self.config.port, "tls": self.config.tls, "username": self.config.username}

    @staticmethod
    def _pending_key(base: str, broker: dict[str, Any]) -> tuple:
        """Host and port decide which broker a removal waits for, as in _recorded_elsewhere: another user, or TLS turned
        on, reaches the same retained data, and a key holding them never matched again once either changed.  The record
        keeps all four (the file format is unchanged), so older files read into this key as they are."""
        return (base, str(broker["host"]), int(broker["port"]))

    def _read_cleanup_pending(self) -> dict[tuple, dict[str, Any]]:
        """Blocking.  Records written before they named their broker (a mapping by base topic) are bound to the broker
        configured now, and saved so at once: a later change of the settings must not move them."""
        data = read_json(self._cleanup_pending_file(), None)
        pending = data.get("pending") if isinstance(data, dict) else None
        unbound = isinstance(pending, dict)
        if unbound:
            pending = [{**rec, "base": base, "broker": self._broker_identity()} for base, rec in pending.items() if isinstance(rec, dict)]
        if not isinstance(pending, list):
            return {}
        out: dict[tuple, dict[str, Any]] = {}
        for rec in pending:
            try:
                key = self._pending_key(rec["base"], rec["broker"])
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(rec["base"], str) and rec["base"].startswith("hass_"):
                out[key] = rec
        if unbound:
            try:
                write_json(self._cleanup_pending_file(), {"pending": list(out.values())}, fsync=False)
            except OSError as err:
                _LOGGER.warning("MQTT: the pending retained cleanup could not be saved: %s", err)
        return out

    def _set_cleanup_pending(self, key: tuple, record: dict[str, Any] | None) -> bool:
        """Blocking: records the removal of one identity on one broker as pending (record None: drops it).  Kept on disk:
        a restart before the broker is back must not forget it.  True when something changed."""
        with self._cleanup_pending_lock:
            if record is None and key not in self._cleanup_pending:
                return False
            pending = {k: r for k, r in self._cleanup_pending.items() if k != key}
            if record is not None:
                pending[key] = record
            self._cleanup_pending = pending
            try:
                write_json(self._cleanup_pending_file(), {"pending": list(pending.values())}, fsync=False)
            except OSError as err:
                _LOGGER.warning("MQTT: the pending retained cleanup could not be saved: %s", err)
        return True

    def retained_cleanup_pending(self, base: str | None = None) -> list[dict[str, Any]]:
        """The removals that wait for a broker (of one identity when `base` is given), those for the broker the settings
        name first: {base_topic, broker (host:port), other_broker, deferred, error, since}."""
        here, pending = self._pending_key("", self._broker_identity())[1:], self._cleanup_pending
        keys = sorted((k for k in pending if base is None or k[0] == base), key=lambda k: (k[1:] != here, k))
        return [{"base_topic": k[0], "broker": f"{k[1]}:{k[2]}", "other_broker": k[1:] != here, "deferred": bool(pending[k].get("deferred")),
                 "error": self._pending_reason(k, pending[k], here), "since": pending[k].get("since")} for k in keys]

    def _pending_reason(self, key: tuple, rec: dict[str, Any], here: tuple) -> str:
        """Why it still waits, as things are now: the reason recorded at the last try ("MQTT is disabled", a broker error)
        is stale once the settings changed."""
        if key[1:] != here:
            return f"waiting for the MQTT settings to name the broker {key[1]}:{key[2]} again"
        if not self.config.enabled:
            return "MQTT is disabled"
        if rec.get("deferred"):
            return "MQTT is enabled again: the removal runs at the next try (every minute)"
        return rec.get("error") or ""

    def _cancel_pending_cleanup(self, key: tuple) -> None:
        """Blocking: the identity is wanted again on that broker (installed and started before the broker came back): what
        it publishes is live and stays.  What the uninstalled copy announced that this one does not have goes with the
        orphan sweep of a later full republish, which reads the retained configs again first."""
        base = key[0]
        if self._set_cleanup_pending(key, None):
            self._orphan_sweep_due, self._boot_components = True, None
            _LOGGER.info("MQTT: %s runs again: the pending removal of its retained data is cancelled", base)
            events.emit("mqtt", f"pending removal of the retained data of {base} cancelled: {base} runs again")

    async def _on_cleanup_timer(self, _now) -> None:
        if not self._cleanup_pending or self._cleanup_retrying or self._stopping:
            return
        self._cleanup_retrying = True
        try:
            async with self._conn_lock:  # no start or reconnect in between: nothing may be published under a name being cleared
                await self.hass.async_add_executor_job(self._retry_pending_cleanups)
        finally:
            self._cleanup_retrying = False

    def _retry_pending_cleanups(self) -> None:
        """Blocking, under the connection lock: the removals an uninstall could not finish, from throwaway clients (no
        identity needed), each under its own base topic and discovery prefix only, and only on the broker it is for: the
        settings may name another one by now, where an empty scan says nothing about the broker that keeps the data."""
        here = self._pending_key("", self._broker_identity())[1:]
        for key, rec in list(self._cleanup_pending.items()):
            base = key[0]
            if self._stopping:
                return
            if key[1:] != here:
                continue  # waits until the settings name that broker again
            if base == self.wanted_base_topic:
                self._cancel_pending_cleanup(key)
                continue
            if base == self._live_base or not self.config.enabled:
                continue  # still connected under it (the uninstall's reconnect comes next), or MQTT is off
            n, why = self._clear_retained_checked(base, rec.get("prefix") or self.config.discovery_prefix, warn=False)
            if n is None:
                if rec.get("deferred"):  # the first try since MQTT is on: the reason is the broker now
                    self._set_cleanup_pending(key, {**rec, "deferred": False, "error": why})
                return  # the broker is still unreachable: the next tick tries again
            self._set_cleanup_pending(key, None)
            why = "MQTT was disabled at the uninstall" if rec.get("deferred") else "the broker was unreachable at the uninstall"
            _LOGGER.info("MQTT: cleared %s retained topics of the uninstalled %s (%s)", n, base, why)
            events.emit("mqtt", f"cleared {n} retained topics of the uninstalled {base} ({why})")

    async def _on_stop(self, _: Event) -> None:
        # first: a connect still probing or sweeping (it holds _conn_lock, which a stop does not wait for) must not
        # create its client, publish "online" and start a republish after this disconnect
        self._stopping = True
        await self.hass.async_add_executor_job(self._disconnect)

    # ----- paho ------------------------------------------------------------

    def _status_topic(self) -> str:
        return f"{self.base_topic}/status"

    def _recorded_elsewhere(self, last: dict[str, Any]) -> bool:
        """The names recorded last went to a broker the settings no longer name.  A record written before brokers
        were named (no ``broker`` key) belongs to the configured one: that is what _defer_cleanup assumes too.

        Host and port only: a different user, or TLS turned on, is the same broker holding the same retained
        data, and treating it as another one deferred a removal to a broker that was never going to be named
        again - a pending entry nothing could ever clear.  The pending key (_pending_key) compares the same two.
        """
        try:
            here = self._broker_identity()
            return (str(last["broker"]["host"]), int(last["broker"]["port"])) != (str(here["host"]), int(here["port"]))
        except (KeyError, TypeError, ValueError):
            return False

    def _defer_foreign_identity(self, last: dict[str, Any]) -> None:
        """Blocking: the retained data of the names recorded last sits on another broker, which no client built here
        reaches.  Recorded as pending for that broker, like an uninstall during an outage: _retry_pending_cleanups
        clears it once the settings name that broker again, and the Status page says so meanwhile."""
        key = self._pending_key(last["base"], last["broker"])
        if key in self._cleanup_pending:
            return  # already waiting: keep its "since"
        self._set_cleanup_pending(key, {"base": last["base"], "prefix": last.get("prefix") or self.config.discovery_prefix,
                                        "broker": last["broker"], "error": "", "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        _LOGGER.warning("MQTT: what %s published stays on %s:%s (the settings name another broker now): it is removed "
                        "when they name it again", last["base"], key[1], key[2])
        events.emit("mqtt", f"retained data of {last['base']} left on {key[1]}:{key[2]}: the settings name another broker now")

    def _sweep_old_identity(self, base: str) -> bool:
        """Blocking: what the names recorded last (base topic, discovery prefix) left retained, when they differ from
        the current ones; records the current names once done.  False when the sweep failed (retried later)."""
        last = read_json(self._identity_file(), {}) or {}
        swept = True
        if isinstance(last, dict) and last.get("base"):
            if self._recorded_elsewhere(last):
                # a scan from here would look for that identity on this broker, find nothing, report a clean sweep
                # and record the new names: what is retained on the other broker would be forgotten, and the main
                # Home Assistant reading that broker would keep the entities for good
                self._defer_foreign_identity(last)
            elif (last["base"], last.get("prefix")) != (base, self.config.discovery_prefix):
                # changed while we were not connected (async_reconnect only moves a live
                # connection): what the previous names left retained goes now; with the
                # same identity only the discovery configs under the old prefix moved
                same_base = last["base"] == base
                n = self._clear_retained_under(last["base"], last.get("prefix") or self.config.discovery_prefix, docs=not same_base)
                swept = n is not None
                _LOGGER.info("MQTT: identity/prefix changed while disconnected (%s/%s -> %s/%s): cleared %s retained topics",
                             last.get("base"), last.get("prefix"), base, self.config.discovery_prefix, n)
        if swept:
            self._remember_identity(base, self.config.discovery_prefix)
        return swept

    def _connect(self) -> None:
        if self._stopping:
            return
        if self.rules.problem:
            # fail closed: which entities are excluded is unknown, so none is published and no command is taken (the
            # main HA shows them unavailable after the retained "offline", and keeps them)
            self.stats["connect_error"] = f"not connecting: {self.rules.problem}"
            return
        base = self.wanted_base_topic
        if not base:
            # nothing running -> no identity -> nothing to publish under
            self.stats["connect_error"] = "no integration is running: MQTT has no identity (hass_<domain>) until one starts"
            _LOGGER.info("MQTT: %s", self.stats["connect_error"])
            return
        if (key := self._pending_key(base, self._broker_identity())) in self._cleanup_pending:
            self._cancel_pending_cleanup(key)  # a removal pending on another broker stays: this one does not reach it
        # a different broker is a different namespace; other TLS settings may not reach the same one
        probe_key = f"{self.config.host}:{self.config.port}/{base}/{self.config.tls}/{self.config.ca_certs}/{self.config.tls_insecure}"
        if not self.config.force_base_topic and probe_key not in self._probed_ok:
            probe = self.probe_foreign(base)
            self.stats["foreign_topics"] = probe.get("foreign", [])
            self.stats["foreign_count"] = probe.get("foreign_count", 0)
            if probe.get("foreign_count"):
                self.stats["connect_error"] = (f"base topic {base} already carries {probe['foreign_count']} retained topics that are not ours "
                                               f"(e.g. {probe['foreign'][0]}); not connecting. Tick force_base_topic to use it anyway")
                _LOGGER.error("MQTT: %s", self.stats["connect_error"])
                return
            if probe.get("error"):
                _LOGGER.warning("MQTT: could not verify that %s is free (%s); connecting; verified again when the manager reconnects (a restart or saving the MQTT settings), not on a broker reconnect", base, probe["error"])
            else:
                self._probed_ok.add(probe_key)  # this process owns the namespace now: no re-probe on reconnects
        elif self.config.force_base_topic:
            self.stats["foreign_topics"], self.stats["foreign_count"] = [], 0
        # a failed sweep (the broker unreachable right now) is retried by the full republish after paho connects
        self._identity_sweep_due = not self._sweep_old_identity(base)
        if self._stopping:
            return
        self._live_base = base
        self._live_prefix = base + "_"
        self._tls_checked_at, self._tls_error, self._last_disconnect = 0.0, "", ""  # new settings: report afresh
        self._connected_at, self._broker_max_packet = 0.0, 0
        old = self._client
        if old is not None:  # belt and braces next to the lock: never leave a second client running
            self._client = None
            self._stop_client(old)
        try:
            c = self._new_client(self.client_id)  # tls_set raises on an unreadable CA file
            c.will_set(self._status_topic(), "offline", qos=1, retain=True)
            c.on_connect = self._on_connect
            c.on_connect_fail = self._on_connect_fail
            c.on_disconnect = self._on_disconnect
            c.on_message = self._on_message
            c.on_subscribe = self._on_subscribe
            c.suppress_exceptions = True  # a callback bug must not kill the network thread
            c.reconnect_delay_set(min_delay=2, max_delay=60)
            c.connect_async(self.config.host, self.config.port, keepalive=60, **self._connect_options(c, INBOUND_MAX_PACKET))
            self._client = c  # before its network thread starts: "online" goes out only for the current client
            c.loop_start()
            self.stats["connect_error"] = ""
        except Exception as err:  # noqa: BLE001
            self.stats["connect_error"] = f"{type(err).__name__}: {err}"
            _LOGGER.error("MQTT connect failed: %s", err)
            return
        if self._stopping:
            self._disconnect(publish_offline=False)  # the stop ran between the check above and the client existing

    def _disconnect(self, publish_offline: bool = True) -> None:
        with self._subscribing_lock:  # an "online" being published goes out before the "offline" below, none after it
            c, self._client = self._client, None
            self._subscribing = None
        if c is not None:
            # no command starts once the client is let go: its thread keeps reading through the "offline" below and
            # its stop (seconds on a broker that stops reading), and what it ran there would answer on a client that
            # is gone - a caller that retries on silence runs it twice.  paho reads the callback under the lock its
            # setter takes; one already handed to _on_message finishes, as it would a moment earlier.
            c.on_message = None
        if c is None:
            self._live_base = self._live_prefix = None
            self._forget_errors()
            return
        try:
            if publish_offline and self._connected:
                c.publish(self._status_topic(), "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:  # noqa: BLE001
            pass
        finally:
            # always: an orphaned paho thread would keep reconnecting with
            # our callbacks bound and steal the client id from the new client
            self._stop_client(c)
        self._connected = False
        self._connected_at = 0.0
        self.stats["connected"] = False
        self._live_base = self._live_prefix = None
        self._forget_errors()  # after _stop_client: its network thread is gone and reports nothing more
        self.hass.loop.call_soon_threadsafe(self._last_hash.clear)  # a new connection re-asserts every retained document

    def _stale_client(self, client) -> bool:
        """Did an older paho client call this?

        A client we replaced keeps its own network thread until its socket gives up - a TLS listener that
        never answers can hold one for minutes - and our callbacks are still bound to it.  _on_subscribe has
        always checked; the three connection callbacks did not, so a dying thread could report itself
        disconnected over a healthy connection and leave the publisher believing it has no broker.
        """
        return self._client is not None and client is not self._client

    def _forget_errors(self) -> None:
        """A deliberate end (settings saved, identity changed, stop): what went wrong belonged to a connection that no longer
        exists, and a refusal by the next one (another broker, perhaps) is news.  paho's own reconnects do not come here."""
        self.stats["connect_error"] = self.stats["subscribe_error"] = ""
        self._last_refusal = ""
        self._refused = False
        self._last_subscribe_error = ""

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if self._stale_client(client):
            return
        if reason_code != 0:
            self._connected = False
            self.stats["connected"] = False
            # paho 2.1 calls on_disconnect right after a refused CONNACK (MQTT 5 and 3.1.1 alike), with a reason of its
            # own ("Unspecified error"): _on_disconnect keeps what is said here
            self._refused = True
            if self._learned_mqtt311(client, reason_code):
                self.stats["connect_error"] = "the broker refused MQTT 5: reconnecting with MQTT 3.1.1"
                self._replace_client_soon(client)
                return
            reason = str(reason_code)
            what = "login" if reason in ("Not authorized", "Bad user name or password") else "connection"
            self.stats["connect_error"] = f"the broker refused the {what} (reason_code={reason_code}); retrying"
            if reason != self._last_refusal:  # one line and one event per reason, like a disconnection: paho retries
                self._last_refusal = reason   # for ever, and a wrong password would otherwise write ~1440 ERRORs a day
                _LOGGER.error("MQTT connect refused: %s", reason_code)
                events.emit("mqtt", f"the broker refused the {what}: {reason_code}")
            return
        self._refused = False
        if self._stopping:
            return  # no "online" and no republish while Home Assistant stops
        if self._learned_receive_max(client, properties):
            # nothing sent yet: a burst of discovery configs over the broker's receive maximum would cost the connection
            self._replace_client_soon(client)
            return
        self._connected = True
        self._connected_at = time.monotonic()
        # MQTT 5 only: the broker states what it accepts, which beats guessing; paho does not check it
        announced = getattr(properties, "MaximumPacketSize", None)
        self._broker_max_packet = announced if isinstance(announced, int) and announced > 0 else 0
        v5 = getattr(client, "protocol", None) == mqtt.MQTTv5
        self._session_v5 = v5  # read by _note_cleared on paho's thread: only a 3.1.1 session echoes our own clears
        self.stats["connected"] = True
        self.stats["connect_error"] = ""
        self.stats["subscribe_error"] = ""
        self.stats["protocol"] = "MQTT 5" if v5 else "MQTT 3.1.1"
        self._tls_checked_at, self._tls_error, self._last_disconnect = 0.0, "", ""
        # Commands from the consuming HA: <base>/cmd/<domain>/<object_id>/<field>
        # and generic service calls: <base>/call/<domain>/<service>
        topics = [f"{self._cmd_base()}/#", f"{self._call_base()}/#", f"{self._manager_cmd_base()}/+"]
        if v5:
            # retainAsPublished: a command published with retain while we are subscribed arrives flagged, so it is
            # refused and cleared at once (3.1.1 brokers drop the flag on live delivery).  noLocal: the empty
            # payload that clears it does not come back to us.
            options = SubscribeOptions(qos=1, noLocal=True, retainAsPublished=True)
            rc, mid = client.subscribe([(t, options) for t in topics])
        else:
            rc, mid = client.subscribe([(t, 1) for t in topics])
        if rc != mqtt.MQTT_ERR_SUCCESS:
            with self._subscribing_lock:
                self._subscribing = None
            self._subscribe_failed(f"the subscription could not be sent ({mqtt.error_string(rc)})")
            self._announce_online(client)
        else:
            # "online" once the broker acknowledged the subscription (_on_subscribe): a command sent at the availability
            # flip would otherwise arrive before it and be lost
            with self._subscribing_lock:
                self._subscribing = [client, mid, topics, False]
            self.hass.loop.call_soon_threadsafe(lambda: self.hass.loop.call_later(SUBACK_WAIT_S, self._suback_overdue, client, mid))
        _LOGGER.info("MQTT connected to %s:%s", self.config.host, self.config.port)
        events.emit("mqtt", f"connected to {self.config.host}:{self.config.port} as {self.base_topic}")
        # Runs in paho's thread: hop onto the HA loop for the full publish.  A
        # broker that came back without its retained store must get every
        # discovery config and the service catalog again (paho's automatic
        # reconnect lands here too): the "already published" memory is dropped
        # on the loop, which is the only place that reads it, right before.
        def _resume() -> None:
            self._last_hash.clear()
            self._oversized_warned.clear()  # what could not be published is worth reporting again on a new connection
            self._manager_absent_sent = False
            self.hass.async_create_task(self.async_republish_all())

        self.hass.loop.call_soon_threadsafe(_resume)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None) -> None:
        """Paho thread: the SUBACK.  A broker whose ACL allows publishing but not subscribing refuses the topics here, and
        only here: the connection stays up and state mirroring works while no command reaches this container."""
        with self._subscribing_lock:
            pending = self._subscribing
            if pending is None or pending[0] is not client or pending[1] != mid:
                return  # an earlier connection's
            self._subscribing = None
        refused = [f"{topic} ({code})" for topic, code in zip(pending[2], reason_codes) if getattr(code, "is_failure", False)]
        if refused:
            self._subscribe_failed(f"the broker refused the subscription to {', '.join(refused)}")
        else:
            self._last_subscribe_error = ""
            if pending[3]:  # announced by the overdue timer, which reported a SUBACK that did come after all
                self.stats["subscribe_error"] = self.stats["connect_error"] = ""
        if not pending[3]:
            # a refusal too: the documents still flow, and an offline device would hide them for nothing
            self._announce_online(client)

    def _suback_overdue(self, client: mqtt.Client, mid: Any) -> None:
        """Loop: no SUBACK after SUBACK_WAIT_S."""
        with self._subscribing_lock:
            pending = self._subscribing
            if pending is None or pending[0] is not client or pending[1] != mid or pending[3]:
                return
            pending[3] = True
        if client is not self._client or not self._connected:
            return
        self._subscribe_failed(f"the broker did not acknowledge the subscription within {SUBACK_WAIT_S} s")
        self._announce_online(client)

    def _announce_online(self, client: mqtt.Client) -> None:
        """Under the subscribing lock, which _disconnect takes to detach the client before its retained "offline": an
        "online" racing it goes out first or not at all.  publish() only queues the packet, so paho's thread never waits."""
        with self._subscribing_lock:
            if client is self._client and not self._stopping:
                client.publish(self._status_topic(), "online", qos=1, retain=True)

    def _subscribe_failed(self, reason: str) -> None:
        message = (f"{reason}: commands, service calls and manager actions from the main Home Assistant do not reach this "
                   "container (a broker ACL that denies subscribing?); state is still published")
        self.stats["subscribe_error"] = self.stats["connect_error"] = message
        if message != self._last_subscribe_error:  # once, not once per reconnect
            self._last_subscribe_error = message
            _LOGGER.error("MQTT: %s", message)
            events.emit("mqtt", message)

    def _replace_client_soon(self, client: mqtt.Client) -> None:
        """Paho thread: paho would retry with the same protocol and window forever, so a new client is built,
        on the loop and under the connection lock like any reconnect."""
        async def replace() -> None:
            async with self._conn_lock:
                if self._client is not client or self._stopping:
                    return  # a reconnect or a stop replaced it first
                await self.hass.async_add_executor_job(self._disconnect, False)
                await self.hass.async_add_executor_job(self._connect)

        self.hass.loop.call_soon_threadsafe(
            lambda: self.hass.async_create_background_task(replace(), "integration_manager MQTT client replaced"))

    def _on_connect_fail(self, client, userdata) -> None:
        """Paho thread: the broker could not be reached (paho keeps retrying)."""
        if self._stale_client(client):
            return
        where = f"{self.config.host}:{self.config.port}"
        message = f"cannot reach the broker at {where} (retrying)"
        if self.config.tls:
            now = time.monotonic()
            if not self._tls_checked_at or now - self._tls_checked_at >= TLS_CHECK_INTERVAL_S:
                self._tls_checked_at = now
                error = self._tls_handshake_error()
                if error and error != self._tls_error:
                    _LOGGER.warning("MQTT %s: %s", where, error)
                    events.emit("mqtt", f"{where}: {error}")
                self._tls_error = error
            if self._tls_error:
                message = f"{self._tls_error} ({where}, retrying)"
        self.stats["connect_error"] = message

    def _tls_handshake_error(self) -> str:
        """Blocking (paho thread): paho reports a failed TLS handshake as a plain connect failure, without the reason.
        One handshake of our own, with the same CA and host name check, names it: a wrong CA, a host name mismatch."""
        try:
            # built like paho's tls_set: create_default_context adds VERIFY_X509_STRICT (Python 3.13+), which refuses a
            # hand-made CA without keyUsage and would name that instead of paho's real reason
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if self.config.ca_certs:
                ctx.load_verify_locations(self.config.ca_certs)
            else:
                ctx.load_default_certs()
            if self.config.tls_insecure:
                ctx.check_hostname = False
            with socket.create_connection((self.config.host, self.config.port), timeout=5) as sock, \
                    ctx.wrap_socket(sock, server_hostname=self.config.host):
                return ""
        except ssl.SSLError as err:  # before OSError, its base class
            return f"TLS handshake failed: {getattr(err, 'verify_message', None) or err.reason or err}"
        except OSError:
            return ""  # not a TLS problem: the broker is unreachable, which the generic message says

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if self._stale_client(client):
            return
        lived = time.monotonic() - self._connected_at if self._connected_at else None
        self._connected = False
        self._connected_at = 0.0
        self.stats["connected"] = False
        # paho 2.1 reads the reason of a broker's MQTT 5 DISCONNECT only when properties follow it: a bare "packet too
        # large" (what mosquitto sends) reaches us as a normal disconnection, and a broker never ends a session we did
        # not ask it to end for a normal reason
        from_broker = getattr(flags, "is_disconnect_packet_from_server", False) is True
        if self._refused:
            self._refused = False  # the end of the refused attempt: its reason is reported and stays the status
            return
        if reason_code != 0 or from_broker:
            reason = str(reason_code) if reason_code != 0 else "the broker ended the session"
            # A plain connection to a TLS listener never gets a CONNACK, so the TLS hint is only honest while
            # none arrived.  A drop moments after one is a broker closing an established connection - a packet
            # over its maximum is the usual cause - and blaming TLS sends the operator the wrong way.
            # A second container running the same integration on this broker connects with the same client id, and
            # the broker ends the older session for it: both see this, over and over (mosquitto's MQTT 5 "session
            # taken over" carries no properties, and paho 2.1 drops a DISCONNECT reason that has none)
            hint = (f"; the broker closed the connection {lived:.0f}s after accepting it (a packet over its maximum looks like this, "
                    f"and so does another client connecting with the client id {self.client_id}: a second container "
                    "running the same integration on this broker)"
                    if lived is not None and lived < DROP_AFTER_CONNECT_S
                    else "" if lived is not None
                    else "; if the port is a TLS listener, turn TLS on" if not self.config.tls and reason == "Unspecified error" else "")
            self.stats["connect_error"] = f"disconnected ({reason}){hint}; reconnecting"
            if reason != self._last_disconnect:  # one warning and one event per reason, not one per retry
                self._last_disconnect = reason
                _LOGGER.warning("MQTT disconnected (%s)%s; paho will retry", reason, hint)
                events.emit("mqtt", f"disconnected ({reason}){hint}; reconnecting")

    def _cmd_base(self) -> str:
        return f"{self.base_topic}/cmd"

    def _call_base(self) -> str:
        return f"{self.base_topic}/call"

    def _manager_cmd_base(self) -> str:
        return f"{self.base_topic}/manager/cmd"

    def _on_message(self, client, userdata, msg) -> None:
        """Command or service call from the consuming HA (paho thread).
        Anything raised here would end paho's network loop, so nothing may."""
        try:
            self._handle_message(msg)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("MQTT message %s could not be handled: %s", msg.topic, err)

    def _note_cleared(self, topic: str) -> None:
        """Paho's thread: this process just emptied that retained command topic.  Only on an MQTT 3.1.1 session:
        an MQTT 5 subscription carries noLocal, so that clear never comes back, and arming the topic would only
        swallow the next genuine empty command on it (a blanked text entity, an empty notification)."""
        if self._session_v5:
            return
        now = time.monotonic()
        self._cleared_cmds = {t: at for t, at in self._cleared_cmds.items() if now - at < CLEARED_ECHO_WINDOW_S}
        while len(self._cleared_cmds) >= CLEARED_ECHO_MAX:
            del self._cleared_cmds[next(iter(self._cleared_cmds))]  # the oldest
        self._cleared_cmds[topic] = now

    def _clear_of_ours(self, topic: str) -> bool:
        """The empty payload now arriving on `topic` is the clear this process published moments ago.  Once per
        clear: a second empty payload on the same topic is a real (and refused) command, not the echo."""
        at = self._cleared_cmds.pop(topic, None)
        return at is not None and time.monotonic() - at < CLEARED_ECHO_WINDOW_S

    def _handle_message(self, msg) -> None:
        if getattr(msg, "retain", False):
            if not msg.payload:
                return  # a retained command being cleared (MQTT 5 keeps the flag on it): nothing to run, nothing to clear
            # a command published with retain would run again at every
            # (re)subscription: physical effects must never replay.  Left on
            # the broker it would also arrive again at every connect, so the
            # topic is cleared here and not only when our identity moves.
            _LOGGER.warning("MQTT: ignoring retained command on %s (commands must not be retained), clearing it", msg.topic)
            if self._publish(msg.topic, None, qos=1):
                self._note_cleared(msg.topic)  # a clear never sent (an identity move) has no echo to swallow
            return
        if not msg.payload and self._clear_of_ours(msg.topic):
            # our own clear of that retained command, echoed back: an MQTT 3.1.1 subscription has no noLocal, and
            # the broker strips the retain flag on a live delivery, so it arrives looking like a command of ""
            # - which text and notify take as a value (a blanked text entity, an empty notification).
            _LOGGER.debug("MQTT: the clear of the retained command on %s came back to us; ignored", msg.topic)
            return
        manager_prefix = self._manager_cmd_base() + "/"
        if msg.topic.startswith(manager_prefix):
            self._on_manager_command(msg.topic[len(manager_prefix):], msg.payload.decode(errors="replace"))
            return
        call_prefix = self._call_base() + "/"
        if msg.topic.startswith(call_prefix):
            if not msg.payload.strip():  # whitespace alone is no JSON object either
                self._reject_empty_call(msg.topic[len(call_prefix):])
                return
            rest, payload = msg.topic[len(call_prefix):], msg.payload.decode(errors="replace")
            try:
                self._on_call(rest, payload)
            except Exception as err:  # noqa: BLE001 - the caller waits on result/: an answer, never silence
                self._call_crashed(rest, payload, err)
            return
        prefix = self._cmd_base() + "/"
        if not msg.topic.startswith(prefix):
            return
        parts = msg.topic[len(prefix):].split("/")
        if len(parts) != 3:
            return
        domain, object_id, field = parts
        payload = msg.payload.decode(errors="replace")
        if not payload and ((domain, field) not in (("text", "value"), ("notify", "message")) or self._moving):
            # clearing a retained command reaches live subscribers as an empty payload (this process
            # clears retained cmd topics itself when its identity moves): only text/notify take "" as a value
            self._finish(self._remember("cmd", f"{domain}.{object_id}/{field}", ""), "ignored", "empty payload")
            return
        # what is sent to a text entity in password mode (announced as one by discovery) never shows in the history,
        # the status or the log: on any of its topics, since a payload published to a wrong field is the value too
        secret = payload if domain == "text" and payload and self._password_text(f"text.{object_id}") else None
        shown = "***" if secret else payload
        rec = self._remember("cmd", f"{domain}.{object_id}/{field}", shown)
        if problem := _payload_problem(payload):
            # unparsed like an oversized call: a JSON command (siren, alarm) would be read
            # anyway, and the state it sets is retained and re-asserted at every republish
            _LOGGER.warning("MQTT command on %s rejected: payload %s", msg.topic, problem)
            self._finish(rec, "rejected", f"bad payload: {problem}")
            return
        if f"{domain}.{object_id}" not in self._topics:
            self._finish(rec, "rejected", "not an entity this container publishes")
            return
        try:
            mapped = disc.command_to_service(domain, object_id, field, payload)
        except (ValueError, KeyError, OverflowError, RecursionError) as err:
            # the message quotes the payload (float() does, and a KeyError is the payload alone): masked like the payload
            error = _mask_text(f"unknown {field} {err}" if isinstance(err, KeyError) else str(err), MASK_SCAN_CHARS)
            _LOGGER.warning("MQTT command %s=%r rejected: %s", msg.topic, _mask_codes(shown, MASK_SCAN_CHARS), error)
            self._finish(rec, "rejected", error)
            return
        if mapped is None:
            _LOGGER.warning("MQTT command not supported: %s", msg.topic)
            self._finish(rec, "rejected", "not supported")
            return
        svc_domain, service, data = mapped
        rec["what"] = f"{domain}.{object_id}/{field} → {svc_domain}.{service}"
        self.stats["commands"] += 1
        self.stats["last_command"] = f"{msg.topic} = {_mask_codes(shown, MASK_SCAN_CHARS)}"[:140]

        async def _call() -> None:
            recs = [rec]

            def finish(state: str, error: str | None = None) -> None:
                for r in recs:
                    self._finish(r, state, error)

            if svc_domain == "climate" and service == "set_temperature" and ("target_temp_high" in data) != ("target_temp_low" in data):
                # Pairing the first half with the old other bound would turn 20-24 -> 26-28 into 26-24 (rejected)
                # and then 20-28: wait for the second half of the same change first.
                pending = await self._pair_range(data, rec)
                if pending is None:
                    return  # completed the half that was waiting: its call answers this record too
                recs = pending["recs"]
                data.update(pending["data"])
                for other in ("target_temp_low", "target_temp_high"):
                    if other not in data:  # only one bound changed: the other stays as it is
                        st = self.hass.states.get(data["entity_id"])
                        val = st.attributes.get(other) if st else None
                        if val is None:
                            finish("rejected", f"{other} is unknown: a range setpoint needs both bounds")
                            return
                        data[other] = val
                try:
                    low, high = float(data["target_temp_low"]), float(data["target_temp_high"])
                except (TypeError, ValueError):
                    low, high = 0.0, 0.0
                if low > high:
                    finish("rejected", f"low {data['target_temp_low']} is above high {data['target_temp_high']}: send both bounds of the range")
                    return
            if f"{domain}.{object_id}" not in self._topics:
                # _topics was read on paho's thread, before this task was handed to the loop; an exclusion
                # adopted in between (_drop_newly_excluded) empties it, and the operator who excluded the
                # integration must not see one more command reach it.  A generic call reads it here too.
                finish("rejected", "not an entity this container publishes")
                return
            if self._in_flight >= CALLS_IN_FLIGHT_MAX:
                finish("rejected", f"too many calls in progress ({CALLS_IN_FLIGHT_MAX}): try again later")
                return
            # like a service call: the handler keeps running past the timeout, the
            # history shows "timeout" now and "late-ok"/"late-error" when it ends
            task = self.hass.async_create_task(self.hass.services.async_call(svc_domain, service, data, blocking=True))
            self._call_started(task)
            done, _ = await asyncio.wait({task}, timeout=CALL_TIMEOUT_S)
            late = not done
            if late:
                finish("timeout", f"no answer after {CALL_TIMEOUT_S}s (service still running)")
                _LOGGER.warning("MQTT command %s -> %s.%s: no answer after %ss", msg.topic, svc_domain, service, CALL_TIMEOUT_S)
            try:
                await task
                finish("late-ok" if late else "ok")
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                finish("late-error" if late else "error", "cancelled by the service handler")
            except Exception as err:  # noqa: BLE001 - logged, never crashes the loop
                error = f"{type(err).__name__}: {err}"
                if secret:
                    error = error.replace(secret, "***")  # text's own ValueError quotes the value
                # what a third party's exception says is free-form text: the Logs page and the diagnostics
                # zip scrub it on the way out, but `docker logs` does not, and that is what people paste
                _LOGGER.error("MQTT command %s -> %s.%s failed: %s", msg.topic, svc_domain, service, _scrubbed(error))
                finish("late-error" if late else "error", error)

        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(_call()))

    async def _pair_range(self, data: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any] | None:
        """One half of a climate range change: None when it completed a half already waiting (that call
        answers both records), otherwise the pending change after the second half came or the wait ended."""
        eid = data["entity_id"]
        half = {k: v for k, v in data.items() if k in ("target_temp_low", "target_temp_high")}
        waiting = self._range_pending.get(eid)
        if waiting is not None and not waiting["event"].is_set():
            waiting["data"].update(half)  # the same bound twice: the newer value wins
            waiting["recs"].append(rec)
            if "target_temp_low" in waiting["data"] and "target_temp_high" in waiting["data"]:
                waiting["event"].set()
            return None
        pending = {"data": dict(data), "recs": [rec], "event": asyncio.Event()}
        self._range_pending[eid] = pending
        try:
            await asyncio.wait_for(pending["event"].wait(), RANGE_PAIR_WAIT_S)
        except TimeoutError:
            pass
        finally:
            if self._range_pending.get(eid) is pending:
                del self._range_pending[eid]
        return pending

    def _password_text(self, entity_id: str) -> bool:
        return password_text(self.hass, entity_id)

    def _password_value(self, domain: str, service: str, data: dict[str, Any]) -> str | None:
        # paho's thread: a snapshot of the published entities, the loop adds and removes them meanwhile
        return password_value(self.hass, domain, service, data, lambda: list(self._topics))

    def _on_manager_command(self, action: str, payload: str) -> None:
        """<base>/manager/cmd/<action> (paho thread): see manager_device.py."""
        rec = self._remember("manager", action[:40], payload)
        expected = disc.MANAGER_ACTIONS.get(action)
        if expected is None or payload.strip() != expected:
            # an empty payload clearing a retained command reaches live subscribers too: never an action, and no answer
            error = f"unknown action {action[:40]!r}" if expected is None else f"payload must be {expected!r}"
            self._finish(rec, "rejected", error)
            if payload.strip():
                self._answer_rejected(action, error)
            return
        if not self.config.manager_commands:
            self._finish(rec, "rejected", "manager_commands is off")
            if payload.strip():
                self._answer_rejected(action, "manager_commands is off")
            return
        if self.manager is None:
            self._finish(rec, "rejected", "the manager device is not set up")
            return
        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(self.manager.async_action(action, rec)))

    def _answer_rejected(self, action: str, error: str) -> None:
        """Paho thread: the sender of a refused manager command gets told why on <base>/manager/result.  One
        non-retained publish: nothing changed, so the retained manager document is not sent again, and nothing
        waits for the broker (anyone who may publish under the base topic can send these)."""
        c = self._client
        if c is None or not self._connected:
            return
        c.publish(f"{self.base_topic}/manager/result", _dumps({"ok": False, "action": action[:40], "error": error}), qos=1, retain=False)

    def _remember(self, kind: str, what: str, data: Any, call_id: Any = None, unparsable: bool = False) -> dict[str, Any]:
        """data: the text as received, or what it parsed to (masked on its keys, never parsed again); unparsable: text
        that is known not to parse (only the text rule reads it)."""
        if isinstance(data, str):
            # masked before it is cut: a cut JSON document no longer parses, and its escaped keys would show.  paho's
            # network thread runs this before any payload check: the text rule reads MASK_SCAN_CHARS of it at most
            text = data if len(data) <= CALL_MAX_BYTES else data[:MASK_SCAN_CHARS + 1]
            shown = _mask_text(text, MASK_SCAN_CHARS) if unparsable else _mask_codes(text, MASK_SCAN_CHARS)
        else:
            masked, changed = _masked(data, MASK_SCAN_CHARS)
            shown = _mask_text(json.dumps(masked, ensure_ascii=False, default=str) if changed else json.dumps(data, default=str), MASK_SCAN_CHARS)
        rec = {"id": call_id, "kind": kind, "what": what,
               "data": shown[:200],
               "received": time.time(), "finished": None, "duration_ms": None, "state": "running", "error": None, "result": None}
        self.history.append(rec)
        return rec

    @staticmethod
    def _finish(rec: dict[str, Any], state: str, error: str | None = None, result: dict[str, Any] | None = None) -> None:
        now = time.time()
        rec["finished"] = now
        rec["duration_ms"] = int((now - rec["received"]) * 1000)
        rec["state"], rec["error"] = state, error
        if result is not None:
            rec["result"] = result

    def _seen_call(self, key: str | None) -> dict[str, Any] | None:
        """The canonical record of a call with this id inside the dedup window.  Expires old records on every call,
        with an _id or without: a burst of ids followed by calls without one must not keep them all."""
        now = time.time()
        with self._calls_lock:
            for k in [k for k, r in self._calls.items() if now - r["received"] >= DEDUP_WINDOW_S]:
                del self._calls[k]  # expired
            return self._calls.get(key) if key is not None else None

    def _call_started(self, task: asyncio.Task) -> None:
        """A service task counts against CALLS_IN_FLIGHT_MAX until it ends, a timed-out one included."""
        self._in_flight += 1

        def ended(_task: asyncio.Task) -> None:
            self._in_flight -= 1

        task.add_done_callback(ended)

    def recent_commands(self, limit: int = 30) -> list[dict[str, Any]]:
        """The public history: the Commands page, /api/mqtt/commands, the MQTT status document and the
        diagnostics zip read it, and nothing else reads a record's error.  "error" is the one field a third
        party writes freely - a service raising ValueError("authentication failed: password=...") puts its own
        words in it, and _remember masks only what the caller sent - so it is scrubbed here rather than in
        _finish: this is the single place every reader comes through, it already drops "result" (which carries
        the same text), and it runs on the loop, not on paho's network thread.  The log scrubber, not the
        payload one: free text is what it is for, and it is the rule that knows auth schemes
        ("Authorization: Bearer ..."), which the payload rule's name=value form does not reach."""
        from .diagnostics import scrub_text  # deferred: diagnostics imports this module

        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) if t else None
        rows = list(self.history)[-limit:]
        return [{**r, "received": iso(r["received"]), "finished": iso(r["finished"]),
                 "error": scrub_text(r["error"]) if r["error"] else r["error"], "result": None} for r in reversed(rows)]

    def _service_reach(self, domain: str | None, service: str | None) -> set[str] | None:
        """Entity domains an entity service can act on, None when it is not one (or cannot be recognised as
        one).  Home Assistant hands an entity service only its own component's entities, so an area, floor or
        label picking up entities of other domains is not a request to touch them.  Anything unrecognised keeps
        the strict check: a plain service handler may do what it likes with an area_id."""
        from homeassistant.helpers.entity_platform import DATA_DOMAIN_PLATFORM_ENTITIES

        if not domain or not service:
            return None
        registered = self.hass.services.async_services_for_domain(domain).get(service)
        handler = getattr(getattr(registered, "job", None), "target", None)
        if getattr(getattr(handler, "func", handler), "__name__", "") not in _ENTITY_SERVICE_CALLS:
            return None
        # a platform entity service (async_register_platform_entity_service) reaches entities of other domains
        return {domain} | {ed for ed, sd in self.hass.data.get(DATA_DOMAIN_PLATFORM_ENTITIES, {}) if sd == domain}

    def _call_target_problem(self, data: dict[str, Any], domain: str | None = None, service: str | None = None) -> str | None:
        """A call reaches only entities this container publishes, like a cmd/ topic: the target
        (entity ids, and area/floor/label/device ids resolved the way Home Assistant resolves them)
        must not name anything else.  A device_id that is not a registry device (a RAMSES address
        given to send_packet, say) resolves to nothing and stays plain service data.  A group counts
        with its members; entity ids in other fields of the data count too (see _entity_ids_in)."""
        from homeassistant.const import ENTITY_MATCH_ALL
        from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

        ids = data.get("entity_id")
        if ids is not None and not (isinstance(ids, str) or isinstance(ids, list) and all(isinstance(x, str) for x in ids)):
            # dropped below, it would reach the service unchecked (Home Assistant then fails on it with a TypeError)
            return "the target cannot be read (entity_id): entity, device, area, floor and label ids must be strings"
        named = [ids] if isinstance(ids, str) else ids if isinstance(ids, list) else []
        # "a, b" is split by the service schema only after this check: split it the same way first
        split = [p.strip().lower() for x in named if isinstance(x, str) for p in x.split(",") if p.strip()]
        if ENTITY_MATCH_ALL in split:
            return "entity_id all is not accepted over MQTT: name the entities"
        try:
            selection = {**data, "entity_id": split} if "entity_id" in data else data
            selected = async_extract_referenced_entity_ids(self.hass, TargetSelection(selection), expand_group=True)
        except Exception as err:  # noqa: BLE001 - what cannot be resolved here cannot be checked: never passed on unchecked
            return f"the target cannot be read ({type(err).__name__}): entity, device, area, floor and label ids must be strings"
        # expansion replaces a group by its members: the group named must be published too
        wanted = selected.referenced | {e for e in split if valid_entity_id(e)} | _entity_ids_in(data)
        indirect = selected.indirectly_referenced
        if (reach := self._service_reach(domain, service)) is not None:
            # what the service can never act on is no reason to refuse it; ids the caller named stay strict
            indirect = {e for e in indirect if e.split(".", 1)[0] in reach}
        outside = sorted(e for e in wanted | indirect if e not in self._topics)
        if outside:
            return f"not entities this container publishes: {', '.join(outside[:5])}{'…' if len(outside) > 5 else ''}"
        return None

    def _excluded_now(self, entity_id: str) -> bool:
        """Excluded by a rule or by its integration: an entity excluded while this process was down
        still has a retained discovery config and document, which the orphan sweep must remove."""
        if self.rules.for_entity(entity_id).get("exclude"):
            return True
        # like _group_by_device: an entity without a registry entry (YAML platform) belongs to the platform that added it
        integration = platform_of(self.hass, entity_id)
        return bool(integration and self._integration_excluded(integration))

    def _integration_excluded(self, integration: str | None) -> bool:
        """Its entities are not published: excluded in the settings, or created by Home Assistant itself."""
        return integration in self.config.exclude_integrations or integration in NEVER_PUBLISHED_INTEGRATIONS

    def _reject_empty_call(self, rest: str) -> None:
        """A call needs a JSON object ({} without data); an empty payload is what clearing a retained call looks like."""
        parts = [p.lower() for p in rest.split("/")]
        valid = len(parts) == 2 and all(_SERVICE_NAME.fullmatch(p) for p in parts)
        error = "empty payload: send {} to call a service without data"
        self._finish(self._remember("call", ".".join(parts) if valid else rest[:80], ""), "rejected", error)
        if valid and not self._moving:
            domain, service = parts
            # the shape every other result has: a consumer routes on "service" and correlates on "id"
            self._publish_result(domain, service, {"id": None, "service": f"{domain}.{service}", "ok": False, "error": error})

    @staticmethod
    def _internal_error(what: str, err: Exception) -> str:
        """Logged with where it happened but without its message, which may quote the data of the call (a password)."""
        frame = traceback.extract_tb(err.__traceback__)[-1] if err.__traceback__ else None
        where = f" in {frame.name} line {frame.lineno}" if frame else ""
        _LOGGER.error("MQTT call %s could not be handled: %s%s", what, type(err).__name__, where)
        return f"internal error ({type(err).__name__}): see the log of the container"

    def _call_crashed(self, rest: str, payload: str, err: Exception) -> None:
        """Paho thread: _on_call raised."""
        error = self._internal_error(repr(rest[:80]), err)
        try:
            parsed = _loads_call(payload)
        except Exception:  # noqa: BLE001
            parsed = None
        call_id = parsed.get("_id") if isinstance(parsed, dict) else _refused_call_id(payload) if parsed is None else None
        if _call_id_problem(call_id) is not None:
            call_id = _short_call_id(call_id)  # the answer carries it, and so does the history
        parts = [p.lower() for p in rest.split("/")]
        valid = len(parts) == 2 and all(_SERVICE_NAME.fullmatch(p) for p in parts)
        self._finish(self._remember("call", ".".join(parts) if valid else rest[:80], "", call_id), "error", error)
        if valid and not self._moving:
            self._publish_result(parts[0], parts[1], {"id": call_id, "service": ".".join(parts), "ok": False, "error": error})

    def _on_call(self, rest: str, payload: str) -> None:
        """Generic service call: <base>/call/<domain>/<service> with a JSON
        object as payload (service data incl. entity_id/device_id/area_id;
        an optional "_id" is echoed back).  Outcome goes to
        <base>/result/<domain>/<service>, not retained."""
        parts = rest.split("/")
        # parsed once, on paho's thread, and passed along: the refusals below all answer with the id it carries (the
        # command history is only useful to the consumer when a refused call carries the id it sent), and the history
        # masks what it parsed to
        try:
            parsed: Any = _loads_call(payload)
            bad = None if isinstance(parsed, dict) else "payload must be a JSON object"
        except (ValueError, RecursionError) as err:
            parsed, bad = None, str(err)
        sent_id = parsed.get("_id") if isinstance(parsed, dict) else _refused_call_id(payload) if parsed is None else None
        # nothing below this line handles the id at full size: every refusal answers with it, and the history keeps it
        id_problem = _call_id_problem(sent_id)
        if id_problem is not None:
            sent_id = _short_call_id(sent_id)
        # read before anything is remembered, so a refused call masks the value too: a text.set_value that does not
        # parse cannot tell which entity it was for, and is kept masked whole
        set_value = len(parts) == 2 and (parts[0].lower(), parts[1].lower()) == ("text", "set_value")
        secret = self._password_value("text", "set_value", parsed) if set_value and isinstance(parsed, dict) else None

        def remember(what: str) -> dict[str, Any]:
            if secret:
                return self._remember("call", what, {**parsed, "value": "***"}, sent_id)
            if isinstance(parsed, (dict, list)):
                return self._remember("call", what, parsed, sent_id)
            if set_value and parsed is None:
                return self._remember("call", what, "***", sent_id, unparsable=True)
            return self._remember("call", what, payload, sent_id, unparsable=bad is not None and parsed is None)

        if len(parts) != 2:
            self._finish(remember(rest[:80]), "rejected", "topic must be <base>/call/<domain>/<service>")
            _LOGGER.warning("MQTT call on %r ignored: the topic must be <base>/call/<domain>/<service>", rest[:80])
            return
        domain, service = parts[0].lower(), parts[1].lower()  # HA looks services up in lower case, so the deny list must too
        if not _SERVICE_NAME.fullmatch(domain) or not _SERVICE_NAME.fullmatch(service):
            self._finish(remember(rest[:80]), "rejected", "domain and service must be names made of a-z, 0-9 and _")
            return
        denied = (f"domain {domain} is not callable over MQTT" if domain in MQTT_CALL_DENY_DOMAINS or domain in self.config.exclude_integrations
                  else f"{domain}.{service} is not callable over MQTT" if (domain, service) in MQTT_CALL_DENY_SERVICES else None)
        if denied:
            self._publish_result(domain, service, {"id": sent_id, "service": f"{domain}.{service}", "ok": False, "error": denied})
            self._finish(remember(f"{domain}.{service}"), "rejected", denied)
            return
        if id_problem is not None:
            self._publish_result(domain, service, {"id": sent_id, "service": f"{domain}.{service}", "ok": False, "error": id_problem})
            self._finish(remember(f"{domain}.{service}"), "rejected", id_problem)
            _LOGGER.warning("MQTT call %s.%s refused: %s", domain, service, id_problem)
            return
        if bad is not None:
            self._publish_result(domain, service, {"id": sent_id, "service": f"{domain}.{service}", "ok": False, "error": f"bad payload: {bad}"})
            self._finish(remember(f"{domain}.{service}"), "rejected", f"bad payload: {bad}")
            return
        data: dict[str, Any] = parsed
        call_id = data.pop("_id", None)
        shown = {**data, "value": "***"} if secret else data
        # an _id is unique per service for the consumer (a counter that restarts, one per automation)
        call_key = _call_key(domain, service, call_id) if call_id not in (None, "") else None
        prior = self._seen_call(call_key)
        if prior is not None:
            # A retry of the same _id (the consumer did not see the result in
            # time): answer from history, never run the service twice.
            dup = self._remember("call", f"{domain}.{service}", shown, call_id)
            if prior["state"] == "running":
                self._publish_result(domain, service, {"id": call_id, "service": f"{domain}.{service}", "ok": None, "state": "running", "duplicate": True})
                self._finish(dup, "duplicate", "still running")
            else:
                self._publish_result(domain, service, {**(prior.get("result") or {"id": call_id, "service": f"{domain}.{service}", "ok": None}), "duplicate": True})
                self._finish(dup, "duplicate", f"answered from history ({prior['state']})")
            _LOGGER.info("MQTT call %s.%s id=%s repeated: answered from history (%s)", domain, service, call_id, prior["state"])
            return
        rec = self._remember("call", f"{domain}.{service}", shown, call_id)
        # the canonical record, duplicates never replace it: only what their answer needs (not the data)
        seen: dict[str, Any] | None = None
        if call_key is not None:
            seen = {"received": rec["received"], "state": "running", "result": None}
            with self._calls_lock:
                self._calls[call_key] = seen
                while len(self._calls) > CALLS_REMEMBERED:
                    del self._calls[next(iter(self._calls))]  # the oldest _id
        self.stats["calls"] += 1
        self.stats["last_call"] = f"{domain}.{service} {rec['data']}"[:140]

        def done(state: str, error: str | None, res: dict[str, Any]) -> None:
            if secret and error:
                # the result published to the caller keeps the service's own words: the caller sent the value
                error = error.replace(secret, "***")
                self._finish(rec, state, error, {**res, "error": error})
            else:
                self._finish(rec, state, error, res)
            if seen is not None and state != "timeout":
                # timed out, the service still runs: a repeat of the _id is answered "running" until it ends
                seen["state"], seen["result"] = state, res

        def forget() -> None:
            """A refusal that happens BEFORE async_call: the service never ran, so the _id must not be
            remembered.  An integration still loading answers "unknown service" for a moment, and a retry of
            that same _id within DEDUP_WINDOW_S would be answered from history instead of being run."""
            with self._calls_lock:  # paho's thread iterates the dict
                if seen is not None and self._calls.get(call_key) is seen:
                    del self._calls[call_key]

        async def _call() -> None:
            try:
                await _run()
            except Exception as err:  # noqa: BLE001 - the caller waits on result/: an answer, never silence
                # the _id is NOT forgotten here: an internal error is not a refusal, and a repeat of it is
                # answered from that record rather than sent down the same broken path again
                res = {"id": call_id, "service": f"{domain}.{service}", "ok": False, "error": self._internal_error(f"{domain}.{service}", err)}
                done("error", res["error"], res)
                self._publish_result(domain, service, res)

        async def _run() -> None:
            base: dict[str, Any] = {"id": call_id, "service": f"{domain}.{service}"}
            if not self.hass.services.has_service(domain, service):
                res = {**base, "ok": False, "error": f"unknown service {domain}.{service}"}
                self._publish_result(domain, service, res)
                done("error", res["error"], res)
                forget()  # an integration that is still loading: the retry must run, not be answered from history
                _LOGGER.warning("MQTT call %s.%s failed: unknown service", domain, service)
                return
            if problem := self._call_target_problem(data, domain, service):
                res = {**base, "ok": False, "error": problem}
                self._publish_result(domain, service, res)
                done("rejected", problem, res)
                forget()  # an entity that has not been added yet: the same _id may be sent again
                _LOGGER.warning("MQTT call %s.%s refused: %s", domain, service, problem)
                return
            if self._in_flight >= CALLS_IN_FLIGHT_MAX:
                res = {**base, "ok": False, "error": f"too many calls in progress ({CALLS_IN_FLIGHT_MAX}): try again later"}
                self._publish_result(domain, service, res)
                done("rejected", res["error"], res)
                forget()  # never ran: a retry with the same _id runs once there is room
                _LOGGER.warning("MQTT call %s.%s refused: %s calls in progress", domain, service, self._in_flight)
                return
            wants = self.hass.services.supports_response(domain, service) != SupportsResponse.NONE
            _LOGGER.debug("MQTT call %s.%s start (response=%s) data=%s", domain, service, wants, _mask_codes(json.dumps(shown, default=str)))
            # Not wait_for(): cancelling a service handler that shields or
            # swallows CancelledError would hang the timeout itself.  The
            # call keeps running; the caller gets a timeout now and the real
            # outcome later, flagged "late".
            task = self.hass.async_create_task(
                self.hass.services.async_call(domain, service, data, blocking=True, return_response=wants)
            )
            self._call_started(task)
            finished, _ = await asyncio.wait({task}, timeout=CALL_TIMEOUT_S)
            late = not finished
            _LOGGER.debug("MQTT call %s.%s wait returned, done=%s", domain, service, bool(finished))
            if late:
                res = {**base, "ok": False, "error": f"timeout after {CALL_TIMEOUT_S}s (service still running)"}
                self._publish_result(domain, service, res)
                done("timeout", res["error"], res)
                _LOGGER.warning("MQTT call %s.%s timed out after %ss", domain, service, CALL_TIMEOUT_S)
            try:
                resp = await task
                result = {**base, "ok": True}
                if wants:
                    result["response"] = resp
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # The handler's own task was cancelled (some integrations
                # cancel long RF requests they cannot send); report, don't
                # propagate into our task.
                result = {**base, "ok": False, "error": "cancelled by the service handler"}
                _LOGGER.warning("MQTT call %s.%s was cancelled by the handler", domain, service)
            except Exception as err:  # noqa: BLE001 - reported to the caller
                result = {**base, "ok": False, "error": f"{type(err).__name__}: {err}"}
                _LOGGER.warning("MQTT call %s.%s failed: %s", domain, service,
                                _scrubbed(str(err).replace(secret, "***") if secret else str(err)))
            if late:
                result["late"] = True
            self._publish_result(domain, service, result)
            done(("late-ok" if result.get("ok") else "late-error") if late else ("ok" if result.get("ok") else "error"),
                 result.get("error"), result)

        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(_call()))

    def _publish_result(self, domain: str, service: str, result: dict[str, Any]) -> None:
        c = self._client
        if c is None or not self._connected:
            return
        topic, payload = f"{self.base_topic}/result/{domain}/{service}", _dumps(result)
        if self._oversized(topic, payload):
            # the caller is waiting for an answer on this topic: send it without the response data rather than nothing
            payload = _dumps({"id": result.get("id"), "service": result.get("service"), "ok": False,
                              "error": f"the result is over the {self._publish_limit()} byte maximum the broker accepts"})
            if self._oversized(topic, payload):
                return
        info = c.publish(topic, payload, qos=1, retain=False)
        _LOGGER.debug("MQTT result %s.%s published rc=%s ok=%s", domain, service, info.rc, result.get("ok"))

    def _publish_if_changed(self, topic: str, payload: str, qos: int | None = None) -> bool:
        """Retained payloads that carry no timestamp (discovery configs, the
        service catalog): skip when identical to what was last published on
        this connection (the hash map is cleared on every disconnect, so a
        reconnect still re-asserts everything).  Returns True when the
        broker holds the current payload."""
        digest = hashlib.sha1(payload.encode()).hexdigest()
        if self._last_hash.get(topic) == digest:
            self.stats["unchanged_skipped"] += 1
            return True
        if self._publish(topic, payload, qos=qos):
            self._last_hash[topic] = digest
            return True
        return False

    def _publish_limit(self) -> int:
        """Bytes a single packet may have: what the broker announced on connect, else our own default."""
        return self._broker_max_packet or PUBLISH_MAX_BYTES

    def _oversized(self, topic: str, payload: str) -> bool:
        """A packet the broker refuses costs the connection, and with QoS 1 paho replays it on every
        reconnect until a new client is built: skipping the document is the cheaper failure."""
        size = len(payload.encode()) + len(topic.encode()) + PUBLISH_OVERHEAD_BYTES
        limit = self._publish_limit()
        if size <= limit:
            return False
        self.stats["oversized_skipped"] += 1
        self.stats["last_oversized"] = f"{topic}: {size} bytes over the {limit} byte maximum"
        if topic not in self._oversized_warned:  # once per topic per connection, not once per republish
            self._oversized_warned.add(topic)
            _LOGGER.error("MQTT: %s not published: %s bytes, over the %s byte maximum the broker accepts", topic, size, limit)
            events.emit("mqtt", f"{topic} not published: {size} bytes, over the {limit} byte maximum")
        return True

    def _publish(self, topic: str, payload: str | None, retain: bool = True, qos: int | None = None) -> bool:
        c = self._client
        if c is None or not self._connected or self._moving:
            return False
        if payload is not None and self._oversized(topic, payload):
            return False
        if payload is None:
            # a cleared retained topic must be published again next time, even
            # with content identical to what was there (device gone and back,
            # service catalog of a domain that returns)
            self._last_hash.pop(topic, None)
        info = c.publish(topic, payload if payload is not None else "", qos=self.config.qos if qos is None else qos, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # ----- entity documents -----------------------------------------------

    def _topic_for(self, entity_id: str, integration: str) -> str:
        domain, object_id = entity_id.split(".", 1)
        segment = f"{integration}-integration" if integration in RESERVED_TOPIC_SEGMENTS else integration
        return f"{self.base_topic}/{segment}/{domain}/{object_id}"

    def _integration_of(self, entity_id: str) -> str | None:
        return platform_of(self.hass, entity_id)

    def build_document(self, state: State) -> tuple[str, dict[str, Any]] | None:
        """Return (topic, document) or None if the entity is excluded."""
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(state.entity_id)
        integration = platform_of(self.hass, state.entity_id) or "unregistered"
        if self._integration_excluded(integration):
            return None
        rule = self.rules.for_entity(state.entity_id)
        if rule.get("exclude"):
            return None

        domain, object_id = state.entity_id.split(".", 1)
        doc: dict[str, Any] = {
            "entity_id": state.entity_id,
            "domain": domain,
            "object_id": object_id,
            "integration": integration,
            "state": _published_state(state.state),
            "attributes": _published_attributes(state.attributes),
            "last_changed": state.last_changed.isoformat(),
            "last_updated": state.last_updated.isoformat(),
            # moves on every write by the integration, value changed or not; picked
            # up by the incremental pass (reports fire no state_changed event)
            "last_reported": state.last_reported.isoformat(),
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **disc.document_extras(state),
        }
        if entry:
            doc.update(
                {
                    "unique_id": entry.unique_id,
                    "name": entry.name or entry.original_name,
                    "original_name": entry.original_name,
                    "device_class": entry.device_class or entry.original_device_class,
                    "unit_of_measurement": entry.unit_of_measurement,
                    "icon": entry.icon or entry.original_icon,
                    "entity_category": entry.entity_category.value if entry.entity_category else None,
                    "disabled": entry.disabled,
                    "hidden": entry.hidden,
                    "area_id": entry.area_id,
                    "labels": sorted(entry.labels),
                    "config_entry_id": entry.config_entry_id,
                    "translation_key": entry.translation_key,
                }
            )
            if entry.device_id:
                dev = dr.async_get(self.hass).async_get(entry.device_id)
                if dev and disc.is_child_device(dev):
                    parent = dr.async_get(self.hass).async_get(dev.parent_device_id)
                    doc["device"] = {"id": dev.id, "name": dev.name_by_user or dev.name, "child_of": dev.parent_device_id,
                                     "parent_name": (parent.name_by_user or parent.name) if parent else None}
                elif dev:
                    doc["device"] = {
                        "id": dev.id,
                        "name": dev.name_by_user or dev.name,
                        "manufacturer": dev.manufacturer,
                        "model": dev.model,
                        "sw_version": dev.sw_version,
                        "identifiers": [list(i) for i in dev.identifiers],
                        "via_device_id": dev.via_device_id,
                        "area_id": dev.area_id,
                    }
        if rule:
            doc["mqtt_rule"] = rule
            if rule.get("name"):
                doc["name"] = rule["name"]
        return self._topic_for(state.entity_id, integration), doc

    def _publish_state(self, state: State, is_event: bool = False, force: bool = True) -> bool:
        built = self.build_document(state)
        if built is None:
            return False
        topic, doc = built
        self._topics[state.entity_id] = topic
        digest = hashlib.sha1(_dumps({k: v for k, v in doc.items() if k != "published_at"},
                                         sort_keys=True).encode()).hexdigest()
        if not force and self._last_hash.get(topic) == digest:
            self.stats["unchanged_skipped"] += 1
            return False
        payload = _dumps(doc)
        if not self._publish(topic, payload):
            return False
        self._last_hash[topic] = digest
        if is_event and doc["domain"] == "event":
            # Real occurrence: also emit on the non-retained stream the
            # discovered event entity listens to (a retained doc would
            # replay the last event on every reconnect).
            self._publish(disc.event_stream_topic(topic), payload, retain=False)
        self.stats["published"] += 1
        self.stats["last_publish"] = doc["published_at"]
        return True

    # ----- HA MQTT discovery (device-based) ---------------------------------

    def _discovery_topic(self, discovery_id: str) -> str:
        return f"{self.config.discovery_prefix}/device/{discovery_id}/config"

    def _group_by_device(self) -> tuple[dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]], dict[str, int]]:
        """Return {discovery_id: (device_block, {entity_id: component})} and
        counters.  Every entity gets a component: native MQTT platform where
        one exists, a read-only sensor mirror otherwise; registry entries
        without a state (disabled) are included as enabled_by_default=false."""
        ent_reg = er.async_get(self.hass)
        groups: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] = {}
        counts = {"mirrored": 0, "disabled": 0, "collisions": 0, "default_id_duplicates": 0,
                  "compat_device_classes": 0, "compat_platforms": 0}
        # None unless main_ha_version is set: then the payload is byte-for-byte what it was before the setting existed
        compat = disc.compat_for(self.config.main_ha_version)
        seen: set[str] = set()
        keys: dict[tuple[str, str], str] = {}  # (discovery id, component key) -> the entity that has it
        defaults: dict[str, str] = {}  # default_entity_id -> the first entity that asks for it

        def add(disc_id: str, block: dict[str, Any], entity_id: str, comp: dict[str, Any]) -> None:
            # The component key replaces the first "." with "_": image_processing.x and image.processing_x
            # share one key, and the second would silently overwrite the first in the device config.
            # Keys stay as they are (changing them would recreate every entity on the main HA): the second is skipped.
            owner = keys.setdefault((disc_id, _comp_key(entity_id)), entity_id)
            if owner != entity_id:
                counts["collisions"] += 1
                if entity_id not in self._collision_warned:
                    self._collision_warned.add(entity_id)
                    _LOGGER.warning("MQTT discovery: %s skipped, its component key %s is already used by %s on the same device",
                                    entity_id, _comp_key(entity_id), owner)
                return
            # A mirror (camera.front -> sensor.camera_front) can ask for the id of a real entity: both are announced,
            # the main HA gives the second one a _2 suffix, so say which
            wanted = comp.get("default_entity_id")
            first = defaults.setdefault(wanted, entity_id) if wanted else entity_id
            if first != entity_id:
                counts["default_id_duplicates"] += 1
                if entity_id not in self._default_id_warned:
                    self._default_id_warned.add(entity_id)
                    _LOGGER.warning("MQTT discovery: %s asks for entity id %s like %s: the main Home Assistant will give one of them a _2 suffix",
                                    entity_id, wanted, first)
            groups.setdefault(disc_id, (block, {}))[1][entity_id] = comp
        for state in self.hass.states.async_all():
            seen.add(state.entity_id)
            entry = ent_reg.async_get(state.entity_id)
            integration = platform_of(self.hass, state.entity_id) or "unregistered"
            if self._integration_excluded(integration):
                continue
            rule = self.rules.for_entity(state.entity_id)
            if rule.get("exclude"):
                continue
            doc_topic = self._topic_for(state.entity_id, integration)
            try:
                comp = self.rules.apply_component(disc.build_component(self.hass, state, doc_topic, self._cmd_base(), self.prefix, compat), rule)
            except Exception as err:  # noqa: BLE001 - one bad attribute must not drop discovery of every device
                _LOGGER.warning("MQTT discovery: %s skipped: %s", state.entity_id, err)
                continue
            if comp["platform"] != state.domain:
                counts["mirrored"] += 1
            disc_id, block = disc.device_block(self.hass, entry.device_id if entry else None, integration, self.prefix)
            add(disc_id, block, state.entity_id, comp)
        loaded = set(self.hass.config.components)
        for entry in list(ent_reg.entities.values()):
            if entry.entity_id in seen or self._integration_excluded(entry.platform):
                continue
            if entry.platform not in loaded:
                continue  # an installed-but-stopped integration's entries are not announced under this identity
            rule = self.rules.for_entity(entry.entity_id)
            if rule.get("exclude"):
                continue
            doc_topic = self._topic_for(entry.entity_id, entry.platform)
            try:
                comp = self.rules.apply_component(disc.build_component_from_entry(self.hass, entry, doc_topic, self._cmd_base(), self.prefix, compat), rule)
            except Exception as err:  # noqa: BLE001 - one bad attribute must not drop discovery of every device
                _LOGGER.warning("MQTT discovery: %s skipped: %s", entry.entity_id, err)
                continue
            if entry.disabled:
                counts["disabled"] += 1
            if comp["platform"] != entry.domain:
                counts["mirrored"] += 1
            disc_id, block = disc.device_block(self.hass, entry.device_id, entry.platform, self.prefix)
            add(disc_id, block, entry.entity_id, comp)
        if compat is not None:
            counts["compat_device_classes"] = compat.device_class_drops
            counts["compat_platforms"] = compat.platform_drops
            self._warn_compat(compat)
        return groups, counts

    def _warn_compat(self, compat: disc.Compat) -> None:
        """Once per thing left out: the setting is silent otherwise, and an
        operator who declared the wrong version would never see why an entity
        lost its device class or arrived as a sensor."""
        for key, n in sorted(compat.dropped_device_classes.items()):
            if key in self._compat_warned:
                continue
            self._compat_warned.add(key)
            _LOGGER.info("MQTT discovery: device class %s is left out of %s entity/entities: Home Assistant %s "
                         "(main_ha_version) does not know it", key, n, self.config.main_ha_version)
        for domain, n in sorted(compat.mirrored_platforms.items()):
            if domain in self._compat_warned:
                continue
            self._compat_warned.add(domain)
            _LOGGER.info("MQTT discovery: %s %s entity/entities are announced as sensors: Home Assistant %s "
                         "(main_ha_version) has no MQTT %s platform", n, domain, self.config.main_ha_version, domain)

    def _rule_components(self, pattern: str) -> list[tuple[str, dict[str, Any]]]:
        """(entity_id, component as announced without rules) of the entities a rule pattern matches now."""
        ent_reg = er.async_get(self.hass)
        compat = disc.compat_for(self.config.main_ha_version)
        out, seen = [], set()
        for state in self.hass.states.async_all():
            seen.add(state.entity_id)
            if not matches(pattern, state.entity_id):
                continue
            integration = platform_of(self.hass, state.entity_id) or "unregistered"
            if self._integration_excluded(integration):
                continue
            try:
                out.append((state.entity_id, disc.build_component(self.hass, state, self._topic_for(state.entity_id, integration),
                                                                  self._cmd_base(), self.prefix, compat)))
            except Exception:  # noqa: BLE001 - not announced at all (see _group_by_device): nothing to fit
                continue
        for entry in list(ent_reg.entities.values()):
            if entry.entity_id in seen or not matches(pattern, entry.entity_id) or self._integration_excluded(entry.platform):
                continue
            try:
                out.append((entry.entity_id, disc.build_component_from_entry(self.hass, entry, self._topic_for(entry.entity_id, entry.platform),
                                                                             self._cmd_base(), self.prefix, compat)))
            except Exception:  # noqa: BLE001
                continue
        return out

    def _announced_groups(self) -> tuple[dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]], dict[str, int]]:
        """_group_by_device plus the manager device, with via_device only towards devices that are announced too
        (the consumer would create a nameless stub otherwise)."""
        groups, counts = self._group_by_device()
        hid, hblock, hcomps = self._manager_discovery()
        groups[hid] = (hblock, hcomps)
        for block, _comps in groups.values():
            if block.get("via_device") and block["via_device"] not in groups:
                block.pop("via_device", None)
        return groups, counts

    def discovery_preview(self) -> list[dict[str, Any]]:
        """What would be (or is) published as discovery, for the UI/API."""
        groups, _counts = self._announced_groups()
        return [
            {"discovery_id": disc_id, "topic": self._discovery_topic(disc_id), "device": block,
             "components": {eid: comp for eid, comp in comps.items()}}
            for disc_id, (block, comps) in groups.items()
        ]

    def _publish_device_discovery(self, discovery_id: str, block: dict[str, Any], comps: dict[str, dict[str, Any]],
                                  removed: dict[str, str] | None = None) -> None:
        payload = {
            "device": block,
            "origin": disc.origin(self.prefix),
            "payload_available": "online",
            "payload_not_available": "offline",
            # keyed by "<domain>_<object_id>": object ids alone collide across
            # domains (switch.x + light.x on one device)
            "components": {_comp_key(eid): comp for eid, comp in comps.items()},
        }
        # announced before this start and not (yet) here: kept until the orphan sweep decides
        # (an entity still setting up comes back; one a restore took away gets its removal form)
        for key, comp in self._boot_carried(discovery_id).items():
            payload["components"].setdefault(key, comp)
        # Entities that were in this device last time and are gone now must
        # be sent once in HA's removal form, otherwise the consumer keeps them.
        gone_ids = set(self._discovery_map.get(discovery_id, {})) - set(comps)
        for gone in gone_ids:
            # the platform we PUBLISHED (a mirrored media_player is a "sensor"), or HA rejects the whole device
            payload["components"][_comp_key(gone)] = {"platform": self._discovery_map[discovery_id][gone].get("platform", gone.split(".", 1)[0])}
        if self._orphan_sweep_due:
            self._boot_removed -= {(discovery_id, _comp_key(eid)) for eid in comps}  # back (e.g. renamed back): announced again
        self._note_boot_removed(discovery_id, gone_ids)
        for key, platform in (removed or {}).items():  # retained by an earlier process, gone before this one started
            payload["components"].setdefault(key, {"platform": platform})
        topic = self._discovery_topic(discovery_id)
        if self._publish_if_changed(topic, _dumps(payload), qos=1):
            self._discovery_map[discovery_id] = comps
            self._blocks[discovery_id] = block
        # not published (disconnected/moving): the map keeps the old components,
        # so the next full republish computes the removal forms again

    def _boot_carried(self, discovery_id: str, without=()) -> dict[str, dict[str, Any]]:
        """Components an earlier process announced for this device that this process has not removed: entities that
        are still setting up here.  They count as entities of the device, so the last one this process has going away
        must not clear its config (that would take them off the consumer until they finish setting up).  `without`
        are entities that are going away now: they did set up here, so nothing is carried for them."""
        if not (self._orphan_sweep_due and self._boot_components):
            return {}
        dropped = {_comp_key(entity_id) for entity_id in without}
        return {key: comp for key, comp in self._boot_components.get(discovery_id, {}).items()
                if key not in dropped and (discovery_id, key) not in self._boot_removed}

    def _note_boot_removed(self, discovery_id: str, entity_ids) -> None:
        """Removed from the consumer by this process (removal forms, or the device config cleared): until the orphan
        sweep, what an earlier process announced for them is not carried in that device's configs any more."""
        if self._orphan_sweep_due:
            self._boot_removed |= {(discovery_id, _comp_key(eid)) for eid in entity_ids}

    def _publish_discovery_all(self, follow_up: bool = True) -> None:
        if not self.config.discovery_enabled:
            return  # e.g. a delayed republish that lands after an undo
        groups, counts = self._announced_groups()

        def depth(disc_id: str) -> int:  # parents before children
            d, cur = 0, disc_id
            while d < 10 and (nxt := groups[cur][0].get("via_device")) in groups and nxt != cur:
                d, cur = d + 1, nxt
            return d

        # An entity that moved to another device: the parent ignores its unique_id
        # in the new device's config while the old one still owns it, then the
        # old config's removal form deletes it.  So: vanished devices and configs
        # carrying removal forms first, and the devices that took entities over
        # are sent again a moment later.
        prev_owner = {eid: did for did, comps in self._discovery_map.items() for eid in comps}
        moved_in = {did for did, (_b, comps) in groups.items() if any(prev_owner.get(eid) not in (None, did) for eid in comps)}
        with_removals = {did for did in groups if set(self._discovery_map.get(did, {})) - set(groups[did][1])}
        for gone in set(self._discovery_map) - set(groups):
            if self._boot_carried(gone, self._discovery_map.get(gone, ())) and (block := self._blocks.get(gone)) is not None:
                self._publish_device_discovery(gone, block, {})  # entities still setting up: removal forms, not a clear
            elif self._publish(self._discovery_topic(gone), None, qos=1):
                self._note_boot_removed(gone, self._discovery_map.pop(gone))
        for disc_id in sorted(groups, key=lambda d: (d not in with_removals, depth(d))):
            block, comps = groups[disc_id]
            self._publish_device_discovery(disc_id, block, comps)
        if moved_in and self._connected and follow_up:
            for did in moved_in:
                self._last_hash.pop(self._discovery_topic(did), None)
            # once: while a config cannot be published (over the broker's maximum) the map keeps the old owner, and the
            # move shows again in every pass; the next registry refresh or full republish tries again
            self.hass.loop.call_soon_threadsafe(lambda: self.hass.loop.call_later(5, self._publish_discovery_all, False))
        self.stats["discovery_devices"] = len(groups)
        self.stats["discovery_components"] = sum(len(c) for _, c in groups.values())
        self.stats["discovery_mirrored"] = counts["mirrored"]
        self.stats["discovery_collisions"] = counts.get("collisions", 0)
        self.stats["discovery_default_id_duplicates"] = counts.get("default_id_duplicates", 0)
        self.stats["discovery_disabled"] = counts["disabled"]
        self.stats["discovery_compat_device_classes_dropped"] = counts.get("compat_device_classes", 0)
        self.stats["discovery_compat_platforms_mirrored"] = counts.get("compat_platforms", 0)

    def _clear_stale_docs(self) -> int:
        """Blocking: retained entity documents of ours under <base>/ that this
        process no longer publishes (entities a new version dropped)."""
        base = self.base_topic
        try:
            found = self._retained_scan("stale", [(f"{base}/#", 1)])
            live = set(self._topics.values())
            keep_prefixes = (f"{base}/services/", f"{base}/cmd/", f"{base}/call/")
            stale = [t for t, p in found.items()
                     if t not in live and t not in (self._status_topic(), self._health_topic(), self._manager_topic())
                     and not t.startswith(keep_prefixes)
                     and self._is_ours(t, p, base) and b"published_at" in p]
            self._clear_topics("stale", stale)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("stale document cleanup failed: %s", err)
            return 0
        if stale:
            _LOGGER.info("MQTT: cleared %s retained documents of entities that no longer exist", len(stale))
        return len(stale)

    def remove_discovered_component(self, discovery_id: str, entity_id: str, platform: str) -> bool:
        """Tell the consumer to drop ONE component of a device we announce
        (its removal form inside the device config), without touching its
        registry: an entity that vanished here while we were not looking.
        False when that device is not announced now: its other components would have to be announced
        with it, and an empty config instead removes every one of them from the consumer."""
        if not self._connected:
            return False
        groups, _ = self._announced_groups()  # the manager device included: it is announced like any other
        manager_id = f"{self.base_topic}_manager"
        announced = self.config.discovery_enabled or (discovery_id == manager_id and self.config.manager_discovery)
        if not announced:
            return False
        if discovery_id == manager_id and "." not in entity_id:
            # parity derives the entity id from the unique id, and the manager's components are not named after
            # theirs (unique id manager_restart, component key button_<base>_restart): mapped back here, or the
            # removal form goes out under a key the main Home Assistant never saw and removes nothing
            mapped = disc.manager_entity_id(self.base_topic, entity_id)
            if mapped is None:
                return False  # no component of the manager device has that unique id: nothing of ours to remove
            entity_id = mapped
        if discovery_id not in groups:
            # the whole device is gone here: an empty retained config removes it there
            self._last_hash.pop(self._discovery_topic(discovery_id), None)
            return self._publish(self._discovery_topic(discovery_id), None, qos=1)
        block, comps = groups[discovery_id]
        payload = {"device": block, "origin": disc.origin(self.prefix), "payload_available": "online", "payload_not_available": "offline",
                   "components": {_comp_key(eid): comp for eid, comp in comps.items()}}
        payload["components"][_comp_key(entity_id)] = {"platform": platform}
        return self._publish(self._discovery_topic(discovery_id), _dumps(payload), qos=1)

    def _clear_discovery_retained(self) -> int:
        """Blocking: every retained discovery config of THIS identity under
        <prefix>/device/+/config (the consumer removes the entities)."""
        base, prefix = self.base_topic, self.config.discovery_prefix
        try:
            found = self._retained_scan("undisc", [(f"{prefix}/device/+/config", 1)])
            # the manager device stays while manager_discovery wants it: removing it would drop the
            # consumer's customisations of those entities only to announce them again a minute later
            keep = {self._discovery_topic(f"{base}_manager")} if self.config.manager_discovery else set()
            ours = [t for t, p in found.items() if self._is_ours(t, p, base) and t not in keep]
            self._clear_topics("undisc", ours)
            self.stats["discovery_devices"] = len(keep)
            self.stats["discovery_components"] = 0
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("discovery cleanup failed: %s", err)
            raise RuntimeError(f"discovery cleanup failed: {err}") from err  # never report "cleared 0" for a cleanup that did not run
        return len(ours)

    async def async_clear_identity(self, base_topic: str) -> int:
        """Everything retained of ours under one identity (uninstall of that
        integration): documents, services, health, status and its discovery
        configs, so the consumer removes the entities."""
        if not self.config.enabled:
            # MQTT is off: nothing goes out now (the broker may not even take our credentials), what was published before waits
            await self.hass.async_add_executor_job(self._defer_cleanup, base_topic)
            return 0
        prefix, broker = self.config.discovery_prefix, self._broker_identity()
        n, why = await self.hass.async_add_executor_job(self._clear_retained_checked, base_topic, prefix, True, False)  # logged below
        if n is None and base_topic:
            # the broker is unreachable: the uninstall stands, the removal waits for it (a timer, even with nothing installed)
            key = self._pending_key(base_topic, broker)
            known = self._cleanup_pending.get(key)
            since = (known or {}).get("since") or time.strftime("%Y-%m-%dT%H:%M:%S%z")
            await self.hass.async_add_executor_job(self._set_cleanup_pending, key, {"base": base_topic, "prefix": prefix, "broker": broker,
                                                                                   "error": why, "since": since})
            if known is None:  # the uninstall tries twice: one line
                _LOGGER.warning("MQTT: the retained data of the uninstalled %s stays on the broker for now (%s): retried every %s s",
                                base_topic, why, CLEANUP_RETRY_S)
                events.emit("mqtt", f"retained data of the uninstalled {base_topic} not cleared ({why}): retried until the broker takes it")
        if base_topic == self.base_topic:
            self._topics.clear()
            self._last_hash.clear()
            self._discovery_map.clear()
            self._blocks.clear()
        return n or 0

    def _defer_cleanup(self, base: str) -> None:
        """Blocking, MQTT off: the removal of an identity published before MQTT was turned off (the names recorded last) is
        kept as pending on the broker it went to, and runs once MQTT is on with that broker.  An identity never published
        (not the names recorded last) records nothing."""
        last = read_json(self._identity_file(), {}) or {}
        if not base or not isinstance(last, dict) or last.get("base") != base:
            return
        broker = last.get("broker")
        try:
            key = self._pending_key(base, broker)
        except (KeyError, TypeError, ValueError):  # recorded by a version that did not name the broker: the configured one
            broker = self._broker_identity()
            key = self._pending_key(base, broker)
        if key in self._cleanup_pending:
            return  # already waiting (the uninstall tries twice): one line
        self._set_cleanup_pending(key, {"base": base, "prefix": last.get("prefix") or self.config.discovery_prefix, "broker": broker,
                                        "error": "MQTT is disabled", "deferred": True, "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        _LOGGER.warning("MQTT: the retained data of the uninstalled %s stays on %s:%s until MQTT is enabled", base, key[1], key[2])
        events.emit("mqtt", f"retained data of the uninstalled {base} not cleared (MQTT is disabled): removed once MQTT is enabled")

    async def async_clear_discovery(self) -> int:
        n = await self.hass.async_add_executor_job(self._clear_discovery_retained)
        self._discovery_map.clear()
        self._blocks.clear()
        # the configs are gone from the broker: an identical config published
        # later (Enable after Undo) must not be skipped as "unchanged"
        self._forget_hashes(f"{self.config.discovery_prefix}/device/")
        return n

    def _forget_hashes(self, topic_prefix: str) -> None:
        for t in [t for t in list(self._last_hash) if t.startswith(topic_prefix)]:
            del self._last_hash[t]

    async def _async_read_boot_components(self) -> None:
        """What earlier processes announced, before this process's first discovery publish."""
        base, prefix = self.base_topic, self.config.discovery_prefix
        try:
            found = await self.hass.async_add_executor_job(self._retained_scan, "boot", [(f"{prefix}/device/+/config", 1)])
        except Exception as err:  # noqa: BLE001 - without it the sweep still clears documents; nothing is carried
            _LOGGER.warning("MQTT: could not read the discovery configs announced before this start: %s", err)
            self._boot_components = {}
            return
        manager_topic = self._discovery_topic(f"{base}_manager")
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for topic, payload in found.items():
            if topic == manager_topic or not self._is_ours(topic, payload, base):
                continue
            try:
                doc = json.loads(payload)
            except ValueError:
                continue
            comps = doc.get("components") if isinstance(doc, dict) and isinstance(doc.get("components"), dict) else {}
            full = {k: c for k, c in comps.items() if isinstance(c, dict) and c.get("unique_id")}  # removal forms are not carried
            if full:
                out[topic.split("/")[-2]] = full
        self._boot_components = out

    def _entity_gone(self, entity_id: str) -> bool:
        """Neither a state nor a registry entry: an entity still setting up
        (or of an entry retrying) is in the registry and must not be removed."""
        from homeassistant.helpers import entity_registry as er

        return self.hass.states.get(entity_id) is None and er.async_get(self.hass).async_get(entity_id) is None

    async def _async_sweep_orphans(self) -> None:
        """Retained discovery components and entity documents of ours whose
        entity no longer exists here (a restore, an import or a rebuild took it
        away before this process started): removal forms for the components,
        empty retained payloads for the documents and for devices left with
        nothing."""
        base, prefix = self.base_topic, self.config.discovery_prefix
        try:
            found = await self.hass.async_add_executor_job(self._retained_scan, "orphans", [(f"{base}/#", 1), (f"{prefix}/device/+/config", 1)])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MQTT: orphan sweep failed, retried at the next connect: %s", err)
            self._orphan_sweep_due = True
            return
        manager_topic = self._discovery_topic(f"{base}_manager")
        groups, _ = self._group_by_device() if self.config.discovery_enabled else ({}, {})
        live_topics = set(self._topics.values())  # once: the loop below runs over every retained topic
        # an entity announced now under another device (it moved while the container was down): the old config must
        # drop it, or the consumer keeps it there and ignores it in the config of the device it moved to
        owner = {eid: did for did, (_block, comps) in groups.items() for eid in comps}
        moved_to: set[str] = set()
        removed_components, cleared_devices, docs = 0, 0, []
        for topic, payload in found.items():
            if not self._is_ours(topic, payload, base):
                continue
            try:
                doc = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(doc, dict):
                continue
            if topic.startswith(f"{prefix}/device/"):
                if topic == manager_topic or not self.config.discovery_enabled:
                    continue
                comps = doc.get("components") if isinstance(doc.get("components"), dict) else {}
                gone = {key: c.get("platform") for key, c in comps.items()
                        if isinstance(c, dict) and str(c.get("unique_id") or "").startswith(self.prefix)
                        and (self._entity_gone(eid := str(c["unique_id"])[len(self.prefix):]) or self._excluded_now(eid))}
                did = topic.split("/")[-2]
                moved = {key: c.get("platform") for key, c in comps.items()
                         if key not in gone and isinstance(c, dict) and str(c.get("unique_id") or "").startswith(self.prefix)
                         and owner.get(str(c["unique_id"])[len(self.prefix):]) not in (None, did)}
                moved_to |= {owner[str(comps[key]["unique_id"])[len(self.prefix):]] for key in moved}
                gone |= moved
                live = {key for key, c in comps.items() if isinstance(c, dict) and c.get("unique_id") and key not in gone}
                if did not in groups:
                    if gone and not live and self._publish(topic, None, qos=1):
                        cleared_devices += 1
                    elif moved and live:
                        # entities still setting up keep the config: only the moved ones get their removal form
                        doc["components"] = {**comps, **{key: {"platform": p} for key, p in moved.items() if p}}
                        self._last_hash.pop(topic, None)
                        if self._publish(topic, _dumps(doc), qos=1):
                            removed_components += len(moved)
                    continue
                current = {_comp_key(eid) for eid in groups[did][1]}
                extra = {key: platform for key, platform in gone.items() if key not in current and platform}
                if extra:
                    self._last_hash.pop(topic, None)
                    self._publish_device_discovery(did, groups[did][0], groups[did][1], removed=extra)
                    removed_components += len(extra)
            elif "published_at" in doc and topic not in live_topics:
                eid = doc.get("entity_id")
                if isinstance(eid, str) and (self._entity_gone(eid) or self._excluded_now(eid)):
                    docs.append(topic)
        self._boot_components = {}  # decided: from here on configs carry only what exists
        self._boot_removed = set()
        if moved_to:
            # announced again once the old configs have dropped them (as a move seen live, _publish_discovery_all)
            for did in moved_to:
                self._last_hash.pop(self._discovery_topic(did), None)
            self.hass.loop.call_later(5, self._publish_discovery_all, False)
        if docs:
            if removed_components or cleared_devices:
                await asyncio.sleep(2)  # the removal forms reach the consumer before the documents empty (no "Erroneous JSON")
            try:
                await self.hass.async_add_executor_job(self._clear_topics, "orphans", docs)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("MQTT: clearing %s orphan documents failed: %s", len(docs), err)
        if removed_components or cleared_devices or docs:
            _LOGGER.info("MQTT: removed %s orphan components, %s empty devices and %s documents of entities that no longer exist",
                         removed_components, cleared_devices, len(docs))

    async def _async_resync_excluded(self) -> None:
        """Retained documents of excluded integrations, and discovery configs of
        devices no longer announced, swept from the broker (what an exclusion
        while disconnected could not clear)."""
        base, prefix = self.base_topic, self.config.discovery_prefix
        excluded = set(self.config.exclude_integrations) | NEVER_PUBLISHED_INTEGRATIONS
        try:
            found = await self.hass.async_add_executor_job(self._retained_scan, "resync", [(f"{base}/#", 1), (f"{prefix}/device/+/config", 1)])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MQTT: sweep of excluded integrations failed: %s", err)
            self._resync_excluded = True
            return
        docs, configs = [], []
        for topic, payload in found.items():
            if not self._is_ours(topic, payload, base):
                continue
            if topic.startswith(f"{prefix}/device/"):
                configs.append(topic)
                continue
            try:
                doc = json.loads(payload)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("integration") in excluded:
                docs.append(topic)
        if docs:
            await self.hass.async_add_executor_job(self._clear_topics, "resync", docs)
        if self.config.discovery_enabled:
            groups, _ = self._group_by_device()
            for topic in configs:
                if topic.split("/")[-2] not in groups and topic != self._discovery_topic(f"{base}_manager"):
                    self._publish(topic, None, qos=1)
        _LOGGER.info("MQTT: swept %s retained documents of excluded integrations", len(docs))

    async def async_after_start(self, res: dict[str, Any]) -> None:
        """After a successful start (UI or MQTT action): the identity follows
        the running integration; a version switch clears the retained
        documents of entities the new version no longer has (discovery is NOT
        reset: the consumer would delete and recreate every entity)."""
        await self.async_reconnect()
        if res.get("pre_update_backup"):
            res["stale_docs_cleared"] = await self.async_clear_stale_docs()

    CONNECT_GRACE_S = 5.0  # a reconnect is connect_async + loop_start: the CONNACK lands on paho's thread

    async def async_clear_stale_docs(self) -> int:
        # Every caller arrives straight from async_after_start, which reconnects - and _connected only
        # flips in the _on_connect callback, on paho's own thread.  Answering 0 here because the CONNACK
        # has not landed yet meant a version switch never cleared the documents of entities the new
        # version dropped: they stayed on the main Home Assistant until an unrelated republish.  The
        # broker being genuinely down still answers 0, five seconds later.  With MQTT off nothing will connect: no
        # wait, and no warning about a connection nobody asked for.
        if not self.config.enabled:
            return 0
        if not self._connected:
            deadline = time.monotonic() + self.CONNECT_GRACE_S
            while not self._connected and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        if not self._connected:
            _LOGGER.warning("MQTT: stale documents not cleared (no connection within %.0f s)", self.CONNECT_GRACE_S)
            return 0
        await self.async_republish_all()  # so _topics reflects the new version first
        return await self.hass.async_add_executor_job(self._clear_stale_docs)

    # ----- rules ---------------------------------------------------------------

    async def async_apply_rules(self) -> dict[str, int]:
        """After the rules changed: clear the retained documents (and the
        discovery components) of entities that are now excluded, then a full
        republish picks up names/flags."""
        cleared = 0
        for entity_id in list(self._topics):
            if self.rules.for_entity(entity_id).get("exclude"):
                if self.config.discovery_enabled:
                    self._remove_component(entity_id)  # removal first: an empty document before it logs "Erroneous JSON" there
                self._clear(entity_id)
                cleared += 1
        n = await self.async_republish_all()
        return {"cleared": cleared, "republished": n}

    # ----- health ------------------------------------------------------------

    def _health_topic(self) -> str:
        return f"{self.base_topic}/health"

    def _manager_topic(self) -> str:
        return f"{self.base_topic}/manager"

    def build_health(self, grace: bool = True) -> dict[str, Any]:
        """The retained health document: what the installer knows about the
        running integration plus what this process sees of its entities.
        state: ok | degraded | error | stopped."""
        base = dict(self._health_provider() if self._health_provider else {"integration": None, "state": "stopped"})
        domain = base.get("integration")
        now = time.time()
        if domain:
            reg = er.async_get(self.hass)
            ids = {e.entity_id for e in reg.entities.values() if e.platform == domain and not e.disabled}
            # entities without a unique_id (YAML platforms) never reach the
            # registry but do live on the integration's entity platforms
            platforms = async_get_platforms(self.hass, domain)
            for platform in platforms:
                ids.update(platform.entities)
            has_entities = bool(ids) or any(p.entities for p in platforms)
            ids = sorted(ids)
            states = [st for st in (self.hass.states.get(i) for i in ids) if st is not None]
            unavailable = sum(1 for st in states if st.state == "unavailable")
            unknown = sum(1 for st in states if st.state == "unknown")
            last = max((st.last_updated.timestamp() for st in states), default=None)
            # last_reported moves whenever the integration writes a state, even an
            # unchanged value (a message received): a quiet bus with steady values
            # is alive, a silent integration is not
            reported = max((st.last_reported.timestamp() for st in states), default=None)
            base.update({
                "entities": len(ids), "entities_with_state": len(states), "entities_unavailable": unavailable, "entities_unknown": unknown,
                "last_state_update": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(last)) if last else None,
                "last_state_update_age_s": int(now - last) if last else None,
                "last_report": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(reported)) if reported else None,
                "last_report_age_s": int(now - reported) if reported else None,
            })
            rules = self._rules_provider(domain)
            base["rules"] = rules
            booting = grace and now - self._started_at < min(rules["stale_s"], HEALTH_GRACE_S)  # grace: entities fill in after the first traffic
            if base.get("state") == "ok" and booting:
                # ok only because the checks below were skipped: no verdict on the entities (the watchdog reads it so)
                base["grace"] = True
            elif base.get("state") == "ok":
                # opted in ("updated"): a coordinator that re-writes the same states on a dead source keeps
                # last_reported moving; only a value (or attribute) that changes counts as fresh data
                updated = rules.get("stale_basis") == "updated"
                if states and unavailable * 100 >= len(states) * rules["unavailable_pct"]:
                    base["state"], base["reason"] = "degraded", f"{unavailable} of {len(states)} entities unavailable"
                elif rules["mode"] == "periodic" and updated and last and now - last > rules["stale_s"]:
                    base["state"], base["reason"] = "degraded", f"no entity value change for {int(now - last)} s"
                elif rules["mode"] == "periodic" and not updated and reported and now - reported > rules["stale_s"]:
                    base["state"], base["reason"] = "degraded", f"no entity report for {int(now - reported)} s"
                elif not states and has_entities:
                    base["state"], base["reason"] = "degraded", "no entities with a state yet"
                # an integration that exposes no entities at all (services only,
                # a hub without platforms) is judged by its config entries only
        base.update({
            "ha_version": ha_version_str,
            "manager_uptime_s": int(now - self._started_at),
            "mqtt_published": self.stats.get("published", 0),
            "notifications": _notification_count(self.hass),
            "base_topic": self.base_topic if (self._live_base or self.wanted_base_topic) else None,  # never "hass_none"
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        return base

    def publish_health(self) -> dict[str, Any]:
        doc = self.build_health()
        self._health_last = doc
        self.stats["health_state"] = doc.get("state")
        if self.hass.state is not CoreState.running:
            # booting: the integration is still being set up, and its "error" would flip the consumer's
            # connectivity and the timeline for a moment; the retained verdict of the last run stays
            # until Home Assistant has started (then _on_started publishes at once)
            return doc
        prev, state = self._health_announced, doc.get("state")
        if prev is None or state != prev:
            self._health_since = doc["updated_at"]
            reason = f": {doc.get('reason')}" if doc.get("reason") else ""
            if prev is not None:
                events.emit("health", f"{prev} → {state}{reason}", integration=doc.get("integration"))
            # once per transition (the timer republishes the same verdict every minute)
            if state in ("degraded", "error"):
                _LOGGER.warning("health: %s is %s%s", doc.get("integration") or "the integration", state, reason)
            elif state == "ok" and prev in ("degraded", "error"):
                _LOGGER.info("health: %s is ok again (was %s)", doc.get("integration") or "the integration", prev)
        doc["since"] = self._health_since
        self._health_announced = state
        if self._connected:
            self._publish(self._health_topic(), _dumps(doc))
            self.stats["health_published"] = doc["updated_at"]
        return doc

    @callback
    def _on_started(self, _event: Event) -> None:
        self._health_soon()
        self.hass.loop.call_later(ORPHAN_SWEEP_DELAY_S, lambda: self.hass.async_create_task(self._async_orphan_sweep_if_due()))

    async def _async_orphan_sweep_if_due(self) -> None:
        if self._orphan_sweep_due and self._connected and not self._moving:
            self._orphan_sweep_due = False
            await self._async_sweep_orphans()

    @callback
    def _on_entry_changed(self, _change: Any, entry: Any) -> None:
        if entry.domain != "integration_manager":
            self._health_soon()

    @callback
    def _on_component_loaded(self, event: Event) -> None:
        if event.data.get("component") not in ("integration_manager", "persistent_notification"):
            self._health_soon()

    @callback
    def _health_soon(self) -> None:
        """A burst of entry state changes (not_loaded -> setup_in_progress -> loaded) gives one publication."""
        if self._health_soon_handle is not None:
            self._health_soon_handle.cancel()
        self._health_soon_handle = self.hass.loop.call_later(2, self.publish_health)

    async def _on_health_timer(self, _now) -> None:
        self.publish_health()  # first: a resource sample that fails or hangs must not hold the verdict back
        if self.manager is not None:
            try:
                await self.manager.async_sample()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("resource sample failed: %s", err)
        self.publish_manager()
        self._publish_manager_discovery()

    def publish_manager(self) -> None:
        if self.manager is not None and self._connected and not self._moving:
            self._publish(self._manager_topic(), _dumps(self.manager.document()))

    async def async_publish_manager_result(self, result: dict[str, Any]) -> None:
        """The outcome of a manager action, delivered to the broker before
        whatever comes next (a reconnect, a restart) can drop it."""
        c = self._client
        if c is None or not self._connected:
            return
        info = c.publish(f"{self.base_topic}/manager/result", _dumps(result), qos=1, retain=False)
        # the answer (not retained) goes to whoever asked, under the names they used; the retained document does not
        # go out while an identity move sweeps those names, or the sweep's work is undone
        doc = (c.publish(self._manager_topic(), _dumps(self.manager.document()), qos=1, retain=True)
               if self.manager and not self._moving else None)
        # paho already holds both messages, so delivery does not depend on this wait; the executor job does,
        # and a wedged pool would hang the action's task for good, which is how a restart came to answer "ok"
        # and never happen. The wait is bounded here, not only inside wait_for_publish.
        waited = self.hass.async_add_executor_job(
            lambda: [i.wait_for_publish(3) for i in (info, doc) if i is not None])
        done, _ = await asyncio.wait({waited}, timeout=MANAGER_RESULT_WAIT_S)  # a future, not a coroutine: never a task
        if not done:
            # the executor is wedged; whatever it raises later must not surface as "never retrieved"
            waited.add_done_callback(lambda f: f.exception())
            _LOGGER.warning("MQTT: the result of a manager action may not have reached the broker in time")
            return
        try:
            waited.result()
        except (RuntimeError, ValueError) as err:  # the connection dropped in between: the action itself still completes
            _LOGGER.warning("MQTT: the result of a manager action may not have reached the broker: %s", err)

    def _publish_manager_discovery(self) -> None:
        """The manager device on its own while entity discovery is off (with
        discovery on, _publish_discovery_all carries it)."""
        if self.config.discovery_enabled or not self._connected or self._moving:
            return
        mid, block, comps = self._manager_discovery()
        if self.config.manager_discovery:
            self._publish_device_discovery(mid, block, comps)
            if mid in self._discovery_map:  # published (with entity discovery on, turning that off removes it)
                self._set_manager_announced(self._discovery_topic(mid), True)
                self._manager_absent_sent = False  # turned off again later in this connection: removed again
            return
        if self._manager_absent_sent:
            return
        topic = self._discovery_topic(mid)
        # Removed only when there is something to remove: an empty config for a device the main HA never received makes
        # it warn "No device components to cleanup" at every connect.  What was announced is kept on disk, so turning it
        # off while disconnected or across a restart still removes it; without a record the broker is asked, once.
        if self._manager_announced is None:
            self._manager_absent_sent = True
            self.hass.async_create_background_task(self._async_clear_manager_if_retained(mid, topic),
                                                   "integration_manager MQTT manager device check")
        elif topic not in self._manager_announced:
            self._manager_absent_sent = True
        elif self._publish(topic, None, qos=1):
            self._manager_removed(mid, topic)

    def _manager_removed(self, mid: str, topic: str) -> None:
        self._discovery_map.pop(mid, None)
        self._blocks.pop(mid, None)
        self._manager_absent_sent = True
        self._set_manager_announced(topic, False)

    async def _async_clear_manager_if_retained(self, mid: str, topic: str) -> None:
        """No record of what was announced (a new install, or one upgraded from a version that kept none): a manager
        device config retained on the broker is what the main HA has; one that is not there needs no removal."""
        try:
            found = await self.hass.async_add_executor_job(self._retained_scan, "mgr", [(topic, 1)])
        except Exception as err:  # noqa: BLE001 - unknown, so removed as before: at worst the main HA warns once
            _LOGGER.debug("MQTT: could not read the retained manager device config (%s): removing it", err)
            found = {topic: b"?"}
        if not self._connected or self._moving or self.config.manager_discovery or self.config.discovery_enabled:
            return  # the settings or the connection changed meanwhile: the next pass decides
        if not found.get(topic):
            self._set_manager_announced(topic, False)
        elif self._publish(topic, None, qos=1):
            self._manager_removed(mid, topic)

    def _manager_announced_file(self) -> str:
        return self.hass.config.path("integration_manager", "mqtt_manager_device.json")

    def _read_manager_announced(self) -> frozenset[str] | None:
        """Blocking."""
        data = read_json(self._manager_announced_file(), None)
        topics = data.get("announced") if isinstance(data, dict) else None
        return frozenset(t for t in topics if isinstance(t, str)) if isinstance(topics, list) else None

    def _set_manager_announced(self, topic: str, announced: bool) -> None:
        """Loop: written by the ordered writer, and only on a change (the manager config is re-announced every minute)."""
        current = self._manager_announced or frozenset()
        if self._manager_announced is not None and (topic in current) == announced:
            return
        self._manager_announced = current | {topic} if announced else current - {topic}
        writer.write_nowait(self._manager_announced_file(), {"announced": sorted(self._manager_announced)}, fsync=False)

    def _manager_discovery(self) -> tuple[str, dict[str, Any], dict[str, dict[str, Any]]]:
        return disc.manager_device(
            self.base_topic, self.prefix,
            {"status": self._status_topic(), "health": self._health_topic(), "manager": self._manager_topic(), "cmd": self._manager_cmd_base()},
            (self._health_last or {}).get("integration"), self.manager.version if self.manager else "", self.config.manager_commands)

    # ----- service catalog ------------------------------------------------

    async def _publish_services(self) -> None:
        """One retained document per domain with every registered service,
        its fields/target/description; the consuming HA calls them through
        <base>/call/<domain>/<service>."""
        if not self._connected:
            return
        rows = await service_rows(self.hass)
        rows = [{**r, "services": [s for s in r["services"] if (r["domain"], s["name"]) not in MQTT_CALL_DENY_SERVICES]} for r in rows
                if r["domain"] not in self.config.exclude_integrations and r["domain"] not in MQTT_CALL_DENY_DOMAINS]
        base = f"{self.base_topic}/services"
        current = {r["domain"] for r in rows}
        for r in rows:
            self._publish_if_changed(f"{base}/{r['domain']}", _dumps(
                {"domain": r["domain"], "custom": r["custom"], "call_topic": f"{self._call_base()}/{r['domain']}/<service>",
                 "result_topic": f"{self.base_topic}/result/{r['domain']}/<service>", "services": r["services"]}))
        for gone in self._services_published - current:
            self._publish(f"{base}/{gone}", None)
        self._services_published = current
        self.stats["services_published"] = sum(len(r["services"]) for r in rows)

    def _clear_services_catalog(self) -> None:
        """Clear every retained catalog document.  Forgetting which domains were published is what makes a
        later start publish them all again (the content gate would otherwise skip an unchanged domain)."""
        for domain in self._services_published:
            self._publish(f"{self.base_topic}/services/{domain}", None, qos=1)
        self._services_published = set()
        self.stats["services_published"] = 0

    def _remove_component(self, entity_id: str) -> None:
        """HA's documented removal form: republish the device with the
        component reduced to {"platform": <domain>}; the next full republish
        drops the key entirely."""
        if not self._connected or self._moving:
            return  # the map keeps it; the next full republish sends the removal form
        for disc_id, comps in list(self._discovery_map.items()):
            if entity_id not in comps:
                continue
            groups = None
            block = self._blocks.get(disc_id)
            if block is None:
                groups, _ = self._group_by_device()
                block = groups[disc_id][0] if disc_id in groups else None
            if block is None:
                return  # device vanished entirely: the full republish clears its config
            remaining = {eid: c for eid, c in comps.items() if eid != entity_id}
            if not remaining:
                if groups is None:
                    groups, _ = self._group_by_device()
                if disc_id not in groups and not self._boot_carried(disc_id, [entity_id]):
                    # Last entity of a device that is gone from here too (a deleted
                    # config entry): a config carrying nothing but the removal form
                    # leaves an empty device on the consumer until the next full
                    # republish, an hour away by default.
                    if self._publish(self._discovery_topic(disc_id), None, qos=1):
                        self._note_boot_removed(disc_id, self._discovery_map.pop(disc_id, None) or {})
                        self._blocks.pop(disc_id, None)
                    return
            # _publish_device_discovery adds the removal form for entity_id itself
            self._publish_device_discovery(disc_id, block, remaining)
            return
        # never published by this process, only carried from the one before (excluded by a rule at this start, or
        # still setting up): the carry has to end here too, or the config keeps announcing it to the consumer
        key = _comp_key(entity_id)
        for disc_id, comps in (self._boot_components or {}).items():
            if key not in comps or (disc_id, key) in self._boot_removed:
                continue
            self._note_boot_removed(disc_id, [entity_id])
            block = self._blocks.get(disc_id)
            live = self._discovery_map.get(disc_id)
            if block is None or live is None:
                return  # no config of ours to correct: the full republish (or the sweep) clears the device
            payload_comps = dict(live)
            self._publish_device_discovery(disc_id, block, payload_comps, removed={key: comps[key].get("platform", entity_id.split(".", 1)[0])})
            return

    # ----- events ----------------------------------------------------------

    @callback
    def _on_state(self, event: Event) -> None:
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if new is not None:
            # An event entity's state is the time of its last event: only a
            # changed state is a new event (restore at startup, availability
            # flaps and attribute-only writes must not replay it).
            is_event = False
            if new.domain == "event" and new.state not in ("unavailable", "unknown"):
                # the state is the time of the last occurrence: only a time not seen before is a new one
                # (an availability flap restores the same time: unavailable -> T must not replay T)
                last = self._last_event.get(new.entity_id)
                if last is None and old is not None and old.state != "unavailable":
                    # "unknown" is an entity that exists and has never fired, so the step out of it is
                    # the first real occurrence; a restored state arrives with no old state at all, and
                    # a flap (unavailable -> T) is left alone because this process may not have seen T
                    last = old.state
                is_event = old is not None and last is not None and new.state != last
                self._last_event[new.entity_id] = new.state
            self._publish_state(new, is_event=is_event)
            self._refresh_discovery_if_reshaped(new)
        elif old is not None:
            entry = er.async_get(self.hass).async_get(old.entity_id)
            if entry is not None and entry.disabled and entry.disabled_by is not er.RegistryEntryDisabler.CONFIG_ENTRY:
                self._publish_disabled(old.entity_id, old)  # disabling removes the state: see _on_registry
            else:
                self._clear(old.entity_id)

    def _refresh_discovery_if_reshaped(self, state: State) -> None:
        """A discovery component is not only a set of topics, it is also what the entity's attributes say
        it can do: a cover's position and tilt, a fan's speed, direction and oscillation, an alarm's code
        format.  An entity that had no value for one of those when its config went out (it was
        `unavailable`, or it had not reported yet) was announced without that control, and nothing here
        looked again until the full republish - an hour away by default.  So: build the component the way
        discovery would build it now, and when it is not the one that was announced, schedule the same
        debounced refresh a registry change uses.  Unchanged configs are dropped by the content gate, so
        an entity whose shape is stable costs one dict comparison per state change and no traffic."""
        if not (self.config.discovery_enabled and self._connected) or self._moving:
            return
        announced = next((comps[state.entity_id] for comps in self._discovery_map.values()
                          if state.entity_id in comps), None)
        if announced is None:
            return  # not announced by this process (excluded, or still to come): the republish decides
        integration = platform_of(self.hass, state.entity_id) or "unregistered"
        if self._integration_excluded(integration):
            return
        rule = self.rules.for_entity(state.entity_id)
        if rule.get("exclude"):
            return
        try:
            comp = self.rules.apply_component(
                disc.build_component(self.hass, state, self._topic_for(state.entity_id, integration),
                                     self._cmd_base(), self.prefix, disc.compat_for(self.config.main_ha_version)), rule)
        except Exception:  # noqa: BLE001 - _group_by_device names it when it skips the entity
            return
        if comp != announced:
            self._schedule_discovery_refresh()

    def _publish_disabled(self, entity_id: str, old: State | None) -> None:
        """An entity disabled here (by the user, its device or its integration) stays on the consumer with its
        customisations, unavailable there: an empty document would leave its last value showing."""
        attrs = dict(old.attributes) if old is not None else {}
        self._publish_state(State(entity_id, "unavailable", attrs, validate_entity_id=False))

    @callback
    def _on_registry(self, event: Event) -> None:
        action = event.data.get("action")
        entity_id = event.data.get("entity_id", "")
        if action == "remove":
            if self.config.discovery_enabled:
                self._remove_component(entity_id)
            self._clear(entity_id)
            return
        disabled = False
        if action == "update" and "disabled_by" in (event.data.get("changes") or {}) and self._connected:
            entry = er.async_get(self.hass).async_get(entity_id)
            if entry is not None and entry.disabled_by is er.RegistryEntryDisabler.CONFIG_ENTRY:
                # a Stop (or a disabled config entry): the entity keeps existing on the
                # consumer, unavailable, with its customisations; nothing is removed
                return
            if entry is not None and entry.disabled:
                # Not removed from the consumer: the full republish announces every registry entry without a state
                # (enabled_by_default false) and would bring it back there, without its customisations.  It stays,
                # unavailable (an empty document would leave its last value showing); deleting it removes it.
                disabled = True
                self._publish_disabled(entity_id, self.hass.states.get(entity_id))
        if action in ("create", "update") and self._connected:
            old_id = (event.data.get("changes") or {}).get("entity_id") or event.data.get("old_entity_id")
            if old_id and old_id != entity_id:
                if self.config.discovery_enabled:
                    self._remove_component(old_id)  # removal first: an empty document before it logs "Erroneous JSON" there
                self._clear(old_id)
            state = self.hass.states.get(entity_id)
            if state is not None and not disabled:
                self._publish_state(state)
            # Registry metadata changed (name, device, class...): refresh
            # discovery once the burst is over, not per event.
            if self.config.discovery_enabled:
                self._schedule_discovery_refresh()

    @callback
    def _on_device_registry(self, _event: Event) -> None:
        """A device's name, model or parent appears in the discovery device
        block, nowhere in the entity documents: without this the retained
        config keeps the old name until the next full republish."""
        if self.config.discovery_enabled and self._connected:
            self._schedule_discovery_refresh()

    def _schedule_discovery_refresh(self) -> None:
        """Debounced: an integration adding 100 entities fires 100 events."""
        if self._registry_timer is not None:
            self._registry_timer.cancel()
        self._registry_timer = self.hass.loop.call_later(
            3, lambda: self.hass.async_create_task(self._async_discovery_refresh())
        )

    def _clear(self, entity_id: str) -> None:
        if not entity_id:
            return
        topic = self._topics.pop(entity_id, None)
        if topic is None:
            integ = self._integration_of(entity_id)
            if integ is None:
                return  # never published by us (or already cleared with the registry entry): nothing to clear
            if self._excluded_now(entity_id):
                # never published by this process: its document was cleared when it was excluded, or by the orphan
                # sweep when that happened while the container was down (a reload or a stop reaches here for every one)
                return
            topic = self._topic_for(entity_id, integ)
        self._last_hash.pop(topic, None)  # a re-included entity must be published again, changed or not
        if self._publish(topic, None, qos=1):
            self.stats["cleared"] += 1
        else:
            self._pending_clears.add(topic)  # sent at the next republish

    async def _on_timer(self, _now) -> None:
        full = time.time() - self._last_full >= _bounded("full_republish_interval_min", self.config.full_republish_interval_min) * 60
        await self.async_republish_all(full=full)

    @callback
    def _on_service_event(self, _event: Event) -> None:
        if self._services_timer is not None:
            self._services_timer.cancel()
        # A burst of registrations means an integration just finished
        # loading: republish the catalog once the burst is over.  Its
        # entities are covered by the registry burst (_async_discovery_refresh).
        self._services_timer = self.hass.loop.call_later(
            5, lambda: self.hass.async_create_task(self._async_services_refresh())
        )

    async def _async_discovery_refresh(self) -> None:
        """A registry burst (rename, device change, an integration adding
        entities): discovery configs (unchanged ones are skipped by the
        content gate) and the documents of entities without one yet; not a
        full republish."""
        if not self._connected or self._moving:
            return
        for state in self.hass.states.async_all():
            if state.entity_id not in self._topics:
                self._publish_state(state)
        if self.config.discovery_enabled:
            self._publish_discovery_all()
        self.publish_health()
        self._publish_manager_discovery()

    async def _async_services_refresh(self) -> None:
        if self._connected and not self._moving:
            await self._publish_services()

    async def async_republish_all(self, full: bool = True) -> int:
        """full=True: every document, discovery and services (connect, hourly,
        explicit).  full=False: only documents whose content changed since
        they were last published (the periodic pass).  Long runs yield to
        the loop every batch so incoming data keeps flowing."""
        if not self._connected or self._moving:
            return 0
        for topic in list(self._pending_clears):
            if self._publish(topic, None, qos=1):
                self._pending_clears.discard(topic)
                self.stats["cleared"] += 1
        n = 0
        for i, state in enumerate(self.hass.states.async_all()):
            try:
                if self._publish_state(state, force=full):
                    n += 1
            except Exception as err:  # noqa: BLE001 - one odd entity must not stop health, discovery and services
                _LOGGER.warning("MQTT: document of %s not published: %s", state.entity_id, err)
            if i % REPUBLISH_BATCH == REPUBLISH_BATCH - 1:
                await asyncio.sleep(REPUBLISH_BATCH_PAUSE_S)
                if not self._connected or self._moving:
                    return n
        self.publish_health()
        if not full:
            self.stats["last_incremental_republish"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.stats["entities_last_incremental"] = n
            _LOGGER.debug("MQTT incremental republish: %s changed documents", n)
            return n
        self._last_full = time.time()
        self.stats["entities_last_run"] = n
        self.stats["last_full_republish"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if self._identity_sweep_due:
            # the identity changed while the broker was unreachable: what the old names left retained goes now
            self._identity_sweep_due = not await self.hass.async_add_executor_job(self._sweep_old_identity, self.base_topic)
        if self.config.discovery_enabled and self._orphan_sweep_due and self._boot_components is None:
            await self._async_read_boot_components()
        if self.config.discovery_enabled:
            self._publish_discovery_all()
        self._publish_manager_discovery()
        self.publish_manager()
        if self._resync_excluded:
            self._resync_excluded = False
            await self._async_resync_excluded()
        if self._undiscover_due and not self.config.discovery_enabled:
            try:
                removed = await self.hass.async_add_executor_job(self._clear_discovery_retained)
                self._set_undiscover_due(False)
                keep = f"{self.base_topic}_manager"
                for did in [d for d in self._discovery_map if d != keep]:
                    self._discovery_map.pop(did, None)
                    self._blocks.pop(did, None)
                _LOGGER.info("MQTT: discovery turned off: removed %s announced devices from the consumer", removed)
            except RuntimeError as err:
                _LOGGER.warning("MQTT: removing the announced entities after discovery was turned off failed, retried: %s", err)
        if self._orphan_sweep_due and self.hass.is_running and time.time() - self._started_at > ORPHAN_SWEEP_DELAY_S:
            self._orphan_sweep_due = False  # a connect after HA started: the timer from _on_started may have found it disconnected
            await self._async_sweep_orphans()
        await self._publish_services()
        _LOGGER.info(
            "MQTT full republish: %s entities, %s services, discovery: %s devices / %s components",
            n, self.stats["services_published"], self.stats["discovery_devices"], self.stats["discovery_components"],
        )
        return n

    def status(self) -> dict[str, Any]:
        named = bool(self._live_base or self.wanted_base_topic)  # no identity: no topic is used, "hass_none" is no name
        return {
            **self.stats,
            "enabled": self.config.enabled,
            "host": self.config.host,
            "port": self.config.port,
            "base_topic": self.base_topic if named else None,
            "wanted_base_topic": self.wanted_base_topic,
            "identity_moved": self._connected and self.wanted_base_topic != self._live_base,
            "has_identity": bool(self.wanted_base_topic),
            "retained_cleanup_pending": self.retained_cleanup_pending(),  # uninstalled identities a broker did not take yet
            "prefix": self.prefix if named else None,
            "force_base_topic": self.config.force_base_topic,
            "tls": self.config.tls,
            # what a full republish publishes a document for: excluded entities (by integration or by a rule) not counted
            "entities_total": sum(1 for state in self.hass.states.async_all() if not self._excluded_now(state.entity_id)),
            # registry entries with no state (disabled or not yet added): no
            # document to publish, only announced through discovery
            "entities_registry_only": sum(
                1 for e in er.async_get(self.hass).entities.values()
                if self.hass.states.get(e.entity_id) is None and not self._integration_excluded(e.platform)
                and not self.rules.for_entity(e.entity_id).get("exclude")
            ),
            "discovery_enabled": self.config.discovery_enabled,
            "discovery_prefix": self.config.discovery_prefix,
            "main_ha_version": self.config.main_ha_version,
            "manager_discovery": self.config.manager_discovery,
            "manager_commands": self.config.manager_commands,
            "manager_topic": self._manager_topic() if named else None,
            "cmd_base": self._cmd_base() if named else None,
            "call_base": self._call_base() if named else None,
            "health_topic": self._health_topic() if named else None,
            "rules": len(self.rules.rules),
            "rules_error": self.rules.problem,
            "health": self._health_last or self.build_health(),
            "recent_commands": self.recent_commands(30),
            "history_size": len(self.history),
        }
