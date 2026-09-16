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

    def test_a_fractional_duration_can_be_submitted(self):
        # cv.time_period_dict takes floats, so half a second is a duration, not a typing mistake
        self.assertEqual(self.sent["duration_fractional_seconds"]["delay"]["seconds"], 0.5)
        self.assertEqual(self.sent["duration_fractional_minutes"]["delay"]["minutes"], 1.5)

    def test_a_duration_part_lets_the_browser_hold_a_fraction(self):
        # step=1 would make the browser call the 0.5 above out of range and round the user's value away
        self.assertEqual(self.sent["duration_part_step"]["seconds"], "any")

    def test_a_custom_choice_survives_an_untouched_submit(self):
        for key in ("custom_multi_dropdown", "custom_multi_list"):
            with self.subTest(shape=key):
                self.assertEqual(self.sent[key]["choices"], ["a", "custom"])
        self.assertEqual(self.sent["custom_single_default"]["choice"], "custom")
        self.assertEqual(self.sent["custom_single_list_default"]["choice"], "custom")

    def test_a_custom_choice_can_be_typed(self):
        self.assertEqual(self.sent["custom_multi_typed"]["choices"], ["a", "x", "y"])
        self.assertEqual(self.sent["custom_single_typed"]["choice"], "typed")

    def test_a_selector_without_custom_value_offers_no_box_to_type_in(self):
        self.assertFalse(self.sent["no_custom_box_without_custom_value"]["has"])


class CustomValueParityTest(unittest.TestCase):
    """The two pages that draw a select from a selector answer custom_value the same way.

    They do not share code: the services page builds HTML strings, the config flow page builds
    elements, and the only file both load (static/hri.js) is not this change's to edit.  What is
    shared is the contract, and this test is where it is written down."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(CONFIG_JS_PATH), "services.js"), encoding="utf-8") as fh:
            self.services_js = fh.read()

    def test_both_pages_type_custom_values_into_one_comma_separated_box(self):
        for name, src in (("config.js", CONFIG_JS), ("services.js", self.services_js)):
            with self.subTest(page=name):
                self.assertIn("custom", src)
                self.assertIn("other values, comma separated", src)
                self.assertIn("split(',')", src)

    def test_both_pages_keep_a_default_the_options_do_not_list(self):
        self.assertIn("opts.push({value:v,label:v})", CONFIG_JS)  # shown as a choice of its own
        self.assertIn("extra=[...pre].filter(x=>!listed.has(x))", self.services_js)  # put back in the custom box


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
