"""Size caps that were untested (timeline, process log, resource history) and discovery component key clashes."""

import collections
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import logbuffer
from custom_components.integration_manager import events
from custom_components.integration_manager import manager_device as md
from custom_components.integration_manager import mqtt_publisher as mp


class EventsRotationTest(unittest.TestCase):
    def test_rotates_at_the_cap_and_keeps_one_previous_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(events, "MAX_BYTES", 400):
            path = os.path.join(d, "integration_manager", "events.jsonl")
            log = events.Events(path)
            for i in range(40):
                log.add("mqtt", f"message {i:02d}")
            self.assertLessEqual(os.path.getsize(path), 400)
            self.assertTrue(os.path.isfile(path + ".1"))
            self.assertFalse(os.path.exists(path + ".2"))
            recent = log.recent(limit=5)
            self.assertEqual([r["message"] for r in recent], [f"message {i:02d}" for i in range(35, 40)])

    def test_size_survives_a_new_instance(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(events, "MAX_BYTES", 400):
            path = os.path.join(d, "integration_manager", "events.jsonl")
            events.Events(path).add("boot", "x" * 300)
            events.Events(path).add("boot", "y" * 300)  # would pass the cap: rotated first
            self.assertTrue(os.path.isfile(path + ".1"))
            self.assertLessEqual(os.path.getsize(path), 400)


class ProcessLogRotationTest(unittest.TestCase):
    def test_files_capped_and_ids_continue_across_restarts(self):
        import logging

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "integration_manager", "process.log")
            logger = logging.getLogger("hri.test.rotation")
            logger.propagate = False
            handler = logbuffer.FileLogHandler(path, max_bytes=600, keep=2)
            logger.addHandler(handler)
            try:
                for i in range(60):
                    logger.warning("line %03d %s", i, "z" * 40)
            finally:
                logger.removeHandler(handler)
                handler.close()
            files = sorted(f for f in os.listdir(os.path.dirname(path)) if f.startswith("process.log"))
            self.assertEqual(files, ["process.log", "process.log.1", "process.log.2"])
            for f in files:
                self.assertLessEqual(os.path.getsize(os.path.join(os.path.dirname(path), f)), 600)
            with open(path, encoding="utf-8") as fh:
                last = json.loads(fh.read().splitlines()[-1])["id"]
            again = logbuffer.FileLogHandler(path, max_bytes=600, keep=2)
            try:
                self.assertEqual(next(again._ids), last + 1)
            finally:
                again.close()


class HistoryCapTest(unittest.TestCase):
    def test_answer_has_at_most_history_points(self):
        device = md.ManagerDevice.__new__(md.ManagerDevice)
        now = time.time()
        device._history = collections.deque([[int(now - (5000 - i) * 60), 100.0 + i, 1.0, 2.0, 5.0, 20.0] for i in range(5000)])
        with mock.patch.object(md.ManagerDevice, "history_hours", return_value=120):
            answer = device.history(120)
        self.assertLessEqual(len(answer["rows"]), md.HISTORY_POINTS)
        self.assertEqual(answer["samples"], 5000)
        self.assertEqual(answer["rows"][-1][0], device._history[-1][0])  # the newest sample is never averaged away


class ComponentKeyClashTest(unittest.TestCase):
    def _publisher(self, entity_ids):
        pub = object.__new__(mp.MqttPublisher)
        pub.config = mp.MqttConfig()
        pub.rules = mock.Mock()
        pub.rules.for_entity.return_value = {}
        pub.rules.apply_component.side_effect = lambda comp, rule: comp
        pub._collision_warned = set()
        pub._live_base = "hass_demo"
        pub._live_prefix = None
        pub._key_provider = lambda: "hass_demo"
        pub.hass = mock.Mock()
        pub.hass.states.async_all.return_value = [mock.Mock(entity_id=e, domain=e.split(".")[0]) for e in entity_ids]
        pub.hass.config.components = set()
        return pub

    def _group(self, pub):
        registry = mock.Mock(entities={})
        registry.async_get.return_value = None
        with mock.patch("homeassistant.helpers.entity_registry.async_get", return_value=registry), \
                mock.patch.object(mp, "platform_of", return_value="demo"), \
                mock.patch.object(mp.disc, "build_component", side_effect=lambda hass, state, *a: {"platform": state.domain}), \
                mock.patch.object(mp.disc, "device_block", return_value=("dev1", {"name": "d"})), \
                mock.patch.object(mp.MqttPublisher, "_topic_for", return_value="t"):
            return pub._group_by_device()

    def test_second_entity_with_the_same_key_is_skipped_and_warned_once(self):
        pub = self._publisher(["image_processing.x", "image.processing_x", "sensor.other"])
        with self.assertLogs(mp._LOGGER, level="WARNING") as logs:
            groups, counts = self._group(pub)
            self._group(pub)  # a republish does not warn again
        self.assertEqual(sorted(groups["dev1"][1]), ["image_processing.x", "sensor.other"])
        self.assertEqual(counts["collisions"], 1)
        self.assertEqual(len([m for m in logs.output if "already used by" in m]), 1)

    def test_no_clash_no_warning(self):
        pub = self._publisher(["sensor.a", "sensor.b"])
        groups, counts = self._group(pub)
        self.assertEqual(sorted(groups["dev1"][1]), ["sensor.a", "sensor.b"])
        self.assertEqual(counts["collisions"], 0)


if __name__ == "__main__":
    unittest.main()
