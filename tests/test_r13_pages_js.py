"""The pages after the thirteenth review, run under node (tests/js/r13_pages.mjs).

F5: a single-select list rendered its radios as name="r_<field name>", so two
sections each holding a field of the same name shared one radio group in the
browser: the later default unchecked the earlier one, picking in one section
cleared the other, and collect() left the cleared field out.

F6: System -> Memory snapshot was a plain link, and /api/diag/memory answers
400 to a request without X-Requested-With: fetch, so the button never worked.

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI).  Every test fails on the tree before the fix."""

import json
import os
import re
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
COMPONENT = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager")
STATIC = os.path.join(COMPONENT, "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "r13_pages.mjs")


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class PagesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_each_field_is_a_radio_group_of_its_own(self):
        groups = self.out["sections"]["groups"]
        self.assertEqual([len(g) for g in groups], [1, 1, 1])
        self.assertEqual(len({g[0] for g in groups}), 3)

    def test_an_untouched_form_sends_every_section(self):
        self.assertEqual(self.out["sections"]["untouched"], {"first": {"mode": "x"}, "second": {"mode": "y"}, "mode": "y"})

    def test_picking_in_one_section_leaves_the_other(self):
        self.assertEqual(self.out["sections"]["picked"], {"first": {"mode": "x"}, "second": {"mode": "x"}, "mode": "y"})

    def test_memory_snapshot_sends_the_header_and_saves_the_answer(self):
        ok = self.out["memsnap"]["ok"]
        self.assertIsNotNone(ok)
        self.assertEqual(ok["sent"], [{"url": "api/diag/memory", "headers": {"X-Requested-With": "fetch"}, "disabled": True}])
        self.assertEqual(ok["alerts"], [])
        self.assertEqual(len(ok["saved"]), 1)
        self.assertRegex(ok["saved"][0]["name"], r"^hri-memory-\d{8}T\d{6}\.json$")
        self.assertEqual(ok["saved"][0]["href"], 'blob:{"rss_mb": 1}')
        self.assertFalse(ok["disabled_after"])

    def test_a_probe_already_running_is_shown(self):
        busy = self.out["memsnap"]["busy"]
        self.assertIsNotNone(busy)
        self.assertEqual(busy["alerts"], ["Memory snapshot: a memory probe is already running: try again when it has finished"])
        self.assertEqual(busy["saved"], [])
        self.assertFalse(busy["disabled_after"])

    def test_an_answer_that_is_not_json_is_shown_by_status(self):
        broken = self.out["memsnap"]["broken"]
        self.assertIsNotNone(broken)
        self.assertEqual((broken["alerts"], broken["saved"]), (["Memory snapshot: HTTP 500"], []))


class SourceTest(unittest.TestCase):

    def test_no_radio_group_is_named_after_its_field(self):
        with open(os.path.join(STATIC, "config.js"), encoding="utf-8") as fh:
            self.assertEqual(re.findall(r'name="r_', fh.read()), [])

    def test_the_memory_snapshot_is_not_a_link(self):
        with open(os.path.join(COMPONENT, "templates", "system.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn('href="api/diag/memory"', html)
        self.assertIn('<button id="memsnap"', html)
        self.assertNotIn("<script", html.replace("<!--js-->", ""))


if __name__ == "__main__":
    unittest.main()
