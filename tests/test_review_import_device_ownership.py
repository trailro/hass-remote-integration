"""Device customizations must retain config-entry ownership, including legacy maps."""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import ha_import


class DeviceOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-device-owner-")
        self.addCleanup(shutil.rmtree, self.cfg, True)
        os.makedirs(os.path.join(self.cfg, ".storage"))
        os.makedirs(os.path.join(self.cfg, ha_import.STATE_DIR))
        self.entries = [SimpleNamespace(entry_id="e1", domain="hub"), SimpleNamespace(entry_id="e2", domain="hub"),
                        SimpleNamespace(entry_id="other", domain="other")]
        self.hass = SimpleNamespace(config=SimpleNamespace(path=lambda rel: os.path.join(self.cfg, rel)),
            config_entries=SimpleNamespace(async_entries=lambda domain=None: [e for e in self.entries if domain is None or e.domain == domain]))
        self.aligner = ha_import.RegistryAligner(self.hass)
        self.aligner._save = mock.Mock()
        self.devices = []
        self.reg = SimpleNamespace(devices=self.devices, async_get=lambda did: next((d for d in self.devices if d.id == did), None),
                                   async_update_device=self.update, async_update_child_device=self.update)
        patch = mock.patch.object(ha_import.dr, "async_get", return_value=self.reg)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(ha_import.er, "async_get", return_value=mock.Mock())
        patch.start()
        self.addCleanup(patch.stop)

    def update(self, device_id, **kwargs):
        dev = self.reg.async_get(device_id)
        for key, value in kwargs.items():
            setattr(dev, key, value)

    def device(self, owner, name=None, child=False):
        # Accepted HA per-entry shape: identical identifiers have different owners.
        d = SimpleNamespace(id="device-" + owner, identifiers={("hub", "same")}, name_by_user=name, disabled_by=None,
                            config_entry_id=owner if child else None, config_entries=set() if child else {owner})
        self.devices.append(d)
        return d

    def merge(self, owner, name, target=None):
        with open(os.path.join(self.cfg, ".storage", "core.device_registry"), "w", encoding="utf-8") as fh:
            json.dump({"data": {"devices": [{"config_entry_id": owner, "identifiers": [["hub", "same"]],
                                              "name_by_user": name, "disabled_by": "user"}]}}, fh)
        self.aligner.merge_map(ha_import._build_map(self.cfg, "hub", owner, target))

    def test_suspended_entries_with_same_identifier_keep_separate_preferences(self):
        self.merge("e1", "Kitchen")
        self.merge("e2", "Bedroom")
        self.assertEqual(self.aligner.pending_counts, (0, 2))
        first, second = self.device("e1"), self.device("e2", child=True)
        self.assertTrue(self.aligner.align_device(second.id))
        self.assertEqual(second.name_by_user, "Bedroom")
        self.assertEqual(self.aligner.pending_counts, (0, 1))
        self.assertTrue(self.aligner.align_device(first.id))
        self.assertEqual(first.name_by_user, "Kitchen")
        self.assertEqual(self.aligner.pending_counts, (0, 0))

    def test_device_of_another_domain_does_not_consume_or_prune_map(self):
        self.merge("e1", "Kitchen")
        wrong = self.device("other", name="Kitchen")
        self.assertFalse(self.aligner.align_device(wrong.id))
        self.assertEqual(self.aligner.prune_satisfied(), 0)
        self.assertEqual(self.aligner.pending_counts, (0, 1))

    def test_destination_entry_id_is_used_when_source_id_was_taken(self):
        self.merge("source", "Kitchen", target="e2")
        dev = self.device("e2")
        self.assertTrue(self.aligner.align_device(dev.id))
        self.assertEqual(dev.name_by_user, "Kitchen")

    def test_restart_preserves_new_keys_and_legacy_unique_map_still_applies(self):
        self.merge("e1", "Kitchen")
        with open(self.aligner.path, "w", encoding="utf-8") as fh:
            json.dump({"domains": self.aligner.maps}, fh)
        self.aligner.maps = ha_import.RegistryAligner(self.hass).maps
        self.assertTrue(self.aligner.align_device(self.device("e1").id))
        self.devices.clear()
        self.entries = [e for e in self.entries if e.entry_id != "e2"]  # old keys have a uniquely recoverable domain owner
        with open(self.aligner.path, "w", encoding="utf-8") as fh:
            json.dump({"domain": "hub", "devices": {json.dumps(["hub", "same"]): {"name_by_user": "Legacy"}}}, fh)
        self.aligner.maps = ha_import.RegistryAligner(self.hass).maps
        dev = self.device("e1")
        self.assertTrue(self.aligner.align_device(dev.id))
        self.assertEqual(dev.name_by_user, "Legacy")

    def test_ambiguous_legacy_owner_is_retained_instead_of_guessed(self):
        key = json.dumps(["hub", "same"])
        self.aligner.maps = {"hub": {"entities": {}, "devices": {key: {"name_by_user": "Legacy"}}}}
        first, second = self.device("e1"), self.device("e2", child=True)
        for dev in (first, second):
            self.assertFalse(self.aligner.align_device(dev.id))
            self.assertIsNone(dev.name_by_user)
        self.assertEqual(self.aligner.prune_satisfied(), 0)
        self.assertEqual(self.aligner.pending_counts, (0, 1))

    def test_real_child_device_uses_child_registry_update(self):
        child_cls = getattr(ha_import.dr, "ChildDeviceEntry", None)
        if child_cls is None:
            self.skipTest("child devices require HA 2026.9+")
        self.merge("e1", "Kitchen")
        child = child_cls(config_entry_id="e1", parent_device_id="parent", identifiers={("hub", "same")})
        self.devices.append(child)
        self.reg.async_update_child_device = mock.Mock()
        self.reg.async_update_device = mock.Mock()
        self.assertTrue(self.aligner.align_device(child.id))
        self.reg.async_update_child_device.assert_called_once_with(child.id, name_by_user="Kitchen",
                                                                   disabled_by=ha_import.dr.DeviceEntryDisabler.USER)
        self.reg.async_update_device.assert_not_called()

    def test_legacy_map_waits_when_other_configured_hub_has_not_created_devices(self):
        key = json.dumps(["hub", "same"])
        self.aligner.maps = {"hub": {"entities": {}, "devices": {key: {"name_by_user": "Legacy"}}}}
        first = self.device("e1")  # e2 is configured, but its integration has not created a device yet
        self.assertFalse(self.aligner.align_device(first.id))
        self.assertIsNone(first.name_by_user)
        self.assertEqual(self.aligner.prune_satisfied(), 0)
        self.assertEqual(self.aligner.pending_counts, (0, 1))
