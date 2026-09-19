"""static/logs.js and static/logfiles.js run for real under node, against the
answers in tests/js/log_pages.mjs (F13 and F16, the client half; the server
half is tests/test_log_follow.py).

F19, in the same harness: LogLevelView refuses a level through json_message,
which sends {"message": ...}, while the page read only .error -- so the reason
(the root logger, an unknown level, the 50-logger cap) never reached the
operator, who saw "error: 400" and the select revert.

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

    def test_a_truncated_follow_reads_on_at_once_and_stops_at_the_bound(self):
        """R11: fetchLogs did not act on truncated, so a follower 60k records
        behind gained one page of 200 every 3 s (~15 minutes)."""
        catch_up = self.out["logs_catch_up"]
        self.assertEqual(catch_up["behind"], {"reads": 4, "last_since": 601, "notice": False})
        self.assertEqual(catch_up["never"]["reads"], 26)  # one read and CATCH_UP more, then the next tick
        self.assertTrue(catch_up["never"]["notice"])

    def test_after_a_restart_the_selection_is_found_again_by_its_label(self):
        """R11 N15: no option had the old id and the browser selected the first
        file, silently."""
        restart = self.out["logfiles_restart"]
        self.assertEqual(restart["unique"], {"selected": "b-restarted", "shown": "contents of b", "note": ""})

    def test_a_label_several_files_show_is_not_resolved_silently(self):
        shared = self.out["logfiles_restart"]["shared"]
        self.assertEqual((shared["selected"], shared["shown"]), ("a-restarted", "contents of a"))
        self.assertIn("2 files show logs/session-token=***", shared["note"])


    def test_a_refused_log_level_says_why(self):
        """F19: only the status reached the page, although the point of the line is the explanation."""
        refused = self.out["log_level_refused"]
        self.assertEqual(refused["message"], ["error: the root logger is not yours to change: pick a logger below it"])
        self.assertEqual(refused["cap"], ["error: already 50 loggers with a level of their own: reset one first"])

    def test_a_refusal_that_does_carry_error_still_works(self):
        self.assertEqual(self.out["log_level_refused"]["error"], ["error: unknown level FINE"])

    def test_an_answer_that_is_not_json_falls_back_to_the_status(self):
        self.assertEqual(self.out["log_level_refused"]["not_json"], ["error: 502"])

    def test_an_accepted_level_says_nothing(self):
        self.assertEqual(self.out["log_level_refused"]["accepted"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
