"""The config entries of a device, read without the accessors Home Assistant 2026.10 deprecated.

From 2026.10, reading ``DeviceEntry.config_entries`` or ``primary_config_entry`` from a custom component logs
a deprecation (a device belongs to one config entry, ``config_entry_id``, since 2026.8); up to 2026.7 there is
no ``config_entry_id`` at all.  discovery.device_config_entries answers on every shape, and nothing else in the
component reads those accessors."""

import ast
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import discovery, ha_import

COMPONENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "integration_manager")


class _Device2026_5:
    """2026.5 - 2026.7: a set of entries and a primary one, plain attributes."""

    def __init__(self, entries, primary=None):
        self.config_entries = set(entries)
        self.primary_config_entry = primary


class _Device2026_8:
    """2026.8 - 2026.9: one entry; the old accessors are shims that log nothing."""

    def __init__(self, entry, composite=None):
        self.config_entry_id = entry
        self._composite = composite

    @property
    def config_entries(self):
        return set(self._composite) if self._composite is not None else {self.config_entry_id}

    @property
    def primary_config_entry(self):
        return self.config_entry_id


class _Child2026_9(_Device2026_8):
    """A 2026.9 ChildDeviceEntry: an attribute only DeviceEntry has raises AttributeError (outside an integration frame)."""

    def __getattr__(self, name):
        raise AttributeError(name)


class _Device2026_10:
    """2026.10: the shims report a deprecation unless the device is a restored composite."""

    def __init__(self, entry, composite=None):
        self.config_entry_id = entry
        self._composite = composite
        self.reported = []

    @property
    def is_composite_device(self):
        return self._composite is not None

    @property
    def config_entries(self):
        if not self.is_composite_device:
            self.reported.append("config_entries")
        return set(self._composite) if self._composite is not None else {self.config_entry_id}

    @property
    def primary_config_entry(self):
        if not self.is_composite_device:
            self.reported.append("primary_config_entry")
        return self.config_entry_id


class ShapesTest(unittest.TestCase):
    def test_up_to_2026_7_the_set(self):
        self.assertEqual(sorted(discovery.device_config_entries(_Device2026_5({"e1", "e2"}, "e1"))), ["e1", "e2"])
        self.assertEqual(discovery.device_config_entries(_Device2026_5(set())), [])

    def test_2026_8_and_2026_9(self):
        self.assertEqual(discovery.device_config_entries(_Device2026_8("e1")), ["e1"])
        self.assertEqual(sorted(discovery.device_config_entries(_Device2026_8("e1", composite={"e1", "e2"}))), ["e1", "e2"])
        self.assertEqual(discovery.device_config_entries(_Child2026_9("e3")), ["e3"])

    def test_2026_10_reads_config_entry_id_and_reports_nothing(self):
        dev = _Device2026_10("e1")
        self.assertEqual(discovery.device_config_entries(dev), ["e1"])
        self.assertEqual(dev.reported, [])

    def test_2026_10_composite_keeps_every_entry_without_a_report(self):
        dev = _Device2026_10("e1", composite={"e1", "e2"})
        self.assertEqual(sorted(discovery.device_config_entries(dev)), ["e1", "e2"])
        self.assertEqual(dev.reported, [])

    def test_import_ownership_on_2026_10_reports_nothing(self):
        dev = _Device2026_10("e1")
        self.assertEqual(ha_import._device_owners(dev), {"e1"})
        self.assertEqual(dev.reported, [])

    def test_the_order_of_an_old_list_is_kept(self):
        # the device page's delete asks the entries in this order
        self.assertEqual(discovery.device_config_entries(SimpleNamespace(config_entries=["b", "a"])), ["b", "a"])


class RealDeviceEntryTest(unittest.TestCase):
    """The Home Assistant this runs on: its own DeviceEntry, and no deprecation reported for reading it."""

    def test_the_running_home_assistant(self):
        dr = discovery.dr
        fields = getattr(dr.DeviceEntry, "__attrs_attrs__", ())
        names = {f.name for f in fields}
        if "config_entry_id" in names:
            dev = dr.DeviceEntry(config_entry_id="e1")
        else:
            dev = dr.DeviceEntry(config_entries={"e1"}, primary_config_entry="e1")
        report = getattr(dr, "report_usage", None)
        with mock.patch.object(dr, "report_usage", create=report is None) as reported:
            self.assertEqual(discovery.device_config_entries(dev), ["e1"])
            self.assertEqual(ha_import._device_owners(dev), {"e1"})
        reported.assert_not_called()


class NoDeprecatedAccessorTest(unittest.TestCase):
    """Only device_config_entries reads a device's config_entries / primary_config_entry."""

    DEPRECATED = {"config_entries", "primary_config_entry", "config_entries_subentries"}

    def test_the_component_reads_them_nowhere_else(self):
        hits = []
        for name in sorted(os.listdir(COMPONENT)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(COMPONENT, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            allowed = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "device_config_entries" and name == "discovery.py":
                    allowed |= {id(n) for n in ast.walk(node)}
            for node in ast.walk(tree):
                if id(node) in allowed:
                    continue
                if isinstance(node, ast.Attribute) and node.attr in self.DEPRECATED:
                    base = node.value
                    hass = (isinstance(base, ast.Name) and base.id == "hass") or (isinstance(base, ast.Attribute) and base.attr == "hass")
                    if node.attr == "config_entries" and hass:
                        continue  # hass.config_entries: the config entries manager, not a device
                    hits.append(f"{name}:{node.lineno} .{node.attr}")
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr")
                      and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in self.DEPRECATED):
                    hits.append(f"{name}:{node.lineno} {node.func.id}(..., {node.args[1].value!r})")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
