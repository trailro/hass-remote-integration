"""The config flow renderer: translations attached to a flow result, and the
selectors the page draws itself instead of handing the user a JSON textarea."""

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import flows
from custom_components.integration_manager import services_catalog as sc

IM_DIR = os.path.dirname(flows.__file__)
with open(os.path.join(IM_DIR, "static", "config.js"), encoding="utf-8") as _fh:
    CONFIG_JS = _fh.read()

EN = {
    "config": {
        "step": {
            "user": {"title": "HRI Probe", "data": {"name": "Name"}, "data_description": {"name": "No spaces."}},
            "path": {"menu_options": {"basic": "Basic", "advanced": "Advanced"}},
        },
        "error": {"invalid_name": "The name must not start or end with a space.", "unused": "never asked for"},
        "abort": {"unknown": "The flow failed with an error it did not expect."},
        "progress": {"probing": "Probing the device..."},
    },
    "options": {"step": {"init": {"title": "HRI Probe options"}}},
    "services": {"tick": {"name": "Tick", "description": "x" * 5000}},
}


class TranslationSliceTest(unittest.TestCase):
    def test_only_the_step_shown_travels(self):
        out = flows.flow_translations(EN, "config", {"type": "form", "step_id": "user"})
        self.assertEqual(list(out["step"]), ["user"])
        self.assertEqual(out["step"]["user"]["data"]["name"], "Name")
        self.assertNotIn("services", json.dumps(out))
        self.assertNotIn("path", json.dumps(out))
        self.assertLess(len(json.dumps(out)), 400)  # not the whole file, which services alone blow past

    def test_menu_labels_ride_along_with_the_step(self):
        out = flows.flow_translations(EN, "config", {"type": "menu", "step_id": "path"})
        self.assertEqual(out["step"]["path"]["menu_options"]["advanced"], "Advanced")

    def test_only_the_error_keys_this_result_names(self):
        out = flows.flow_translations(EN, "config", {"type": "form", "step_id": "user", "errors": {"name": "invalid_name", "base": "nope"}})
        self.assertEqual(list(out["error"]), ["invalid_name"])  # "nope" has no text, "unused" was not asked for

    def test_abort_reason_and_progress_action(self):
        self.assertEqual(
            flows.flow_translations(EN, "config", {"type": "abort", "reason": "unknown"})["abort"]["unknown"],
            "The flow failed with an error it did not expect.",
        )
        self.assertIn("probing", flows.flow_translations(EN, "config", {"type": "progress", "step_id": "probe", "progress_action": "probing"})["progress"])

    def test_options_root_does_not_fall_back_to_config(self):
        self.assertEqual(flows.flow_translations(EN, "options", {"type": "menu", "step_id": "init"})["step"]["init"]["title"], "HRI Probe options")
        self.assertEqual(flows.flow_translations(EN, "options", {"type": "form", "step_id": "user"}), {})

    def test_an_integration_without_translations_renders_anyway(self):
        for tr in ({}, {"config": "not a mapping"}, {"config": {"step": []}}):
            self.assertEqual(flows.flow_translations(tr, "config", {"type": "form", "step_id": "user"}), {})


def _hass(result, entry_domain="hri_probe"):
    hass = mock.Mock()
    hass.config_entries.flow.async_init = mock.AsyncMock(return_value=result)
    hass.config_entries.options.async_init = mock.AsyncMock(return_value=result)
    hass.config_entries.options.async_configure = mock.AsyncMock(return_value=result)
    hass.config_entries.async_get_entry = mock.Mock(return_value=SimpleNamespace(domain=entry_domain))
    return hass


class FlowResultCarriesTranslationsTest(unittest.TestCase):
    def test_config_flow_result(self):
        result = {"type": "form", "flow_id": "f1", "handler": "hri_probe", "step_id": "user", "data_schema": None,
                  "errors": {"name": "invalid_name"}}
        driver = flows.FlowDriver(_hass(result))
        with mock.patch.object(flows, "integration_translations", mock.AsyncMock(return_value=EN)) as load:
            out = asyncio.run(driver.start("hri_probe"))
        self.assertEqual(load.await_args.args[1], "hri_probe")
        self.assertEqual(out["translations"]["step"]["user"]["title"], "HRI Probe")
        self.assertIn("invalid_name", out["translations"]["error"])

    def test_options_flow_looks_the_domain_up_by_entry_id(self):
        """An options flow's handler is the config entry id, not the domain."""
        result = {"type": "menu", "flow_id": "f2", "handler": "0123456789abcdef", "step_id": "init", "menu_options": ["tuning"]}
        hass = _hass(result)
        driver = flows.FlowDriver(hass)
        with mock.patch.object(flows, "integration_translations", mock.AsyncMock(return_value=EN)) as load:
            out = asyncio.run(driver.options_start("0123456789abcdef"))
        hass.config_entries.async_get_entry.assert_called_with("0123456789abcdef")
        self.assertEqual(load.await_args.args[1], "hri_probe")
        self.assertEqual(out["translations"]["step"]["init"]["title"], "HRI Probe options")

    def test_no_translations_no_key(self):
        result = {"type": "form", "flow_id": "f3", "handler": "bare", "step_id": "user", "data_schema": None}
        driver = flows.FlowDriver(_hass(result))
        with mock.patch.object(flows, "integration_translations", mock.AsyncMock(return_value={})):
            out = asyncio.run(driver.start("bare"))
        self.assertNotIn("translations", out)
        self.assertEqual(out["step_id"], "user")

    def test_a_deleted_entry_does_not_break_the_result(self):
        result = {"type": "menu", "flow_id": "f4", "handler": "gone", "step_id": "init"}
        hass = _hass(result)
        hass.config_entries.async_get_entry.return_value = None
        with mock.patch.object(flows, "integration_translations", mock.AsyncMock(return_value=EN)) as load:
            out = asyncio.run(flows.FlowDriver(hass).options_configure("f4", None))
        load.assert_not_awaited()
        self.assertNotIn("translations", out)


class IntegrationTranslationsTest(unittest.TestCase):
    """The loader the flow renderer shares with the services catalog."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.hass = SimpleNamespace(async_add_executor_job=self._run)

    @staticmethod
    async def _run(func, *args):
        return func(*args)

    def _load(self, domain="demo"):
        integration = SimpleNamespace(file_path=Path(self.dir))
        with mock.patch.object(sc.loader, "async_get_integration", mock.AsyncMock(return_value=integration)):
            return asyncio.run(sc.integration_translations(self.hass, domain))

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(self._load(), {})

    def test_read_once_per_mtime(self):
        os.makedirs(os.path.join(self.dir, "translations"))
        path = os.path.join(self.dir, "translations", "en.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(EN, fh)
        sc._FILE_CACHE.pop(path, None)
        first = self._load()
        self.assertEqual(first["config"]["step"]["user"]["title"], "HRI Probe")
        self.assertIs(self._load(), first)  # same parsed object: the mtime cache, not a second parse


class RendererSelectorsTest(unittest.TestCase):
    """static/config.js: what field() draws and what collect() sends back."""

    def setUp(self):
        self.field = CONFIG_JS[CONFIG_JS.index("function field("):CONFIG_JS.index("function collect(")]
        self.collect = CONFIG_JS[CONFIG_JS.index("function collect("):CONFIG_JS.index("function render(")]
        self.render = CONFIG_JS[CONFIG_JS.index("function render("):]

    def test_every_kind_field_draws_is_a_kind_collect_sends(self):
        drawn = set()
        for m in re.finditer(r"\.dataset\.kind=([^;]+);", self.field):
            drawn.update(re.findall(r"'([a-z_]+)'", m.group(1)))
        handled = set(re.findall(r"k==='([a-z_]+)'", self.collect))
        self.assertEqual(drawn - handled - {"text"}, set())  # text is collect()'s own fallback branch
        # the time/date/datetime branch passes its kind through, so name those here
        self.assertLessEqual({"duration", "constant", "time", "date", "datetime", "color"}, handled)

    def test_duration_is_numbers_collected_into_one_object(self):
        branch = self.field[self.field.index("kind==='duration'"):self.field.index("kind==='time'")]
        for unit in ("days", "hours", "minutes", "seconds", "milliseconds"):
            self.assertIn(f"'{unit}'", branch)
        for option in ("enable_day", "enable_second", "enable_millisecond"):
            self.assertIn(option, branch)
        # the units the schema does not show are carried over rather than dropped (tests/test_camp_config_js.py runs it)
        self.assertRegex(self.collect, r"k==='duration'.*v=\{\.\.\.w\._kept\}")

    def test_a_duration_part_takes_a_fraction(self):
        # cv.time_period_dict accepts floats: 0.5 s is a duration the page must be able to hold and send back
        part = CONFIG_JS[CONFIG_JS.index("function partInput("):CONFIG_JS.index("function field(")]
        self.assertIn("i.step='any'", part)
        self.assertNotIn("i.step=1", part)
        self.assertRegex(self.collect, r"k==='duration'.*num\(w\._parts\[u\]\.value,n,false\)")

    def test_custom_value_draws_a_box_to_type_in_and_keeps_an_unlisted_default(self):
        branch = self.field[self.field.index("kind==='select'"):self.field.index("kind==='number'")]
        self.assertIn("sel.select.custom_value", branch)
        # an unlisted default is kept: a choice of its own for a single value, a box of its own among several (F18)
        self.assertIn("if(multi) extra.push(v); else opts.push({value:v,label:v});", branch)
        self.assertIn("ci.dataset.custom='1'", branch)
        self.assertIn("wrap._custom=ci", branch)
        self.assertIn("l=itemList(extra,{})", branch)
        self.assertIn("wrap._custom=box", branch)
        # collect() reads it for every shape the select branch can draw: several values one box each, a single
        # value as the whole box (a comma, or a space around it, is part of the value either way)
        self.assertIn("w._custom.querySelectorAll('[data-item]')", self.collect)
        self.assertNotIn("split(',')", self.collect)
        for kind, reader in (("radio", r"typedOne\(\)"), ("checklist", r"withTyped\("), ("select", r"typedOne\(\)"), ("multiselect", r"withTyped\(")):
            with self.subTest(kind=kind):
                line = next(ln for ln in self.collect.splitlines() if f"k==='{kind}'" in ln)
                self.assertRegex(line, reader)

    def test_a_multiple_text_selector_is_one_input_per_item(self):
        branch = self.field[self.field.index("sel.text.multiple"):self.field.index("kind==='text' || f.type==='string'")]
        items = CONFIG_JS[CONFIG_JS.index("function itemList("):CONFIG_JS.index("function field(")]
        self.assertIn("wrap.dataset.kind='textlist'", branch)
        self.assertIn("itemList(", branch)
        self.assertIn("i.dataset.item='1'", items)
        self.assertIn("del.onclick=()=>row.remove()", items)
        self.assertRegex(self.collect, r"k==='textlist'.*querySelectorAll\('\[data-item\]'\)")

    def test_a_constant_has_no_input_and_is_still_sent(self):
        self.assertIn("wrap._const=c.value", self.field)
        self.assertRegex(self.collect, r"k==='constant'.*v=w\._const")

    def test_color_is_a_colour_picker_sent_back_as_three_bytes(self):
        self.assertIn("el.type='color'", self.field)
        self.assertRegex(self.collect, r"k==='color'.*parseInt\(m\[g\],16\)")

    def test_slider_mode_draws_a_range_with_its_unit_and_a_read_out(self):
        self.assertIn("n.mode==='slider'", self.field)
        self.assertIn("el.type=slider?'range':'number'", self.field)
        self.assertIn("n.unit_of_measurement", self.field)
        self.assertIn("el.oninput=show", self.field)

    def test_the_json_fallback_and_its_label_are_still_there(self):
        self.assertIn('(selector "${kind}" unsupported, JSON)', self.field)

    def test_labels_menu_entries_errors_and_aborts_prefer_the_translation(self):
        self.assertIn("(t.data||{})[name]||name", self.field)
        self.assertIn("(t.data_description||{})[name]", self.field)
        self.assertIn("t.menu_options||{}", self.render)
        self.assertIn("trOf(r,'error',v)||v", self.render)
        self.assertIn("trOf(r,'abort',r.reason)", self.render)
        self.assertIn("sections", self.field)  # a section's own name and its fields' labels


if __name__ == "__main__":
    unittest.main()
