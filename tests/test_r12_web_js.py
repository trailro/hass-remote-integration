"""The pages after the twelfth review, run under node (tests/js/r12_web_pages.mjs).

m15: a logout the volume refused ended every session only until a restart, and
the page went to /login without a word.

C3: the call form put the target's entity domains, read from an integration's
services.yaml, into the page as markup.

C1 (this review): two lines of the MQTT page escaped their text and then assigned
it to .textContent, which parses no HTML, so a service call carrying quotes or an
ampersand was shown with &quot; and &amp; in it.

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

    def test_a_refused_subscription_shows_while_connected(self):
        refused = self.out["mqconn"]["refused"]
        self.assertIn("connected", refused["text"])
        self.assertIn("subscription refused: <b>cmd/#</b> (Not authorized)", refused["text"])
        self.assertEqual(refused["bold"], 0)
        self.assertNotIn("refused", self.out["mqconn"]["fine"]["text"])

    def test_the_target_domains_are_text(self):
        self.assertEqual(self.out["target_domains"], {
            "images": 0, "label": "target entity_id(s) comma separated · <img src=x onerror=alert(1)>/light, switch"})

    def test_the_mqtt_command_and_call_lines_show_the_payload_as_it_is(self):
        """C1: .textContent parses no HTML, so esc() only put &quot; and &amp; on screen."""
        text = self.out["mqtt_text"]
        self.assertEqual(text["mqcmd"], '4 · last: call light.turn_on {"brightness": 255} & wait '
                                        '· topic: hass_demo/cmd/<domain>/<object_id>/<field>')
        self.assertEqual(text["mqcall"], '12 services published on hass_demo/services/<domain> · calls: 7 '
                                         '· last: light.turn_on {"entity_id": "light.hall"} '
                                         '· topic: hass_demo/call/<domain>/<service> (JSON)')
        for line in text.values():
            self.assertNotIn("&quot;", line)
            self.assertNotIn("&amp;", line)
