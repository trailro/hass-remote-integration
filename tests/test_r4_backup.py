"""Review round 4, backup part: member names, import store ownership, rebuild vs upload, change report, temp files."""

import asyncio
import json
import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.test_review_backup import _volume


class MemberNameTest(unittest.TestCase):
    """HRI-05"""

    def _backup(self, cfg, extra):
        bdir = os.path.join(cfg, backupkit.BACKUP_DIR)
        os.makedirs(bdir, exist_ok=True)
        path = os.path.join(bdir, "b.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(backupkit.MARKER, "{}")
            zf.writestr(".storage/x", "{}")
            zf.writestr(extra, "EVIL")
        return path

    def test_non_normalized_names_are_refused(self):
        cfg = _volume()
        for name in ("integration_manager/./auth_key", "integration_manager//auth_revoked", "./integration_manager/events.jsonl",
                     "integration_manager/.", ".storage/a/../core.uuid"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                backupkit.validate(self._backup(cfg, name))

    def test_live_login_key_survives_a_crafted_restore(self):
        cfg = _volume()
        key = os.path.join(cfg, backupkit.STATE_DIR, "auth_key")
        with open(key, "w", encoding="utf-8") as fh:
            fh.write("LIVE")
        self._backup(cfg, "integration_manager/./auth_key")
        with self.assertRaises(ValueError):
            backupkit.schedule_restore(cfg, "b.zip", force=True)
        self.assertIsNone(backupkit.apply_pending(cfg, log=lambda *_: None))
        with open(key, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "LIVE")

    def test_own_backups_still_validate(self):
        cfg = _volume()
        rec = backupkit.create(cfg, "ok")
        backupkit.validate(os.path.join(cfg, backupkit.BACKUP_DIR, rec["name"]))


class StoreTempFilesTest(unittest.TestCase):
    """U6"""

    def test_ha_temp_files_and_import_leftovers_are_not_backed_up(self):
        cfg = _volume()
        for f in ("tmpab_c1234", "core.entity_registry.pre-import", "tmp_things.entry"):
            with open(os.path.join(cfg, ".storage", f), "w", encoding="utf-8") as fh:
                fh.write("{}")
        rec = backupkit.create(cfg, "u6")
        with zipfile.ZipFile(os.path.join(cfg, backupkit.BACKUP_DIR, rec["name"])) as zf:
            names = zf.namelist()
        self.assertNotIn(".storage/tmpab_c1234", names)
        self.assertNotIn(".storage/core.entity_registry.pre-import", names)
        self.assertIn(".storage/tmp_things.entry", names)  # a store that only starts with "tmp"


class StoreOwnershipTest(unittest.TestCase):
    """HRI-07"""

    def _out(self):
        out = tempfile.mkdtemp()
        os.makedirs(os.path.join(out, ".storage"))
        entries = [{"domain": "foo", "entry_id": "a1"}, {"domain": "foo_bar", "entry_id": "b1"}]
        with open(os.path.join(out, ".storage", "core.config_entries"), "w", encoding="utf-8") as fh:
            json.dump({"data": {"entries": entries}}, fh)
        for f in ("foo_bar_tokens", "foo_shared", "foo.a1"):
            with open(os.path.join(out, ".storage", f), "w", encoding="utf-8") as fh:
                fh.write("{}")
        return out

    def test_another_domains_store_is_not_selected_nor_kept(self):
        from custom_components.integration_manager import ha_import

        out = self._out()
        s = ha_import._summarize(out, {}, {"foo"})
        self.assertEqual(s["domains"]["foo"]["storage_files"], ["foo.a1", "foo_shared"])
        self.assertEqual(s["domains"]["foo_bar"]["storage_files"], [])
        self.assertFalse(os.path.exists(os.path.join(out, ".storage", "foo_bar_tokens")))
        self.assertNotIn("foo_bar_tokens", s["storage_files"])

    def test_both_installed_each_gets_its_own(self):
        from custom_components.integration_manager import ha_import

        s = ha_import._summarize(self._out(), {}, {"foo", "foo_bar"})
        self.assertEqual(s["domains"]["foo"]["storage_files"], ["foo.a1", "foo_shared"])
        self.assertEqual(s["domains"]["foo_bar"]["storage_files"], ["foo_bar_tokens"])


def _hass(cfg):
    async def executor(fn, *args):
        return fn(*args)

    return SimpleNamespace(config=SimpleNamespace(config_dir=cfg, path=lambda *p: os.path.join(cfg, *p)), async_add_executor_job=executor)


class RebuildVersusUploadTest(unittest.TestCase):
    """HRI-08"""

    def test_upload_finishing_keeps_a_rebuild_staged_meanwhile(self):
        from custom_components.integration_manager import ha_import, import_views

        cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(cfg, backupkit.STATE_DIR))
        chunks = [b"x" * 10, b""]

        async def read_chunk(_n):
            if len(chunks) == 2:  # stage_rebuild runs while the upload streams
                os.makedirs(os.path.join(cfg, ha_import.EXTRACT_DIR, ".storage"))
                with open(os.path.join(cfg, ha_import.REBUILD_FILE), "w", encoding="utf-8") as fh:
                    fh.write("{}")
            return chunks.pop(0)

        field = SimpleNamespace(name="file", filename="b.tar", read_chunk=read_chunk)

        async def multipart():
            async def nxt():
                return field
            return SimpleNamespace(next=nxt)

        view = import_views.ImportUploadView(_hass(cfg))
        view.json = lambda d: d
        r = asyncio.run(view.post(SimpleNamespace(headers={"X-Requested-With": "fetch"}, multipart=multipart)))
        self.assertFalse(r["ok"], r)
        self.assertTrue(os.path.isdir(os.path.join(cfg, ha_import.EXTRACT_DIR)))
        self.assertFalse(os.path.exists(os.path.join(cfg, ha_import.IMPORT_TAR)))
        self.assertFalse(os.path.exists(os.path.join(cfg, ha_import.IMPORT_TAR + ".tmp")))

    def _change(self, backup):
        from custom_components.integration_manager import views

        cfg = tempfile.mkdtemp()
        updater = SimpleNamespace(cancel_config_change=mock.Mock())
        inst = SimpleNamespace(hass=_hass(cfg), busy=False, running="demo", async_backup=backup)
        return inst, updater, views.async_change_ha_version(inst, updater, "2020.1.0", "rebuild", "test")

    def test_rebuild_is_refused_while_an_upload_holds_the_lock(self):
        from custom_components.integration_manager import import_views

        backup = mock.AsyncMock(return_value={"name": "b.zip"})
        inst, updater, coro = self._change(backup)

        async def run():
            async with import_views._IMPORT_LOCK:
                with self.assertRaises(ValueError) as ctx:
                    await coro
            return ctx.exception

        self.assertIn("upload", str(asyncio.run(run())))
        backup.assert_not_awaited()
        self.assertFalse(inst.busy)

    def test_upload_started_during_the_backup_refuses_the_rebuild_before_anything_is_dropped(self):
        from custom_components.integration_manager import import_views

        async def backup(**_kw):
            await import_views._IMPORT_LOCK.acquire()  # an upload starts while the backup is written
            return {"name": "b.zip"}

        inst, updater, coro = self._change(backup)

        async def run():
            try:
                with self.assertRaises(ValueError):
                    await coro
            finally:
                import_views._IMPORT_LOCK.release()

        asyncio.run(run())
        updater.cancel_config_change.assert_not_called()


class GainedUniqueIdTest(unittest.TestCase):
    """HRI-14"""

    def test_entity_that_gains_a_unique_id_is_not_removed_and_added(self):
        from custom_components.integration_manager import change_report as cr

        r = cr.build({"before": {"entities": {"eid:sensor.x": {"entity_id": "sensor.x", "name": "X", "unit_of_measurement": "W"}}, "services": {}}},
                     {"entities": {"uid:sensor:u1": {"entity_id": "sensor.x", "name": "X", "unit_of_measurement": "kW"}}, "services": {}})
        self.assertEqual((r["entities_added"], r["entities_removed"]), ([], []))
        self.assertEqual(r["entities_changed"], [{"entity_id": "sensor.x", "changes": {"unit_of_measurement": ["W", "kW"]}}])

    def test_a_different_entity_is_still_removed_and_added(self):
        from custom_components.integration_manager import change_report as cr

        r = cr.build({"before": {"entities": {"eid:sensor.x": {"entity_id": "sensor.x"}}, "services": {}}},
                     {"entities": {"uid:sensor:u1": {"entity_id": "sensor.y"}}, "services": {}})
        self.assertTrue(r["breaking"])
        self.assertEqual(len(r["entities_removed"]), 1)


if __name__ == "__main__":
    unittest.main()
