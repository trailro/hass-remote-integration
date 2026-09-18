"""static/logfiles.js runs for real under node against the answers in
tests/js/log_download.mjs: the Download button next to the tail controls.

The server half is tests/test_log_download.py.  Needs node, which the
container the unit tests run in does not have: it skips there and runs
wherever node is installed (a developer machine, CI)."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "log_download.mjs")


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class LogDownloadButtonTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_the_selected_file_is_asked_for_by_its_id_with_the_header(self):
        """A plain link cannot send X-Requested-With, and two files can share a
        label: the id is what says which file is saved."""
        asked = self.out["download"]["asked"]
        self.assertEqual([a["url"] for a in asked], ["/api/log_files/download?id=id-beta"])
        self.assertEqual(asked[0]["headers"], {"X-Requested-With": "fetch"})
        self.assertTrue(asked[0]["disabled"], "the button is held while the file is being fetched")

    def test_the_file_is_saved_under_the_name_the_server_gave(self):
        saved = self.out["download"]["saved"]
        self.assertEqual([s["name"] for s in saved], ["logs-session-token.log"])
        self.assertEqual(saved[0]["href"], "blob:line one\nline two\n")
        self.assertEqual(self.out["download"]["alerts"], [])
        self.assertFalse(self.out["download"]["disabled_after"])

    def test_a_refusal_is_shown_and_nothing_is_saved(self):
        self.assertEqual(self.out["refused"], {"saved": [], "alerts": ["Download: unknown file"], "disabled_after": False})

    def test_the_button_is_off_while_the_integration_writes_no_log_file(self):
        self.assertTrue(self.out["empty"]["disabled"])

    def test_an_answer_without_a_file_name_still_saves_something(self):
        self.assertEqual([s["name"] for s in self.out["fallback"]["saved"]], ["log.txt"])


if __name__ == "__main__":
    unittest.main()
