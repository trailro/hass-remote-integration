"""Third external review: store files of already imported entries, the release check with branches in the store."""

import asyncio
import unittest
from types import SimpleNamespace

from jsonio import is_stable_tag
from custom_components.integration_manager.ha_import import storage_for_entry
from custom_components.integration_manager.installer import Installer


class StorageForEntryTest(unittest.TestCase):
    FILES = ["hub.e1", "hub.e2", "hub_shared_cache"]

    def test_first_entry_gets_its_own_and_domain_wide_files(self):
        self.assertEqual(storage_for_entry(self.FILES, "e1", ["e2"], first_of_domain=True), ["hub.e1", "hub_shared_cache"])

    def test_second_entry_never_rewrites_the_first_ones_or_shared_files(self):
        self.assertEqual(storage_for_entry(self.FILES, "e2", ["e1"], first_of_domain=False), ["hub.e2"])


class StableTagTest(unittest.TestCase):
    def test_classification(self):
        for tag in ("1.2", "v1.2.3", "V2026.9.1"):
            self.assertTrue(is_stable_tag(tag), tag)
        for tag in ("1.2.0b1", "1.3.0rc1", "feature/999", "a1b2c3d", "local", "", None):
            self.assertFalse(is_stable_tag(tag), tag)


class CheckUpdatesTest(unittest.TestCase):
    def _check(self, versions, release_list):
        inst = Installer.__new__(Installer)
        inst.state = SimpleNamespace(installed={"demo": {"versions": dict.fromkeys(versions, {})}}, release_updates={})
        inst.updates = {}
        inst._save_state = lambda: None

        async def fake_releases(domain, force=True):
            return release_list

        inst.releases = fake_releases
        return asyncio.run(inst.check_updates())

    def test_branch_in_the_store_does_not_hide_a_stable_release(self):
        out = self._check(["v1.2.0", "feature/999"], [{"tag": "v1.3.0"}, {"tag": "v1.2.0"}])
        self.assertEqual(out, {"demo": "v1.3.0"})

    def test_prerelease_and_beta_in_the_store_do_not_hide_it(self):
        out = self._check(["v1.2.0", "v1.3.0b1"], [{"tag": "v1.3.0"}, {"tag": "v1.3.0b1", "prerelease": True}])
        self.assertEqual(out, {"demo": "v1.3.0"})

    def test_no_update_when_the_newest_stable_is_installed(self):
        self.assertEqual(self._check(["v1.3.0", "feature/999"], [{"tag": "v1.3.0"}]), {})


if __name__ == "__main__":
    unittest.main()
