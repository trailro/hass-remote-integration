"""static/logs.js and static/logfiles.js run for real under node, against the
answers in tests/js/log_pages.mjs (F13 and F16, the client half; the server
half is tests/test_log_follow.py).

Needs node, which the container the unit tests run in does not have: it skips
there and runs wherever node is installed (a developer machine, CI).  Both
tests fail on the tree before the fix."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not installed
STATIC = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "log_pages.mjs")


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class LogPagesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, STATIC], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_the_follow_continues_from_the_cursor_of_an_empty_page(self):
        """fetchLogs advanced lastId only from the records it was given, so after
        a page the search emptied it asked for since_id=1 on every poll."""
        follow = self.out["logs_follow"]
        self.assertEqual(follow["since_ids"][:3], [0, 1, 201])
        self.assertTrue(follow["shown"][-1].endswith("needle: actual failure"), follow["shown"])

    def test_each_option_of_two_files_with_one_label_opens_its_own_file(self):
        """The option value was the masked name, the same for both files."""
        files = self.out["logfiles"]
        self.assertEqual(files["labels"], 2)
        self.assertEqual([o.get("shown") for o in files["opened"]], ["contents of alpha", "contents of beta"])

    def test_a_selection_the_server_no_longer_knows_loads_the_new_listing(self):
        """Ids are new after a restart: the page lists the files again and loads
        the file now selected instead of staying empty until the next listing."""
        after = self.out["logfiles"]["after_restart"]
        self.assertEqual(after, {"selected": "id-alpha-restarted", "shown": "contents of alpha"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
