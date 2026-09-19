"""The Entities page's marker for the domains the main Home Assistant only learned in 2026.5, run for real under
node (tests/js/entnote.mjs): date, time and datetime carry it, a domain that has always had an MQTT platform does
not, an entity_id that merely contains the word does not, and an entity kept off MQTT is painted as before.

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI)."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "entnote.mjs")

MARK = "HA 2026.5+"


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class EntitiesPageNewHomeAssistantMarkTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def marks(self, entity_id):
        return [t for t in self.out["rows"][entity_id]["tags"] if t["text"] == MARK]

    def test_every_row_was_painted(self):
        self.assertEqual(self.out["painted"], 8)

    def test_the_three_domains_that_need_2026_5_are_marked(self):
        for entity_id in ("date.oven_service", "time.wake_up", "datetime.next_clean"):
            with self.subTest(entity_id=entity_id):
                self.assertEqual(len(self.marks(entity_id)), 1, f"{entity_id} carries no {MARK} tag")

    def test_the_mark_says_what_an_older_main_instance_does_and_what_to_do_about_it(self):
        title = self.marks("date.oven_service")[0]["title"]
        self.assertIn("only from Home Assistant 2026.5", title)
        self.assertIn("the whole discovery payload of this device, not just this entity", title)
        self.assertIn("Set main_ha_version on the MQTT page", title)  # the setting handles it
        self.assertIn("or exclude it from MQTT", title)                # and the old way still works

    def test_the_mark_is_told_apart_from_the_tag_next_to_it(self):
        tags = self.out["rows"]["time.wake_up"]["tags"]
        self.assertEqual([t["text"] for t in tags], ["disc", MARK])  # the reach tag stays, the mark comes after it
        self.assertEqual([t["class"] for t in tags], ["tag ok", "tag bad"])

    def test_a_domain_that_always_had_an_mqtt_platform_is_not_marked(self):
        for entity_id in ("sensor.kitchen_humidity", "button.restart"):
            with self.subTest(entity_id=entity_id):
                self.assertEqual(self.marks(entity_id), [])

    def test_the_domain_is_the_mark_not_the_word_in_the_id(self):
        self.assertEqual(self.marks("sensor.update_date"), [])
        self.assertEqual([t["text"] for t in self.out["rows"]["sensor.update_date"]["tags"]], ["disc"])

    def test_an_entity_kept_off_mqtt_is_painted_as_before(self):
        excluded = self.out["rows"]["date.holiday"]  # a date entity, but nothing of it reaches the main instance
        self.assertEqual(excluded["text"], "excluded")
        self.assertEqual([(t["class"], t["text"]) for t in excluded["tags"]], [("tag warn", "excluded")])

    def test_a_domain_with_no_mqtt_platform_at_all_still_reads_mirror(self):
        mirrored = self.out["rows"]["weather.home"]
        self.assertEqual([(t["class"], t["text"]) for t in mirrored["tags"]], [("tag warn", "mirror")])


if __name__ == "__main__":
    unittest.main()
