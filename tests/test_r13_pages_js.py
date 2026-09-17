"""The pages after the thirteenth review, run under node (tests/js/r13_pages.mjs).

F5: a single-select list rendered its radios as name="r_<field name>", so two
sections each holding a field of the same name shared one radio group in the
browser: the later default unchecked the earlier one, picking in one section
cleared the other, and collect() left the cleared field out.

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


class SourceTest(unittest.TestCase):

    def test_no_radio_group_is_named_after_its_field(self):
        with open(os.path.join(STATIC, "config.js"), encoding="utf-8") as fh:
            self.assertEqual(re.findall(r'name="r_', fh.read()), [])


if __name__ == "__main__":
    unittest.main()
