"""The Entities page's marker for the domains the main Home Assistant only learned in 2026.5, run for real under
node (tests/js/entnote.mjs): date, time and datetime carry it, a domain that has always had an MQTT platform does
not, an entity_id that merely contains the word does not, and an entity kept off MQTT is painted as before.

F14, in the same harness: the Entities and Devices tables refused to repaint while anything inside them had the
focus.  The button the operator had just clicked is inside them and keeps the focus in Chrome and Edge, so the
reload that follows a successful action -- and every poll after it -- returned before painting: the page said
"ok" over the old row, a second rename posted to an id that no longer existed, and a deleted device stayed listed.

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


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class TableRepaintsAfterAnActionTest(unittest.TestCase):
    """F14: what the two tables show after a successful action, by what holds the focus when they repaint."""

    PAGES = ("entities", "devices")

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.focus = json.loads(out.stdout)["focus"]

    def case(self, page, name):
        return self.focus[page][name]

    def test_the_table_the_operator_did_not_touch_repaints(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                case = self.case(page, "nothing_focused")
                self.assertNotEqual(case["before"], case["after"])

    def test_the_button_just_clicked_does_not_stop_the_repaint(self):
        # it is inside the table and keeps the focus in Chrome and Edge: this is the finding
        for page in self.PAGES:
            with self.subTest(page=page):
                self.assertEqual(self.case(page, "button_focused"), self.case(page, "nothing_focused"))

    def test_a_rename_that_still_shows_the_old_row_is_gone(self):
        # the Entities page renamed the entity: the row must carry the new id, not the one the server dropped
        case = self.case("entities", "button_focused")
        self.assertEqual(case["before"], ["sensor.kitchen_humidity"])
        self.assertEqual(case["after"], ["sensor.humidity_kitchen"])

    def test_a_deleted_device_leaves_the_list(self):
        self.assertEqual(self.case("devices", "button_focused"), {"before": ["Old oven"], "after": []})

    def test_a_field_being_typed_in_is_still_protected(self):
        # the point of the guard: an inline edit in progress is not rebuilt under the cursor
        for page in self.PAGES:
            with self.subTest(page=page):
                case = self.case(page, "input_focused")
                self.assertEqual(case["after"], case["before"])

    def test_a_field_outside_the_table_stops_nothing(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                self.assertEqual(self.case(page, "search_focused"), self.case(page, "nothing_focused"))


if __name__ == "__main__":
    unittest.main()
