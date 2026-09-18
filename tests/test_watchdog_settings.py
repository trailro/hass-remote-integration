"""The watchdog setting itself: the defaults, what POST /api/settings accepts and
clamps, and what a hand-edited settings.json is read as."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

from custom_components.integration_manager.manage_views import SettingsView
from custom_components.integration_manager.settings import DEFAULTS, WATCHDOG_BOUNDS, Settings


class _Request:
    content_type = "application/json"

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)


class DefaultsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_off_with_sane_windows(self):
        st = Settings(self.dir)
        self.assertEqual(st.watchdog(), {"enabled": False, "after_min": 15, "min_interval_min": 60, "max_per_day": 3})

    def test_public_carries_the_four_values(self):
        pub = Settings(self.dir).public()
        for key in ("watchdog", "watchdog_after_min", "watchdog_min_interval_min", "watchdog_max_per_day"):
            self.assertIn(key, pub)
        self.assertIs(pub["watchdog"], False)

    def test_a_hand_edited_file_is_clamped_and_never_crashes(self):
        with open(os.path.join(self.dir, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"watchdog": "yes", "watchdog_after_min": 1, "watchdog_min_interval_min": 99999,
                       "watchdog_max_per_day": "lots"}, fh)
        st = Settings(self.dir)
        self.assertEqual(st.watchdog(), {"enabled": True, "after_min": WATCHDOG_BOUNDS["watchdog_after_min"][0],
                                         "min_interval_min": WATCHDOG_BOUNDS["watchdog_min_interval_min"][1],
                                         "max_per_day": DEFAULTS["watchdog_max_per_day"]})

    def test_off_is_off_however_it_is_spelled(self):
        for word in ("false", "no", "off", "", "0"):
            with self.subTest(word=word):
                st = Settings(self.dir)
                st.data["watchdog"] = word
                self.assertFalse(st.watchdog()["enabled"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = Settings(self.dir)
        self.installer = SimpleNamespace(settings=self.settings, hass=None, _releases_cache={}, scheduler=None)
        self.view = SettingsView(self.installer)

    def post(self, body):
        with mock.patch.object(type(self.settings), "async_save", mock.AsyncMock()):
            resp = asyncio.run(self.view.post(_Request(body)))
        return json.loads(resp.body.decode())

    def test_saving_the_four_values(self):
        out = self.post({"watchdog": True, "watchdog_after_min": 20, "watchdog_min_interval_min": 90,
                         "watchdog_max_per_day": 5})
        self.assertTrue(out["ok"])
        self.assertEqual(self.settings.watchdog(), {"enabled": True, "after_min": 20, "min_interval_min": 90, "max_per_day": 5})
        self.assertEqual(out["watchdog_after_min"], 20)

    def test_the_numbers_are_clamped_not_refused(self):
        self.post({"watchdog_after_min": 0, "watchdog_min_interval_min": 10 ** 9, "watchdog_max_per_day": 0})
        self.assertEqual(self.settings.watchdog(), {"enabled": False, "after_min": 5, "min_interval_min": 1440, "max_per_day": 1})

    def test_a_number_that_is_not_a_number_is_refused(self):
        out = self.post({"watchdog_after_min": "soon"})
        self.assertFalse(out["ok"])
        self.assertIn("watchdog_after_min", out["error"])

    def test_the_switch_must_be_a_boolean(self):
        out = self.post({"watchdog": "on"})
        self.assertFalse(out["ok"])
        self.assertIn("true/false", out["error"])

    def test_a_save_that_touches_nothing_else_leaves_the_rest_alone(self):
        before = dict(self.settings.data)
        self.post({"watchdog": True})
        changed = {k for k in before if before[k] != self.settings.data[k]}
        self.assertEqual(changed, {"watchdog"})


if __name__ == "__main__":
    unittest.main()
