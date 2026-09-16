"""External web review: what the config-flow page sends for a number field,
the entry table's dead action, where the manager's views are guarded, and what
the README promises the Cutover page compares."""

import os
import re
import unittest

from custom_components.integration_manager import flows

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IM_DIR = os.path.dirname(flows.__file__)
with open(os.path.join(IM_DIR, "static", "config.js"), encoding="utf-8") as _fh:
    CONFIG_JS = _fh.read()
with open(os.path.join(IM_DIR, "http_util.py"), encoding="utf-8") as _fh:
    HTTP_UTIL = _fh.read()

HELPERS = CONFIG_JS[:CONFIG_JS.index("function collect(")]
COLLECT = CONFIG_JS[CONFIG_JS.index("function collect("):CONFIG_JS.index("// The integration's own translations")]
ENTRIES = CONFIG_JS[CONFIG_JS.index("async function entries()"):]


def branch(kind):
    """The one line of collect() that handles this data-kind."""
    return next(line for line in COLLECT.splitlines() if f"k==='{kind}'" in line)


class NumericFieldsTest(unittest.TestCase):
    """static/config.js: a number field sends what the user typed, or nothing."""

    def test_the_whole_value_is_read_not_the_part_parseInt_understands(self):
        # parseInt("1e2") is 1 and parseInt("1.5") is 1: the schema then validates
        # a number the user never typed, and the page can no longer explain it
        for kind in ("integer", "number"):
            with self.subTest(kind=kind):
                self.assertNotIn("parseInt", branch(kind))
                self.assertNotIn("parseFloat", branch(kind))
                self.assertIn("num(el.value,n,", branch(kind))

    def test_a_value_that_is_not_a_finite_whole_number_stops_the_submit(self):
        self.assertIn("Number.isFinite(v)", HELPERS)
        self.assertIn("Number.isInteger(v)", HELPERS)
        self.assertEqual(HELPERS.count("throw new FieldError"), 2)

    def test_the_duration_parts_are_whole_numbers_and_an_empty_part_is_still_zero(self):
        self.assertNotIn("parseInt", branch("duration"))
        self.assertIn("===''?0:num(", branch("duration"))

    def test_a_colour_is_six_hex_digits_so_no_component_can_come_out_NaN(self):
        self.assertIn("[0-9a-f]{2}", branch("color"))
        self.assertNotIn("(..)", branch("color"))

    def test_an_empty_field_is_still_left_out_of_the_user_input(self):
        for kind in ("integer", "number", "object", "time", "date", "datetime"):
            with self.subTest(kind=kind):
                self.assertIn("continue", branch(kind))

    def test_the_message_lands_under_the_field_collect_refused(self):
        self.assertIn("w._err.textContent=e.detail", COLLECT)
        submit = next(line for line in CONFIG_JS.splitlines() if "$('#submit').onclick" in line)
        self.assertIn("showFieldError(e)", submit)
        self.assertNotIn("invalid JSON", submit)  # not every collect() refusal is a JSON one


class EntryTableActionsTest(unittest.TestCase):
    def test_every_action_handled_is_an_action_the_table_draws(self):
        drawn = set(re.findall(r'data-a="([a-z_]+)"', ENTRIES))
        handled = set(re.findall(r"a==='([a-z_]+)'", ENTRIES))
        self.assertEqual(handled, drawn)
        # the flows in progress get their own buttons and their own handler,
        # with a flow id where this one has an entry id
        self.assertIn("flow={id:f.flow_id,kind:'config'}", ENTRIES)


class ViewGuardDocTest(unittest.TestCase):
    def test_the_module_doc_names_both_guards_in_front_of_a_view(self):
        doc = HTTP_UTIL[:HTTP_UTIL.index('"""', 3)]
        self.assertIn("hostguard.py", doc)
        self.assertIn("auth.py", doc)
        self.assertIn("HRI_PASSWORD", doc)
        self.assertNotIn("unauthenticated", HTTP_UTIL)  # true of HA's auth only, and only without a password


class ParityDocTest(unittest.TestCase):
    def test_the_readme_says_which_two_sets_of_entities_are_compared(self):
        if not os.path.isfile(os.path.join(ROOT, "README.md")):
            self.skipTest("README not copied next to the tests")
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            section = fh.read().split("### 5. Move over from your main Home Assistant", 1)[1]
        step = re.sub(r"\s+", " ", section.split("\n3. ", 1)[0])  # the wrapping is not the point
        # what compute_parity() really puts side by side: our announced components
        # against the parent's mqtt entities under our prefix, matched by unique id
        self.assertIn("announces over MQTT with the MQTT entities your main HA created", step)
        self.assertIn("by unique id", step)
        self.assertIn("not part of the comparison", step)


if __name__ == "__main__":
    unittest.main()
