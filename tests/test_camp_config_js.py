"""static/config.js run for real: field() and collect() against the small DOM
in tests/js/config_form.mjs, plus what the page shows when a flow ends.

The execution part needs node, which the container the unit tests run in does
not have; it skips there and runs wherever node is installed (a developer
machine, CI).  The rendering assertions read the source, as the renderer tests
next door do."""

import json
import os
import shutil
import subprocess
import unittest

# by path, not through an import of the component: this file must also run where Home Assistant is not
# installed (a developer machine with node, where the execution part is the point)
CONFIG_JS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static", "config.js")
HARNESS = os.path.join(os.path.dirname(__file__), "js", "config_form.mjs")
with open(CONFIG_JS_PATH, encoding="utf-8") as _fh:
    CONFIG_JS = _fh.read()


@unittest.skipUnless(shutil.which("node") and os.path.isfile(HARNESS), "node (or the harness) is not available here")
class SubmittedValuesTest(unittest.TestCase):
    """What an untouched form sends back for the selectors HA offers."""

    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), HARNESS, CONFIG_JS_PATH], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"the harness failed: {out.stderr.strip()}")
        cls.sent = json.loads(out.stdout)

    def test_milliseconds_are_offered_and_kept(self):
        self.assertEqual(self.sent["duration_ms"]["delay"]["milliseconds"], 500)

    def test_a_unit_the_form_does_not_show_survives_a_submit(self):
        # enable_millisecond is off, so there is no input for it: the 500 ms belong to the value all the same
        self.assertEqual(self.sent["duration_ms_not_offered"]["delay"]["milliseconds"], 500)

    def test_a_plain_duration_is_unchanged(self):
        self.assertEqual(self.sent["duration_plain"]["delay"], {"hours": 1, "minutes": 30, "seconds": 0})

    def test_a_multiple_select_sends_a_list_in_list_mode_too(self):
        self.assertEqual(self.sent["list_multiple"]["choices"], ["a", "b"])
        self.assertEqual(self.sent["list_multiple_scalar_default"]["choices"], ["a"])  # HA refuses the bare "a"

    def test_single_choice_and_dropdown_are_unchanged(self):
        self.assertEqual(self.sent["list_single"]["choice"], "b")
        self.assertEqual(self.sent["dropdown_multiple"]["choices"], ["b"])


class FlowEndingTest(unittest.TestCase):
    """The page's own vocabulary for a finished flow."""

    def setUp(self):
        self.render = CONFIG_JS[CONFIG_JS.index("function render("):CONFIG_JS.index("let PROGRESS_T")]
        self.entries = CONFIG_JS[CONFIG_JS.index("async function entries("):]

    def test_an_options_flow_does_not_claim_an_entry_was_created(self):
        branch = self.render[self.render.index("r.type==='create_entry'"):self.render.index("r.type==='abort'")]
        self.assertIn("flow&&flow.kind==='options'", branch)
        self.assertIn("Options saved", branch)
        self.assertIn("opt?'':`entry_id:", branch)  # no empty "entry_id: ?" for a flow that creates none

    def test_a_successful_reauth_does_not_read_as_a_failure(self):
        branch = self.render[self.render.index("r.type==='abort'"):self.render.index("r.type==='progress'")]
        self.assertIn("/_successful$/.test(String(r.reason||''))", branch)
        self.assertIn('good?`<span class="ok">Done</span>', branch)

    def test_a_finished_flow_stops_naming_itself(self):
        self.assertIn("function done(){", CONFIG_JS)
        self.assertIn("$('#flowid').textContent='finished'", CONFIG_JS)
        for kind in ("create_entry", "abort"):
            branch = self.render[self.render.index(f"r.type==='{kind}'"):]
            self.assertIn("done();", branch[:branch.index("}else if") if "}else if" in branch else len(branch)])

    def test_a_flow_to_continue_says_which_entry_it_belongs_to(self):
        self.assertIn("titles[e.entry_id]=e.title", self.entries)
        self.assertIn("f.entry_id?(titles[f.entry_id]||f.entry_id.slice(0,8)", self.entries)


if __name__ == "__main__":
    unittest.main()
