"""Two concerns raised by external reviews, settled with evidence.

1. "The cutover comparison counts MQTT entities on the main Home Assistant that
   have nothing to do with this container."  It does not: compute_parity keeps
   only registry entries whose platform is ``mqtt`` *and* whose unique id is one
   of ours (our prefix plus a ``<platform>.<object_id>`` tail, or one of the
   manager device's fixed suffixes).  A foreign MQTT entity can therefore never
   become a match, an orphan, or hide a missing mirror.  ParityScopeTest pins
   that, including the neighbouring-identity case (``hass_a`` must not claim
   ``hass_a_b``'s entities) - nothing pinned it before.

   What the same review found on the other side of the page is real:
   CutoverView._parent_blockers dropped *every* mqtt entity from the set of
   entity ids already registered on the main HA, so an id held there by an
   unrelated MQTT entity (Zigbee2MQTT, Tasmota, a hand-written sensor) or by a
   leftover of an earlier identity of this container passed the check silently -
   and the mirror then lands as ``<id>_2``, exactly what the check exists to
   prevent.  CutoverBlockerScopeTest covers it.

2. "An operator's fnmatch glob could be a ReDoS."  It cannot.  fnmatch.translate
   wraps every ``*`` that is followed by more pattern in an atomic group
   (``(?>.*?a)``) and a glob has no alternation and no backreferences, so the
   compiled regex has no nested quantifier to backtrack through.  Measured in
   the container (Python 3.14.7): the classic ``*a*a...*a`` shape at the 200
   character cap, against a 255 character subject of nothing but ``a``, takes
   1.3 us per match, and cost grows linearly with the subject (0.27 us at 64
   chars, 22 us at 4096).  The worst rule set an operator can save - 50 globs of
   200 characters, matched against 3000 entities whose ids are 255 ``a``s - costs
   200 ms for one full republish; a realistic set costs 3 ms, and ~1 us per state
   change.  GlobCostTest pins the shape rather than the microseconds.
"""

import asyncio
import inspect
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import mqtt_rules, parity
from custom_components.integration_manager import mqtt_publisher as mp

BASE = "hass_ramses_cc"
PREFIX = BASE + "_"


def _run_parity(announced, parent_entities):
    """compute_parity with `announced` as our components and `parent_entities` as the parent's entity registry."""
    hass = mock.Mock()
    hass.states.async_entity_ids.return_value = []
    hass.states.get.return_value = SimpleNamespace(state="on")
    publisher = mock.Mock(prefix=PREFIX, base_topic=BASE)
    publisher.config.discovery_enabled, publisher.config.manager_discovery = True, True
    publisher.discovery_preview.return_value = [{
        "discovery_id": f"{BASE}_dev", "device": {"name": "dev"},
        "components": {eid: {"unique_id": uid, "platform": eid.split(".", 1)[0], "name": "n", "default_entity_id": eid}
                       for eid, uid in announced.items()}}]
    client = mock.Mock()
    client.url = "http://parent"
    client.commands = mock.AsyncMock(return_value=[
        parent_entities, [], [{"entity_id": e["entity_id"], "state": "on"} for e in parent_entities],
        {"components": ["mqtt"], "version": "2026.9.3"},
    ])
    with mock.patch.object(parity, "_parent_client", return_value=client):
        return asyncio.run(parity.compute_parity(hass, mock.Mock(), publisher))


def _entry(entity_id, unique_id, platform="mqtt"):
    return {"entity_id": entity_id, "unique_id": unique_id, "platform": platform}


class ParityScopeTest(unittest.TestCase):
    """Only our own entities on the parent enter the comparison - both sides are scoped by unique id."""

    def test_foreign_mqtt_entities_are_neither_matched_nor_orphans_and_hide_no_gap(self):
        res = _run_parity(
            {"sensor.zone_1": PREFIX + "sensor.zone_1"},
            [
                _entry("sensor.kitchen_temperature", "0x00158d0001234567_temperature"),   # Zigbee2MQTT
                _entry("sensor.boiler_pressure", "boiler-pressure"),                      # a hand-written MQTT sensor
                _entry("switch.sonoff_1", "tasmota_10A2B3_RL_1"),                          # Tasmota
                _entry("sensor.zone_1_bridge", PREFIX + "weird"),                          # our prefix, not entity-shaped
                _entry("sensor.zone_1_native", PREFIX + "sensor.zone_1", platform="ramses_cc"),  # not the mqtt platform
            ],
        )
        self.assertEqual(res["parent"], 0)
        self.assertEqual(res["summary"], {**res["summary"], "matched": 0, "orphans": 0, "missing": 1})
        self.assertEqual([m["entity_id"] for m in res["missing"]], ["sensor.zone_1"])

    def test_a_neighbouring_identity_is_not_claimed(self):
        """hass_ramses_cc must not claim hass_ramses_cc_b's entities, whose unique ids start with our prefix too."""
        res = _run_parity(
            {"sensor.zone_1": PREFIX + "sensor.zone_1"},
            [_entry("sensor.zone_1_2", PREFIX + "b_sensor.zone_1"),
             _entry("binary_sensor.other_integration", PREFIX + "b_health_online")],
        )
        self.assertEqual(res["parent"], 0)
        self.assertEqual(res["summary"]["orphans"], 0)
        self.assertEqual(res["summary"]["missing"], 1)

    def test_our_own_entities_and_manager_entities_are_still_recognised(self):
        """The scoping is not so tight that it drops what really is ours: a match, a manager entity, an orphan."""
        res = _run_parity(
            {"sensor.zone_1": PREFIX + "sensor.zone_1"},
            [_entry("sensor.zone_1", PREFIX + "sensor.zone_1"),
             _entry("binary_sensor.hass_ramses_cc_integration", PREFIX + "health_online"),
             _entry("sensor.gone", PREFIX + "sensor.gone")],
        )
        self.assertEqual(res["parent"], 3)
        self.assertEqual(res["summary"]["matched"], 1)
        self.assertEqual(sorted(o["unique_id"] for o in res["orphans"]),
                         [PREFIX + "health_online", PREFIX + "sensor.gone"])


class FakePublisher:
    """Enough of MqttPublisher for CutoverView; `announced` is {entity_id: unique_id}."""

    def __init__(self, announced):
        self.prefix = PREFIX
        self.config = mock.Mock(discovery_enabled=True)
        self.stats = {"connected": True, "discovery_devices": 3}
        self.async_save = mock.AsyncMock()
        self.async_reload_config = mock.AsyncMock()
        self.async_republish_all = mock.AsyncMock(return_value=7)
        self._announced = announced

    def build_health(self):
        return {"state": "ok"}

    def discovery_preview(self):
        return [{"components": {eid: {"unique_id": uid, "default_entity_id": eid} for eid, uid in self._announced.items()}}]


class CutoverBlockerScopeTest(unittest.TestCase):
    """_parent_blockers: an entity id held on the main HA by anything that is not our own mirror of it sends the
    MQTT entity to <id>_2, whatever integration holds it."""

    def _blockers(self, announced, registry):
        hass = mock.Mock()
        installer = mock.Mock(running="ramses_cc", running_tag="0.60.4", smoke={"pending": None})
        installer.settings.data = {"parent_ha_url": "http://parent", "parent_ha_token": "t"}
        view = parity.CutoverView(hass, installer, FakePublisher(announced))
        client = mock.Mock()
        client.commands = mock.AsyncMock(side_effect=[[[]], [registry]])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            return asyncio.run(view._parent_blockers("ramses_cc", ["mqtt"]))

    def test_an_id_held_by_an_unrelated_mqtt_entity_blocks_the_cutover(self):
        (problem,) = self._blockers(
            {"sensor.zone_1": PREFIX + "sensor.zone_1"},
            [_entry("sensor.zone_1", "0x00158d0001234567_temperature")],   # Zigbee2MQTT got there first
        )
        self.assertIn("sensor.zone_1", problem)
        self.assertIn("_2", problem)

    def test_an_id_held_by_a_leftover_of_an_earlier_identity_blocks_it_too(self):
        (problem,) = self._blockers(
            {"sensor.zone_1": PREFIX + "sensor.zone_1"},
            [_entry("sensor.zone_1", "hass_ramses_sensor.zone_1")],   # this container under its previous instance key
        )
        self.assertIn("sensor.zone_1", problem)

    def test_our_own_mirror_of_that_entity_is_not_a_blocker(self):
        """Re-enabling discovery while our own entities are already there must stay possible."""
        self.assertEqual(self._blockers(
            {"sensor.zone_1": PREFIX + "sensor.zone_1"},
            [_entry("sensor.zone_1", PREFIX + "sensor.zone_1")],
        ), [])

    def test_the_integration_still_holding_the_id_is_still_a_blocker(self):
        (problem,) = self._blockers(
            {"sensor.zone_1": PREFIX + "sensor.zone_1"},
            [_entry("sensor.zone_1", "abc", platform="ramses_cc")],
        )
        self.assertIn("sensor.zone_1", problem)


class GlobCostTest(unittest.TestCase):
    """fnmatch globs from the rules file: matched per entity per republish and per state change."""

    WORST = "*a" * 100          # 200 characters: the cap replace_all enforces
    SUBJECT = "a" * 254 + "b"   # never matches, so every atomic group has to scan the whole subject

    def test_the_pattern_length_cap_is_what_bounds_the_worst_case(self):
        rules = mqtt_rules.MqttRules.__new__(mqtt_rules.MqttRules)
        rules.rules, rules.components = {}, None
        with self.assertRaises(ValueError):
            rules.replace_all({"*" + "a" * 200: {"exclude": True}})
        rules.replace_all({self.WORST: {"exclude": True}})
        self.assertEqual(list(rules.rules), [self.WORST])

    def test_the_worst_glob_an_operator_can_save_does_not_backtrack(self):
        """A catastrophic pattern would not finish; 200 matches of the worst shape measure ~0.3 ms in the container."""
        mqtt_rules.matches(self.WORST, self.SUBJECT)   # warm fnmatch's compile cache, as a republish would
        t0 = time.perf_counter()
        for _ in range(200):
            self.assertFalse(mqtt_rules.matches(self.WORST, self.SUBJECT))
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_the_cost_stays_linear_in_the_subject(self):
        """Doubling the subject must not square the time: the shape has no nested quantifier."""
        def per_match(n):
            subject = "a" * (n - 1) + "b"
            mqtt_rules.matches(self.WORST, subject)
            t0 = time.perf_counter()
            for _ in range(200):
                mqtt_rules.matches(self.WORST, subject)
            return (time.perf_counter() - t0) / 200

        short, long = per_match(256), per_match(4096)
        self.assertLess(long, short * 16 * 8)   # linear would be 16x; 8x head-room for a loaded runner

    def test_a_glob_cannot_leave_the_domain_it_names(self):
        self.assertTrue(mqtt_rules.matches("sensor.*", "sensor.zone_1"))
        self.assertFalse(mqtt_rules.matches("sensor.*", "light.zone_1"))
        self.assertFalse(mqtt_rules.matches("sensor.*", "binary_sensor.zone_1"))

    def test_a_pattern_without_a_wildcard_is_an_exact_id(self):
        """No pattern character, no regex at all: fnmatch is never reached."""
        self.assertTrue(mqtt_rules.matches("sensor.zone_1", "sensor.zone_1"))
        self.assertFalse(mqtt_rules.matches("sensor.zone", "sensor.zone_1"))

    def test_no_glob_can_reach_the_manager_device(self):
        """The manager's own entities are built by discovery.manager_device and never go through the rules, so
        no glob (and no `exclude`) can take the container's status, health or update entities off the main HA."""
        self.assertNotIn("rules", inspect.getsource(mp.MqttPublisher._manager_discovery))
        announced = inspect.getsource(mp.MqttPublisher._announced_groups)
        self.assertIn("groups[hid] = (hblock, hcomps)", announced)
        self.assertNotIn("rules", announced)


if __name__ == "__main__":
    unittest.main()
