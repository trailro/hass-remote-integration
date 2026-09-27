"""The MQTT page shows where the base topic comes from (tests/js/mqtt_identity.mjs): the plain hass_<domain>,
HRI_INSTANCE, or the identity this volume already published under, with a Move button to the one HRI_INSTANCE gives
that says what the main Home Assistant loses; an invalid HRI_INSTANCE is shown as the reason MQTT stays down.

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI)."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "mqtt_identity.mjs")


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class BrokerLineTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_default(self):
        o = self.out["default"]
        self.assertIn("hass_demo/…", o["text"])
        self.assertIn("HRI_INSTANCE is not set", o["text"])
        self.assertIsNone(o["button"])

    def test_instance(self):
        o = self.out["instance"]
        self.assertIn("hass_demo-garage/…", o["text"])
        self.assertIn("from HRI_INSTANCE garage", o["text"])
        self.assertIsNone(o["button"])

    def test_remembered_offers_the_move_with_its_cost(self):
        o = self.out["remembered"]
        self.assertIn("hass_demo/…", o["text"])
        self.assertIn("kept: this volume already published the running integration under it", o["text"])
        self.assertIn("HRI_INSTANCE garage would give hass_demo-garage", o["text"])
        self.assertEqual(o["button"], "Move to hass_demo-garage")
        self.assertIn("and clear hass_demo on the broker", o["text"])
        self.assertIs(o["box"], False)  # leaving the old names is the default
        self.assertEqual(len(o["confirms"]), 1)
        self.assertIn("What hass_demo published stays on the broker", o["confirms"][0])
        self.assertIn("keeps those entities and devices, unavailable", o["confirms"][0])
        self.assertEqual(o["sent"], [["api/mqtt/move_identity", {"to": "hass_demo-garage", "clear": False}]])

    def test_clearing_the_old_names_says_it_deletes_other_containers_entities(self):
        o = self.out["remembered_clear"]
        self.assertEqual(len(o["confirms"]), 1)
        self.assertIn("deletes those entities and devices", o["confirms"][0])
        self.assertIn("also deletes the entities of any OTHER container that publishes under hass_demo", o["confirms"][0])
        self.assertEqual(o["sent"], [["api/mqtt/move_identity", {"to": "hass_demo-garage", "clear": True}]])

    def test_an_unused_invalid_instance_is_a_warning_next_to_the_kept_identity(self):
        o = self.out["warned"]
        self.assertIn("hass_demo/…", o["text"])
        self.assertIn("HRI_INSTANCE='<i>x</i>' is not an instance name (not used: demo keeps hass_demo", o["text"])
        self.assertEqual(o["italic"], 0)
        self.assertIsNone(o["button"])

    def test_no_id_format_choice_where_the_record_holds_one(self):
        for name in ("default", "instance", "remembered", "warned", "invalid"):
            self.assertEqual(self.out[name]["formats"], [], name)

    def test_an_undecided_id_format_can_be_chosen_with_its_cost(self):
        o = self.out["undecided"]
        self.assertIn("discovery waits", o["text"])
        self.assertEqual(o["formats"], ["Keep hass_demo_…", "Use hass_demo-…"])
        self.assertEqual(len(o["confirms"]), 1)
        self.assertIn("1 keeps the ids this container published before (hass_demo_…", o["confirms"][0])
        self.assertIn("2 takes the new unambiguous ids (hass_demo-…)", o["confirms"][0])
        self.assertIn("Choosing wrong makes the main Home Assistant create every entity again, as duplicates", o["confirms"][0])
        self.assertEqual(o["sent"], [["api/mqtt/id_format", {"format": 1}]])
        self.assertEqual(self.out["undecided_new"]["sent"], [["api/mqtt/id_format", {"format": 2}]])

    def test_invalid_is_the_reason_and_escaped(self):
        o = self.out["invalid"]
        self.assertIn("HRI_INSTANCE='<b>x</b>' is not an instance name", o["text"])
        self.assertNotIn("no identity: start an integration first", o["text"])
        self.assertEqual(o["bold"], 0)
        self.assertIsNone(o["button"])


if __name__ == "__main__":
    unittest.main()
