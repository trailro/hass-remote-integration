"""Review round 3, backup part: HA import member cap, restore retries, versions, symlinks, durability."""

import asyncio
import io
import json
import os
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
import jsonio
from tests.test_review_backup import _entrypoint, _hass, _schedule_changed_restore, _storage, _volume, _zip

quiet = lambda *_: None  # noqa: E731


def _ha_backup(path, inner_names, outer_extra=0):
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as tf:
        for name in inner_names:
            data = json.dumps({"data": {"entries": []}}).encode() if name.endswith("core.config_entries") else b""
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    with tarfile.open(path, "w") as tf:
        members = [(f"extra{i}", b"") for i in range(outer_extra)]
        members += [("backup.json", json.dumps({"compressed": True}).encode()), ("homeassistant.tar.gz", inner.getvalue())]
        for name, data in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


class HaImportMemberCapTest(unittest.TestCase):
    """9"""

    def run_inspect(self, inner_names, outer_extra=0):
        from custom_components.integration_manager import ha_import

        cfg = tempfile.mkdtemp()
        path = os.path.join(cfg, ha_import.IMPORT_TAR)
        os.makedirs(os.path.dirname(path))
        _ha_backup(path, inner_names, outer_extra)
        return ha_import.inspect_backup(cfg, None, set())

    def test_inner_member_count_is_capped(self):
        from custom_components.integration_manager import ha_import

        with mock.patch.object(ha_import, "MAX_MEMBERS", 50), self.assertRaises(ValueError) as ctx:
            self.run_inspect([f"e{i}" for i in range(60)])
        self.assertIn("more than 50", str(ctx.exception))

    def test_outer_member_count_is_capped(self):
        from custom_components.integration_manager import ha_import

        with mock.patch.object(ha_import, "MAX_MEMBERS", 50), self.assertRaises(ValueError) as ctx:
            self.run_inspect([], outer_extra=60)
        self.assertIn("more than 50", str(ctx.exception))

    def test_headers_are_not_kept_while_reading(self):
        # (measured alone: peak heap 27 MB for 60k empty members before, 1 MB after; tracemalloc in the suite sees other threads)
        names = [".storage/core.config_entries"] + [f"e{i}" for i in range(5000)]
        kept = []
        real_next = tarfile.TarFile.next

        def spy(tar):
            member = real_next(tar)
            kept.append(len(tar.members))
            return member

        with mock.patch.object(tarfile.TarFile, "next", spy):
            summary = self.run_inspect(names, outer_extra=5000)
        self.assertIn("core.config_entries", summary["storage_files"])
        self.assertLessEqual(max(kept), 1)  # every header kept: thousands


class PreRestoreRecordTest(unittest.TestCase):
    """10"""

    def test_unrecorded_pre_restore_copy_stops_before_the_wipe(self):
        cfg = _volume()
        _schedule_changed_restore(cfg)
        before = _storage(cfg)
        real = backupkit.write_json

        def write_json(path, data, **kw):
            if path.endswith("restore-pending.json") and "pre_restore" in data:
                raise OSError(28, "No space left on device")
            return real(path, data, **kw)

        with mock.patch.object(backupkit, "write_json", write_json), mock.patch.object(backupkit, "_wipe_trees") as wipe:
            result = backupkit.apply_pending(cfg, quiet, record=lambda r: True)
        wipe.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("nothing was changed", result["error"])
        self.assertEqual(_storage(cfg), before)

    def test_retry_that_fails_before_the_wipe_puts_the_first_copy_back(self):
        cfg = _volume()
        _schedule_changed_restore(cfg)
        before = _storage(cfg)
        pre = backupkit.create(cfg, "pre-restore")
        jsonio.write_json(os.path.join(cfg, backupkit.PENDING_META), {**backupkit._pending_meta(cfg), "pre_restore": pre["name"]})
        for n in list(before)[1:]:  # an earlier attempt wiped part of .storage
            os.remove(os.path.join(cfg, ".storage", n))
        with open(backupkit.pending_archive(cfg), "r+b") as fh:
            fh.truncate(100)  # and this attempt cannot read the archive
        result = backupkit.apply_pending(cfg, quiet, record=lambda r: True)
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("rolled_back_to"), pre["name"])
        self.assertEqual(_storage(cfg), before)


class NonVersionHaVersionTest(unittest.TestCase):
    """11"""

    def setUp(self):
        self.cfg = _volume()
        _zip(self.cfg, "u.zip", {"ha_version": "unknown"})

    def test_not_a_version_counts_as_unknown(self):
        with self.assertRaises(backupkit.UnknownVersion):
            backupkit.schedule_restore(self.cfg, "u.zip")
        self.assertIsNone(backupkit.describe(self.cfg, "u.zip")["ha_version"])
        for v in ("2026.8", "2026.9.0b2", "2026.8.3"):
            self.assertEqual(backupkit.known_ha_version(v), v)
        for v in ("unknown", "", "2026", "dev", None, 2026):
            self.assertIsNone(backupkit.known_ha_version(v))

    def test_boot_drops_an_unforced_schedule_claiming_a_non_version(self):
        backupkit.schedule_restore(self.cfg, "u.zip", force=True)
        meta = backupkit._pending_meta(self.cfg)
        jsonio.write_json(os.path.join(self.cfg, backupkit.PENDING_META), {**meta, "ha_version": "unknown", "force": False})
        self.assertIsNone(backupkit.pending_ha_version(self.cfg))
        state = {}
        _entrypoint(self, self.cfg).apply_config_changes(state, "2026.8.3", "2026.8.3")
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertIn("not forced", state["last_error"])

    def test_view_asks_for_force(self):
        from custom_components.integration_manager import backup_views

        view = backup_views.BackupActionView(_hass(self.cfg), SimpleNamespace(busy=False, protected_backups=set))
        view.json = lambda d: d
        r = asyncio.run(backup_views.BackupActionView.post.__wrapped__(view, None, {}, "u.zip", "restore"))
        self.assertTrue(r.get("needs_force"), r)
        self.assertFalse(backupkit.pending(self.cfg))


class SymlinkRestoreTest(unittest.TestCase):
    """12"""

    def test_link_inside_a_tree_is_replaced_not_written_through(self):
        cfg = _volume()
        outside = tempfile.mkdtemp()
        with open(os.path.join(outside, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write("outside")
        link = os.path.join(cfg, "custom_components", "devint")
        os.symlink(outside, link)
        with zipfile.ZipFile(_zip(cfg, "s.zip", {"ha_version": "2026.8.3"}), "a") as zf:
            zf.writestr("custom_components/devint/manifest.json", "from backup")
        backupkit.schedule_restore(cfg, "s.zip")
        result = backupkit.apply_pending(cfg, quiet, record=lambda r: True)
        self.assertTrue(result["ok"], result)
        with open(os.path.join(outside, "manifest.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "outside")
        self.assertFalse(os.path.islink(link))
        with open(os.path.join(link, "manifest.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "from backup")

    def test_linked_top_directory_is_refused_before_anything_changes(self):
        cfg = _volume()
        outside = tempfile.mkdtemp()
        with open(os.path.join(outside, "keep.py"), "w", encoding="utf-8") as fh:
            fh.write("outside")
        import shutil

        shutil.rmtree(os.path.join(cfg, "custom_components"))
        os.symlink(outside, os.path.join(cfg, "custom_components"))
        with zipfile.ZipFile(_zip(cfg, "s.zip", {"ha_version": "2026.8.3"}), "a") as zf:
            zf.writestr("custom_components/x/f.py", "from backup")
        before = _storage(cfg)
        backupkit.schedule_restore(cfg, "s.zip")
        result = backupkit.apply_pending(cfg, quiet, record=lambda r: True)
        self.assertFalse(result["ok"])
        self.assertIn("symbolic link", result["error"])
        self.assertEqual(os.listdir(outside), ["keep.py"])
        self.assertEqual(_storage(cfg), before)


class FailedRestoreNotRetriedTest(unittest.TestCase):
    """13"""

    def test_unrecorded_failure_is_marked_and_recorded_at_the_next_boot(self):
        cfg = _volume()
        _zip(cfg, "f.zip", {"ha_version": "2026.8.3"})
        backupkit.schedule_restore(cfg, "f.zip")
        with mock.patch.object(backupkit, "_extract_to", side_effect=OSError(28, "No space left on device")):
            result = backupkit.apply_pending(cfg, quiet, record=lambda r: False)
        self.assertFalse(result["ok"])
        self.assertFalse(backupkit.pending(cfg))
        self.assertTrue(os.path.isfile(os.path.join(cfg, backupkit.FAILED_META)))
        with mock.patch.object(backupkit, "validate") as validate:
            self.assertIsNone(backupkit.apply_pending(cfg, quiet, record=lambda r: False))
        validate.assert_not_called()
        ep = _entrypoint(self, cfg)
        state = {}
        ep.merge_applied_restore(state)
        self.assertFalse(state["last_restore"]["ok"])
        self.assertEqual(state["last_restore"]["backup"], "f.zip")
        self.assertFalse(os.path.isfile(os.path.join(cfg, backupkit.FAILED_META)))
        self.assertTrue(backupkit._excluded(backupkit.FAILED_META))


class HoldAfterFailedRollbackTest(unittest.TestCase):
    """14"""

    def setUp(self):
        self.cfg = _volume()
        self.src = _schedule_changed_restore(self.cfg)
        self.broken = True
        real_replace, real_extract = os.replace, backupkit._extract_to

        def replace(a, b):
            if self.broken and "staging-restore" in str(a):
                raise OSError(28, "No space left on device")
            return real_replace(a, b)

        def extract(zf, names, root):
            if self.broken and root == self.cfg:
                raise OSError(28, "No space left on device")
            return real_extract(zf, names, root)

        self.patches = [mock.patch.object(backupkit.os, "replace", replace), mock.patch.object(backupkit, "_extract_to", extract)]
        for p in self.patches:
            p.start()
        self.ep = _entrypoint(self, self.cfg)
        self.srv = mock.Mock()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def boot(self, during_sleep):
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            during_sleep()

        state = {}
        with mock.patch.object(self.ep.time, "sleep", sleep), mock.patch.object(self.ep, "start_status_server", return_value=self.srv):
            self.ep.apply_config_changes(state, "2026.8.3", "2026.8.3")
        return state, sleeps

    def test_home_assistant_waits_until_the_retry_applies(self):
        def fix():
            self.broken = False

        state, sleeps = self.boot(fix)
        self.assertEqual(sleeps, [self.ep.RESTORE_RETRY_S])
        self.assertTrue(state["last_restore"]["ok"], state["last_restore"])
        self.assertEqual(set(_storage(self.cfg).values()), {f"orig{i}" for i in range(10)})
        self.assertFalse(backupkit.pending(self.cfg))
        self.srv.shutdown.assert_called_once()
        self.assertIn("could not be put back", self.ep._status["title"])

    def test_removing_the_schedule_ends_the_wait(self):
        def remove():
            os.remove(os.path.join(self.cfg, backupkit.PENDING_META))

        state, sleeps = self.boot(remove)
        self.assertEqual(len(sleeps), 1)
        self.assertIn("recovery_source", state["last_restore"])
        self.assertFalse(state["last_restore"]["ok"])


class ScheduledArchiveSyncTest(unittest.TestCase):
    """15"""

    def test_scheduled_copy_and_its_directory_are_synced(self):
        cfg = _volume()
        _zip(cfg, "d.zip", {"ha_version": "2026.8.3"})
        synced, dirs = [], []
        real_fsync = os.fsync
        with mock.patch.object(backupkit.os, "fsync", side_effect=lambda fd: (synced.append(os.readlink(f"/proc/self/fd/{fd}")), real_fsync(fd))), \
                mock.patch.object(backupkit, "fsync_dir", side_effect=dirs.append):
            dst = backupkit.schedule_restore(cfg, "d.zip")
        self.assertTrue(any(p.endswith(".zip.tmp") for p in synced), synced)
        self.assertIn(os.path.dirname(dst), dirs)
        backupkit.validate(dst)

    def test_upload_is_synced(self):
        from custom_components.integration_manager import backup_views

        cfg = _volume()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as zf:
            zf.writestr(backupkit.MARKER, "{}")
            zf.writestr(".storage/core.config_entries", "{}")
        chunks = [payload.getvalue(), b""]

        async def read_chunk(_n):
            return chunks.pop(0)

        field = SimpleNamespace(name="file", filename="b.zip", read_chunk=read_chunk)

        async def multipart():
            async def nxt():
                return field
            return SimpleNamespace(next=nxt)

        view = backup_views.BackupUploadView(_hass(cfg))
        view.json = lambda d: d
        dirs = []
        with mock.patch.object(backup_views.os, "fsync", wraps=os.fsync) as fsync, mock.patch.object(backup_views, "fsync_dir", side_effect=dirs.append):
            r = asyncio.run(view.post(SimpleNamespace(headers={"X-Requested-With": "fetch"}, multipart=multipart)))
        self.assertTrue(r["ok"], r)
        fsync.assert_called()
        self.assertEqual(dirs, [os.path.join(cfg, backupkit.BACKUP_DIR)])


class RestoredFileModesTest(unittest.TestCase):
    """Unconfirmed concern: a backup made by an older image carries 0644 in its zip entries."""

    def test_restored_files_are_private_under_the_entrypoint_umask(self):
        old = os.umask(0o077)
        try:
            cfg = _volume()
            path = os.path.join(cfg, backupkit.BACKUP_DIR, "old.zip")
            os.makedirs(os.path.dirname(path))
            with zipfile.ZipFile(path, "w") as zf:
                for n, d in ((backupkit.MARKER, "{}"), (".storage/core.config_entries", "{}"), ("custom_components/newint/__init__.py", ""),
                             ("secrets.yaml", "a: 1"), ("backup-info.json", json.dumps({"ha_version": "2026.8.3"}))):
                    zi = zipfile.ZipInfo(n)
                    zi.external_attr = 0o100644 << 16
                    zf.writestr(zi, d)
            backupkit.schedule_restore(cfg, "old.zip")
            self.assertTrue(backupkit.apply_pending(cfg, quiet, record=lambda r: True)["ok"])
            for rel in (".storage/core.config_entries", "custom_components/newint/__init__.py", "secrets.yaml"):
                self.assertEqual(os.stat(os.path.join(cfg, rel)).st_mode & 0o777, 0o600, rel)
            self.assertEqual(os.stat(os.path.join(cfg, "custom_components/newint")).st_mode & 0o777, 0o700)
        finally:
            os.umask(old)


class AlignerWriteLockTest(unittest.TestCase):
    """C9"""

    def test_older_snapshot_never_lands_over_a_newer_one(self):
        from custom_components.integration_manager import ha_import

        d = tempfile.mkdtemp()
        aligner = ha_import.RegistryAligner(SimpleNamespace(config=SimpleNamespace(path=lambda *p: os.path.join(d, *p))))
        os.makedirs(os.path.dirname(aligner.path), exist_ok=True)
        real_replace = os.replace

        def slow_replace(src, dst):
            with open(src, encoding="utf-8") as fh:
                if json.load(fh) == {"seq": 1}:
                    newer = threading.Thread(target=aligner._write, args=(json.dumps({"seq": 2}), 2))
                    newer.start()
                    newer.join(0.5)  # without the lock it writes seq 2 now, and seq 1 lands over it
            real_replace(src, dst)

        with mock.patch.object(ha_import.os, "replace", slow_replace):
            aligner._write(json.dumps({"seq": 1}), 1)
            deadline = time.time() + 5
            while aligner._written_seq != 2 and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)
        with open(aligner.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"seq": 2})


if __name__ == "__main__":
    unittest.main()
