"""The pages after the twelfth review, run under node (tests/js/r12_web_pages.mjs).

m15: a logout the volume refused ended every session only until a restart, and
the page went to /login without a word.

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI).  Every test fails on the tree before the fix."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "r12_web_pages.mjs")


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class PagesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_a_logout_the_volume_refused_says_so_before_leaving(self):
        refused = self.out["logout"]["refused"]
        self.assertEqual(refused["sent"], ["/api/logout"])
        self.assertEqual(refused["alerts"], ["logged out, but the logout could not be recorded on the volume (No space left on device)"])
        self.assertEqual(refused["href"], "/login")

    def test_a_recorded_logout_leaves_without_a_word(self):
        self.assertEqual(self.out["logout"]["recorded"], {"alerts": [], "sent": ["/api/logout"], "href": "/login"})
