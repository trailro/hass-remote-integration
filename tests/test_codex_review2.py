"""Second external review: a partial import-all retried per entry, root YAML files cleared by a restore."""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import ha_import


class RestoreRootYamlTest(unittest.TestCase):
    def test_yaml_part_removes_root_yaml_absent_from_the_backup(self):
        cfg = tempfile.mkdtemp()
        for name in ("configuration.yaml", "new.yaml", "notes.txt"):
            with open(os.path.join(cfg, name), "w", encoding="utf-8") as fh:
                fh.write("x")
        backupkit._wipe_trees(cfg, ["configuration.yaml"], ["yaml"])
        self.assertEqual(sorted(os.listdir(cfg)), ["notes.txt"])  # restored from the backup afterwards; other files untouched

    def test_other_parts_keep_root_yaml(self):
        cfg = tempfile.mkdtemp()
        with open(os.path.join(cfg, "new.yaml"), "w", encoding="utf-8") as fh:
            fh.write("x")
        backupkit._wipe_trees(cfg, [], ["storage", "manager"])
        self.assertEqual(os.listdir(cfg), ["new.yaml"])


class _Entries:
    def __init__(self, entries):
        self.entries = entries

    def async_entries(self, domain=None):
        return [e for e in self.entries if domain is None or e.domain == domain]

    def async_get_entry(self, entry_id):
        return next((e for e in self.entries if e.entry_id == entry_id), None)


class ImportAllRetryTest(unittest.TestCase):
    def _run(self, existing, summary_entries):
        hass = SimpleNamespace(config=SimpleNamespace(config_dir="/nonexistent"), config_entries=_Entries(existing))

        async def executor(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = executor
        summary = {"domains": {"hub": {"entries": summary_entries}}}
        applied, cleared = [], []

        async def fake_apply(_hass, _aligner, domain, entry_id, *args, **kwargs):
            applied.append(entry_id)
            return {"entry_id": entry_id, "state": "loaded"}

        with mock.patch.object(ha_import, "load_summary", return_value=summary), \
                mock.patch.object(ha_import, "apply", fake_apply), \
                mock.patch.object(ha_import, "clear", lambda cfg: cleared.append(cfg)):
            res = asyncio.run(ha_import.apply_all(hass, None, None, True, True, "hub", {"hub"}))
        return res, applied, cleared

    def test_retry_imports_the_entry_that_failed_before(self):
        existing = [SimpleNamespace(entry_id="e1", domain="hub", unique_id="u1")]
        res, applied, cleared = self._run(existing, [{"entry_id": "e1", "unique_id": "u1"}, {"entry_id": "e2", "unique_id": "u2"}])
        self.assertEqual(applied, ["e2"])
        self.assertEqual([r["entry_id"] for r in res["skipped"]], ["e1"])
        self.assertTrue(res["cleaned_up"])

    def test_a_domain_with_entries_of_its_own_is_skipped_whole(self):
        existing = [SimpleNamespace(entry_id="mine", domain="hub", unique_id="other")]
        res, applied, _cleared = self._run(existing, [{"entry_id": "e1", "unique_id": "u1"}])
        self.assertEqual(applied, [])
        self.assertEqual(res["skipped"][0]["skipped"], "already has a config entry here")


if __name__ == "__main__":
    unittest.main()
