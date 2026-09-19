"""Review round 6: staging a clean start must not destroy the one an older change staged.

``ha_import.stage_rebuild`` emptied EXTRACT_DIR and extracted the pre-change backup straight into it.  Everything
that can fail comes after that first ``rmtree``: no space left, an archive that became unreadable between the
caller's ``backupkit.validate`` and the open.  The older change then lost its extracted source while its plan
file survived - a state that fails safe at the next boot (``reset_storage_for_rebuild`` sees the source is gone,
drops the clean start and keeps the configuration) but silently: nothing of it reaches the HTTP error.

The extraction happens in a directory of its own now and is swapped into EXTRACT_DIR only once it is complete,
with the plan file written last, so what a reader finds is always one change's source together with that
change's plan - or neither.
"""

import glob
import json
import os
import shutil
import unittest
import zipfile
from unittest import mock

import backupkit
from custom_components.integration_manager import ha_import
from tests.test_review_backup import _entrypoint, _volume

RUNNING = "2026.8.3"
OLDER = "2026.1.0"
EARLIER_TARGET = "2026.2.0"  # what the clean start of an older change was scheduled for


def _backup(cfg, name="pre-ha.zip"):
    """A backup of this volume as backupkit writes one, with the four files a rebuild extracts from it."""
    os.makedirs(os.path.join(cfg, backupkit.BACKUP_DIR), exist_ok=True)
    path = os.path.join(cfg, backupkit.BACKUP_DIR, name)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(backupkit.MARKER, "{}")
        zf.writestr(".storage/core.config_entries", json.dumps({"data": {"entries": [
            {"entry_id": "e1", "domain": "demo", "title": "Demo", "data": {"host": "h"}, "options": {}}]}}))
        zf.writestr(".storage/core.entity_registry", json.dumps({"data": {"entities": [
            {"entity_id": "sensor.demo", "platform": "demo", "config_entry_id": "e1", "unique_id": "u1"}]}}))
        zf.writestr(".storage/core.device_registry", json.dumps({"data": {"devices": []}}))
        zf.writestr(".storage/demo.e1", json.dumps({"data": {"token": "kept"}}))
        zf.writestr(".storage/auth", json.dumps({"data": "never extracted"}))
    return name


class RebuildStagingTest(unittest.TestCase):

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.backup = _backup(self.cfg)
        self.out_dir = os.path.join(self.cfg, ha_import.EXTRACT_DIR)
        self.plan_file = os.path.join(self.cfg, ha_import.REBUILD_FILE)

    # ----- what an older change left behind ---------------------------------

    def _older_clean_start(self, to=EARLIER_TARGET):
        """What a staging leaves: the extracted source, its summary, the plan the entrypoint reads.  The store
        file is named after the earlier backup so anything of it surviving into a later staging is visible."""
        os.makedirs(os.path.join(self.out_dir, ".storage"))
        with open(os.path.join(self.out_dir, ".storage", "demo.earlier"), "w", encoding="utf-8") as fh:
            fh.write("the older change's source")
        with open(os.path.join(self.cfg, ha_import.SUMMARY_FILE), "w", encoding="utf-8") as fh:
            json.dump({"type": ha_import.REBUILD_TYPE, "name": "earlier.zip", "domains": {}}, fh)
        with open(self.plan_file, "w", encoding="utf-8") as fh:
            json.dump({"stage": "reset", "from": RUNNING, "to": to, "backup": "earlier.zip", "domain": "demo"}, fh)

    def _older_is_whole(self):
        summary = ha_import.load_summary(self.cfg)
        self.assertIsNotNone(summary, "the older change's extracted source is gone: a staging that did not go through took it")
        self.assertEqual(summary["name"], "earlier.zip", "the older change's summary was replaced by a staging that failed")
        self.assertTrue(os.path.isfile(os.path.join(self.out_dir, ".storage", "demo.earlier")),
                        "the older change's extracted store file is gone")
        with open(os.path.join(self.out_dir, ".storage", "demo.earlier"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "the older change's source")
        with open(self.plan_file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["backup"], "earlier.zip")

    def _stage(self, target=OLDER):
        return ha_import.stage_rebuild(self.cfg, self.backup, "demo", RUNNING, target)

    def _leftovers(self):
        return sorted(os.path.basename(p) for p in
                      glob.glob(os.path.join(self.cfg, ha_import.STATE_DIR, ha_import._STAGE_GLOB)))

    def _torn_extraction(self, after=1):
        """shutil.copyfileobj as a full volume leaves it: the first file copied, the next one not.  The failure
        is where it really is - in the writing - not in the function under test."""
        real = shutil.copyfileobj
        calls = []

        def copyfileobj(src, dst, *a, **kw):
            calls.append(dst)
            if len(calls) > after:
                raise OSError(28, "No space left on device")
            return real(src, dst, *a, **kw)

        return mock.patch.object(ha_import.shutil, "copyfileobj", copyfileobj)

    # ----- the residual -----------------------------------------------------

    def test_an_older_clean_start_survives_a_staging_that_fails_part_way(self):
        self._older_clean_start()
        with self._torn_extraction(), self.assertRaises(OSError) as caught:
            self._stage()
        self.assertEqual(caught.exception.errno, 28)
        self._older_is_whole()
        self.assertEqual(self._leftovers(), [], "the failed staging's own directory stayed behind")

    def test_the_older_extraction_is_what_a_reader_sees_while_a_staging_runs(self):
        """The staging directory is nothing any reader looks for: load_summary, the plan file and the boot's
        EXTRACT_DIR all still answer for the older change until the swap."""
        self._older_clean_start()
        seen = {}

        real = shutil.copyfileobj

        def copyfileobj(src, dst, *a, **kw):
            seen.setdefault("summary", ha_import.load_summary(self.cfg))
            seen.setdefault("staging", self._leftovers())
            return real(src, dst, *a, **kw)

        with mock.patch.object(ha_import.shutil, "copyfileobj", copyfileobj):
            self._stage()
        self.assertEqual(seen["summary"]["name"], "earlier.zip")
        self.assertEqual(len(seen["staging"]), 1, seen["staging"])
        self.assertTrue(seen["staging"][0].startswith("staging-import-"), seen["staging"])

    def test_a_staging_that_goes_through_replaces_the_older_one_whole(self):
        self._older_clean_start()
        res = self._stage()
        self.assertEqual(res["domain"], "demo")
        self.assertEqual(res["storage_files"], ["demo.e1"])
        summary = ha_import.load_summary(self.cfg)
        self.assertEqual((summary["name"], summary["type"]), (self.backup, ha_import.REBUILD_TYPE))
        self.assertEqual(sorted(os.listdir(os.path.join(self.out_dir, ".storage"))),
                         ["core.device_registry", "core.entity_registry", "demo.e1"],
                         "a file of the older extraction survived into the new one")
        with open(self.plan_file, encoding="utf-8") as fh:
            plan = json.load(fh)
        self.assertEqual((plan["to"], plan["backup"], plan["stage"]), (OLDER, self.backup, "reset"))
        self.assertEqual(self._leftovers(), [], "the replaced extraction was not removed")

    def test_an_interrupted_stagings_directory_is_swept_and_the_live_one_is_not(self):
        self._older_clean_start()
        killed = os.path.join(self.cfg, ha_import.STATE_DIR, "staging-import-new-1-19700101-000000")
        os.makedirs(os.path.join(killed, ".storage"))
        with open(os.path.join(killed, ".storage", "demo.e1"), "w", encoding="utf-8") as fh:
            fh.write("what a killed staging left")
        # a run that fails afterwards: the sweep is all that happened, so nothing else can explain the state
        with self._torn_extraction(), self.assertRaises(OSError):
            self._stage()
        self.assertFalse(os.path.exists(killed), "the leftover of an interrupted staging was kept")
        self.assertEqual(self._leftovers(), [])
        self._older_is_whole()

    def test_a_kill_between_the_swap_and_the_plan_file_is_not_read_as_a_plan_at_the_next_boot(self):
        """The window the swap cannot close: the new extraction is in EXTRACT_DIR and the process dies before
        the plan naming it exists.  The older change's plan is taken out before the swap, so what is left is
        never one change's plan over another change's source - here it is no plan at all."""
        self._older_clean_start()
        storage_before = sorted(os.listdir(os.path.join(self.cfg, ".storage")))
        with mock.patch.object(ha_import, "write_json", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):  # a kill: not an error stage_rebuild can undo
            self._stage()
        self.assertFalse(os.path.isfile(self.plan_file), "the older change's plan was left pointing at another source")
        self.assertEqual(ha_import.load_summary(self.cfg)["name"], self.backup)

        ep = _entrypoint(self, self.cfg)
        self.assertFalse(ep.reset_storage_for_rebuild(EARLIER_TARGET, False),
                         "the boot acted on a clean start no plan file asks for")
        self.assertFalse(ep.reset_storage_for_rebuild(OLDER, False))
        self.assertEqual(sorted(os.listdir(os.path.join(self.cfg, ".storage"))), storage_before)

    def test_a_first_staging_needs_no_older_one(self):
        res = self._stage()
        self.assertEqual(res["entries"], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, ha_import.SUMMARY_FILE)))
        self.assertEqual(self._leftovers(), [])
        # what the summary kept of the config entries is not left on the volume as well
        self.assertNotIn("core.config_entries", os.listdir(os.path.join(self.out_dir, ".storage")))


if __name__ == "__main__":
    unittest.main()
