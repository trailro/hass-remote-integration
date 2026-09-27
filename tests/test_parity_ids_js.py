"""The Cutover page while the id format is undecided, (tests/js/parity_ids.mjs).

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI)."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "parity_ids.mjs")


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class CutoverIdsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_the_status_says_the_id_format_is_undecided(self):
        o = self.out["cutover_status"]
        self.assertIn("id format undecided: nothing is announced or compared until it is decided", o["shown"])
        self.assertIn("discovery waits: whether hass_demo keeps the ids ... (<b>x</b>)", o["shown"])
        self.assertIn("the MQTT page can set it", o["shown"])
        self.assertEqual(o["bold"], 0)

    def test_parity_shows_the_state_and_no_numbers(self):
        o = self.out["parity"]
        self.assertIsNone(o["result"])
        self.assertIn("id format undecided: nothing is compared until it is decided", o["shown"])


if __name__ == "__main__":
    unittest.main()
