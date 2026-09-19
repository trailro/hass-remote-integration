"""Fifth review, MQTT publisher: three concerns about work held between paho's thread and the loop.

R5-1   a command that passed the "is this an entity we publish" gate on paho's thread still reached Home
       Assistant when the operator excluded its integration before the task got to services.async_call.
       REAL: the gate was read at arrival only, where a generic call reads it at execution (_call_target_problem
       runs inside _run).  Fixed by re-reading _topics in the command task.
R5-1b  the same concern's other half: a command or call from before a reconnect finishing after it, and the
       dedup map / pending clears surviving _on_disconnect.  NOT a defect - dropping either is what would
       break things (a re-run service, a retained document never cleared).  Pinned here.
R5-2   two async_republish_all runs overlapping.  NOT a defect: every write step is synchronous and every
       payload is computed from current state when it is written, so no interleaving can leave a stale
       document or a half-built device config behind.  Pinned here.
R5-3   the retained scans grew a dict with no byte budget: a broker holding a very large retained set under
       the scanned prefix (or publishing large retained payloads there) grew this container's memory for as
       long as the scan's time budget lasted.  REAL: _broker_max_packet and _oversized_warned bound what
       goes OUT, nothing bounded what came in.  Fixed with RETAINED_SCAN_MAX_BYTES.

The R5-1 and R5-3 tests fail on the tree before the fix.
"""

import asyncio
import collections
import inspect
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.integration_manager import mqtt_publisher as mp

BASE = "hass_r5"


class FakeClient:
    protocol = mqtt.MQTTv311

    def __init__(self):
        self.published = []
        self.max_inflight_messages = 20

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0)

    def subscribe(self, topics):
        return mqtt.MQTT_ERR_SUCCESS, 1


class Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload.encode(), retain


def _publisher(loop, calls):
    """A publisher built field by field: no HA instance and no broker, as the other MQTT tests do."""
    pub = object.__new__(mp.MqttPublisher)
    pub.hass = mock.Mock()
    pub.hass.data = {}
    pub.hass.loop = loop
    pub.hass.async_create_task = loop.create_task

    async def async_call(domain, service, data, blocking=True, return_response=False):
        calls.append((domain, service, dict(data)))

    pub.hass.services.async_call = async_call
    pub.hass.services.has_service = lambda domain, service: True
    pub.hass.services.supports_response = lambda domain, service: mp.SupportsResponse.NONE
    pub.config = mp.MqttConfig(qos=1)
    pub.history = collections.deque(maxlen=mp.HISTORY_MAX)
    pub._calls = {}
    pub._calls_lock = threading.Lock()
    pub._in_flight = 0
    pub._cleared_cmds = {}
    pub._subscribing = None
    pub._subscribing_lock = threading.Lock()
    pub.stats = {"calls": 0, "commands": 0, "last_call": None, "last_command": None, "published": 0, "cleared": 0,
                 "unchanged_skipped": 0, "oversized_skipped": 0, "last_oversized": None, "services_published": 0,
                 "connected": False, "connect_error": "", "subscribe_error": "", "protocol": None}
    pub._client = FakeClient()
    pub._connected = True
    pub._connected_at = 0.0
    pub._broker_max_packet = 0
    pub._oversized_warned = set()
    pub._last_hash = {}
    pub._last_disconnect = ""
    pub._last_subscribe_error = ""
    pub._manager_absent_sent = True
    pub._moving = False
    pub._stopping = False
    pub._live_base = BASE
    pub._live_prefix = BASE + "_"
    pub._key_provider = lambda: BASE
    pub._tls_checked_at, pub._tls_error = 0.0, ""
    pub._topics = {"text.note": f"{BASE}/x/text/note"}
    pub._range_pending = {}
    pub._pending_clears = set()
    pub._services_published = set()
    pub.hass.states.get.return_value = mock.Mock(attributes={})
    pub.async_republish_all = lambda: asyncio.sleep(0)  # a CONNACK starts one: not what these tests are about
    return pub


# ----- R5-1: a queued command and an exclusion that lands between arrival and execution --------------

class QueuedCommandExclusionTest(unittest.IsolatedAsyncioTestCase):
    """_handle_message reads _topics on paho's thread and hands the service call to the loop; an exclusion
    adopted on the loop (async_reload_config -> _drop_newly_excluded, or a reconnect) empties _topics in
    between.  The operator who excluded an integration must not see one more command reach it."""

    async def _queued(self, exclude):
        calls = []
        loop = asyncio.get_running_loop()
        pub = _publisher(loop, calls)
        pub._handle_message(Msg(f"{BASE}/cmd/text/note/value", "hello"))
        if exclude:
            # the loop turn the command was handed to: the task exists, its first step has not run yet, and
            # this is exactly where _drop_newly_excluded takes the entity out of _topics
            loop.call_soon(lambda: pub._topics.pop("text.note", None))
        for _ in range(8):
            await asyncio.sleep(0)
        return pub, calls

    async def test_a_command_queued_before_the_exclusion_never_reaches_home_assistant(self):
        pub, calls = await self._queued(exclude=True)
        self.assertEqual(calls, [])
        self.assertEqual([(r["state"], r["error"]) for r in pub.history],
                         [("rejected", "not an entity this container publishes")])

    async def test_a_command_for_an_entity_still_published_runs_as_before(self):
        pub, calls = await self._queued(exclude=False)
        self.assertEqual(calls, [("text", "set_value", {"entity_id": "text.note", "value": "hello"})])
        self.assertEqual([r["state"] for r in pub.history], ["ok"])


# ----- R5-1b: what a reconnect must NOT throw away ---------------------------------------------------

class WorkSurvivesAReconnectTest(unittest.IsolatedAsyncioTestCase):
    """The concern's other half.  _on_disconnect clears the connection's bookkeeping and nothing else, and
    that is the correct behaviour: paho acknowledges a QoS 1 command as it delivers it, so a broker never
    sends it again - dropping queued work would lose it, and dropping the dedup map would let a consumer's
    retry run a service a second time."""

    def _reconnect(self, pub):
        pub._on_disconnect(pub._client, None, None, 0)
        self.assertFalse(pub._connected)
        with mock.patch.object(mp.events, "emit"):
            pub._on_connect(pub._client, None, None, 0, None)
        self.assertTrue(pub._connected)

    async def test_an_id_answered_before_the_drop_is_answered_from_history_after_it(self):
        calls = []
        pub = _publisher(asyncio.get_running_loop(), calls)
        pub._handle_message(Msg(f"{BASE}/call/light/turn_on", '{"_id": 7, "entity_id": "text.note"}'))
        for _ in range(8):
            await asyncio.sleep(0)
        self.assertEqual(len(calls), 1)
        self._reconnect(pub)
        pub._handle_message(Msg(f"{BASE}/call/light/turn_on", '{"_id": 7, "entity_id": "text.note"}'))
        for _ in range(8):
            await asyncio.sleep(0)
        self.assertEqual(len(calls), 1, "the retry ran the service a second time")
        self.assertEqual(pub.history[-1]["state"], "duplicate")

    async def test_a_clear_that_could_not_be_sent_is_still_pending_after_the_reconnect(self):
        pub = _publisher(asyncio.get_running_loop(), [])
        pub._pending_clears.add(f"{BASE}/demo/light/gone")
        self._reconnect(pub)
        self.assertEqual(pub._pending_clears, {f"{BASE}/demo/light/gone"})

    async def test_a_command_in_flight_when_the_connection_dropped_still_finishes(self):
        calls = []
        pub = _publisher(asyncio.get_running_loop(), calls)
        pub._handle_message(Msg(f"{BASE}/cmd/text/note/value", "hello"))
        self._reconnect(pub)  # the drop and the new session happen while the task is queued
        for _ in range(8):
            await asyncio.sleep(0)
        self.assertEqual(calls, [("text", "set_value", {"entity_id": "text.note", "value": "hello"})])


# ----- R5-2: two full republishes at once ------------------------------------------------------------

class OverlappingRepublishTest(unittest.IsolatedAsyncioTestCase):
    """A CONNACK starts one and the timer (or the operator) starts another.  They do overlap - there is no
    lock - and that is harmless: the document loop is the only place either of them yields, every payload is
    built from current state at the moment it is written, and every other step (the discovery configs above
    all) is a plain synchronous function that cannot be cut in half."""

    async def test_two_runs_interleave_without_taking_anything_from_each_other(self):
        pub = _publisher(asyncio.get_running_loop(), [])
        pub.config = mp.MqttConfig(qos=1, discovery_enabled=True)
        pub._pending_clears = set()
        pub._identity_sweep_due = pub._undiscover_due = False
        pub._orphan_sweep_due = pub._resync_excluded = True
        pub.hass.is_running = True
        pub._started_at = 0.0
        pub._discovery_map, pub._blocks = {}, {}
        pub._boot_components = {}
        pub.stats.update(discovery_devices=0, discovery_components=0, entities_last_run=0,
                         last_full_republish=None, entities_last_incremental=0, last_incremental_republish=None)
        pub.publish_health = pub.publish_manager = pub._publish_manager_discovery = lambda: None
        pub._publish_services = mock.AsyncMock()
        sweeps = []

        async def sweep(name):
            sweeps.append(name)
            await asyncio.sleep(0)

        pub._async_sweep_orphans = lambda: sweep("orphans")
        pub._async_resync_excluded = lambda: sweep("excluded")
        discovery_passes = []
        pub._publish_discovery_all = lambda *a, **k: discovery_passes.append(len(discovery_passes))
        del pub.async_republish_all  # the real one, this time

        entities = [f"sensor.e{i}" for i in range(mp.REPUBLISH_BATCH * 2 + 7)]
        writers, written = [], []

        def publish_state(state, is_event=False, force=True):
            writers.append(id(asyncio.current_task()))
            written.append(state)
            return True

        pub._publish_state = publish_state
        pub.hass.states.async_all.return_value = entities

        first = asyncio.ensure_future(pub.async_republish_all(full=True))
        await asyncio.sleep(0)
        counts = await asyncio.gather(first, pub.async_republish_all(full=True))

        self.assertEqual(len(set(writers)), 2, "the two runs did not overlap: the test proves nothing")
        self.assertGreater(sum(1 for a, b in zip(writers, writers[1:]) if a != b), 0,
                           "the two runs did not interleave: the test proves nothing")
        # neither run lost entities to the other, and neither counted the other's work as its own
        self.assertEqual(collections.Counter(written), collections.Counter(entities * 2))
        self.assertEqual(counts, [len(entities), len(entities)])
        self.assertEqual(len(discovery_passes), 2)
        # the once-per-process sweeps: their flags are cleared before the await, so the second run skips them
        self.assertEqual(sorted(sweeps), ["excluded", "orphans"])

    def test_the_discovery_pass_cannot_be_cut_in_half(self):
        """What makes a mixed set of device configs impossible: _publish_discovery_all never awaits, so the
        second run cannot start it while the first is between two of its publishes."""
        self.assertFalse(inspect.iscoroutinefunction(mp.MqttPublisher._publish_discovery_all))
        self.assertFalse(inspect.iscoroutinefunction(mp.MqttPublisher._publish_device_discovery))
        self.assertFalse(inspect.iscoroutinefunction(mp.MqttPublisher._group_by_device))


# ----- R5-3: a retained scan and a broker with a very large retained set ------------------------------

class RetainedScanBudgetTest(unittest.TestCase):
    """The scans (probe, stale documents, the boot configs, the orphan and exclusion sweeps) collect every
    retained message under the scanned prefix into one dict.  _broker_max_packet caps what this process
    PUBLISHES and _oversized_warned only remembers the topics it refused to send: neither reads the scan."""

    def _pub(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        return _publisher(loop, [])

    def _scan(self, payload_bytes, count):
        pub = self._pub()
        pub._live_base = BASE

        def client_factory(*_a, **kwargs):
            c = mock.Mock()
            c.protocol = kwargs.get("protocol")
            c.max_inflight_messages = 20
            c.connect.side_effect = lambda *a, **k: c.on_connect(c, None, None, 0, None)

            def subscribe(topics):
                c.on_subscribe(c, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=1)])
                for i in range(count):
                    c.on_message(c, None, Msg(f"{BASE}/big/{i}", "x" * payload_bytes, retain=True))
                return mqtt.MQTT_ERR_SUCCESS, 1

            c.subscribe.side_effect = subscribe
            return c

        with mock.patch.object(mp.mqtt, "Client", side_effect=client_factory), \
                mock.patch.object(mp.MqttPublisher, "_collect_quiet"):
            return pub._retained_scan("probe", [(f"{BASE}/#", 1)])

    def test_the_broker_cannot_grow_this_process_without_bound(self):
        chunk = mp.RETAINED_SCAN_MAX_BYTES // 8
        with self.assertLogs(mp._LOGGER, "WARNING") as logs:
            found = self._scan(chunk, 40)  # five times the budget offered
        held = sum(len(p) + len(t) for t, p in found.items())
        self.assertLessEqual(held, mp.RETAINED_SCAN_MAX_BYTES + chunk)
        self.assertLess(len(found), 40)
        self.assertTrue(any("left unread" in line for line in logs.output))

    def test_a_scan_that_fits_the_budget_keeps_everything(self):
        found = self._scan(1000, 50)
        self.assertEqual(len(found), 50)

    def test_the_publish_cap_is_not_a_scan_cap(self):
        """Why the concern was worth checking: the two constants the code already had bound the other direction."""
        pub = self._pub()
        pub._broker_max_packet = 256
        self.assertEqual(pub._publish_limit(), 256)
        self.assertTrue(pub._oversized(f"{BASE}/a", "x" * 300))
        self.assertEqual(pub._oversized_warned, {f"{BASE}/a"})


if __name__ == "__main__":
    unittest.main()
