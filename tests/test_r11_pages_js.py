"""N14: since static/hri.js turns an answer that is not JSON into {ok: false, error}, a caller that never looked at
ok took any failure for a success.  The callers run for real under node (tests/js/r11_pages.mjs) against that
error object: each must show the error and not what a success would have shown.

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI).  Every test fails on the tree before the fix."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "r11_pages.mjs")
ERROR = "HTTP 500: 500 Internal Server Error"


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class FailedRequestTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_mqtt_republish_and_reconnect(self):
        """Republish flashed "republished undefined entities", Reconnect "MQTT reconnecting"."""
        self.assertEqual(self.out["mqtt"]["logs"], [f"ERROR: {ERROR}", f"ERROR: {ERROR}"])

    def test_cutover_preconditions(self):
        """cstatus() painted five precondition facts (no integration running, MQTT disconnected, ...) read off the error."""
        shown = self.out["cutover_status"]["shown"]
        self.assertEqual(shown, ERROR)

    def test_abort_keeps_the_flow_it_could_not_abort(self):
        """Abort logged "aborted" and dropped the flow id although the DELETE failed."""
        abort = self.out["abort"]
        self.assertEqual(abort["sent"], ["api/flow/F1"])
        self.assertEqual(abort["logs"], [f"ERROR: abort failed: {ERROR}"])
        self.assertEqual(abort["flow"], {"id": "F1", "kind": "config"})
        self.assertFalse(abort["stepcard_hidden"])

    def test_config_entry_delete_and_reload(self):
        """Delete and Reload redrew the table and said nothing."""
        entries = self.out["entries"]
        self.assertEqual(entries["sent"], ["api/entries/e1/delete", "api/entries/e1/reload"])
        self.assertEqual(entries["delete_logs"], [f"error: {ERROR}"])
        self.assertEqual(entries["reload_logs"], [f"error: {ERROR}"])

    def test_patch_delete(self):
        """Delete redrew the list and said nothing."""
        self.assertEqual(self.out["patch_delete"]["shown"], f"ERROR: {ERROR}")

    def test_import_clear(self):
        """Clear said "cleared" and forgot the inspected backup, which the server had kept (an import running)."""
        clear = self.out["import_clear"]
        self.assertEqual(clear["shown"], f"ERROR: {ERROR}")
        self.assertTrue(clear["inspected_kept"])


if __name__ == "__main__":
    unittest.main()
