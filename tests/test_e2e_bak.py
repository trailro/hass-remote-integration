"""End-to-end campaign (0.17.0), backups: special files and symbolic links in a backup, the protection of the last
restore's backups, uploads on a full volume, a damaged settings.json, backup_keep after a manual backup, a log
cursor out of range, malformed backup.json in an import, a long upload name uploaded twice, and the answer to
cancelling a full rollback's restore."""

import asyncio
import errno
import io
import json
import logging
import os
import shutil
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import make_mocked_request

import backupkit
from custom_components.integration_manager import backup_views, ha_import, import_views, logs_page, settings as settings_mod
from custom_components.integration_manager.installer import Installer
from tests.test_review_backup import _hass, _volume, _zip


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class BackupSpecialFilesTest(unittest.TestCase):
    """2: a named pipe in a backed-up tree hung the backup forever; links out of the volume were read."""

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.outside, True)
        _write(os.path.join(self.outside, "secret.txt"), "OUTSIDE-CONTENT")
        self.fifos = []
        self.lines = []

    def tearDown(self):
        for fifo in self.fifos:  # a reader blocked in open() on the old code: let it go
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def fifo(self, rel):
        path = os.path.join(self.cfg, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.mkfifo(path)
        self.fifos.append(path)
        return path

    def create(self):
        out = {}
        thread = threading.Thread(target=lambda: out.update(rec=backupkit.create(self.cfg, "t", log=self.lines.append)), daemon=True)
        thread.start()
        thread.join(15)
        self.assertFalse(thread.is_alive(), "the backup blocked on a special file")
        return _members_of(self.cfg, out["rec"]["name"])

    def test_a_named_pipe_is_skipped_and_the_backup_finishes(self):
        self.fifo("custom_components/x/pipe")
        self.fifo("pipe.yaml")
        members = self.create()
        self.assertIn(".storage/s0", members)
        self.assertNotIn("custom_components/x/pipe", members)
        self.assertNotIn("pipe.yaml", members)
        self.assertTrue(any("custom_components/x/pipe" in line and "not a regular file" in line for line in self.lines), self.lines)

    def test_a_file_replaced_by_a_pipe_after_the_listing_is_skipped(self):
        path = self.fifo("custom_components/x/late")
        with zipfile.ZipFile(io.BytesIO(), "w") as zf:
            done = []
            thread = threading.Thread(target=lambda: done.append(backupkit._write_member(zf, path, "custom_components/x/late", self.lines.append)), daemon=True)
            thread.start()
            thread.join(15)
        self.assertEqual(done, [False])

    def test_links_out_of_the_volume_are_not_read(self):
        os.symlink(os.path.join(self.outside, "secret.txt"), os.path.join(self.cfg, "outside.yaml"))
        os.symlink(os.path.join(self.outside, "secret.txt"), os.path.join(self.cfg, "custom_components/x/outside.py"))
        os.symlink("/dev/zero", os.path.join(self.cfg, "custom_components/x/zero"))
        os.symlink(self.outside, os.path.join(self.cfg, "custom_components/linked"))
        members = self.create()
        for rel in ("outside.yaml", "custom_components/x/outside.py", "custom_components/x/zero", "custom_components/linked/secret.txt"):
            self.assertNotIn(rel, members)
        self.assertFalse(any(b"OUTSIDE-CONTENT" in data for data in members.values()))
        text = "\n".join(self.lines)
        for rel in ("outside.yaml", "custom_components/x/outside.py", "custom_components/x/zero", "custom_components/linked"):
            self.assertIn(rel, text)

    def test_a_link_to_a_file_the_backup_holds_anyway_is_stored_as_that_file(self):
        _write(os.path.join(self.cfg, "configuration.yaml"), "homeassistant:\n")
        os.symlink(os.path.join(self.cfg, "configuration.yaml"), os.path.join(self.cfg, "linked.yaml"))
        os.symlink("../../.storage/s1", os.path.join(self.cfg, "custom_components/x/rel_link"))
        members = self.create()
        self.assertEqual(members["linked.yaml"], b"homeassistant:\n")
        self.assertEqual(members["custom_components/x/rel_link"], b"orig1")

    def test_a_link_to_what_a_backup_never_holds_is_skipped(self):
        _write(os.path.join(self.cfg, backupkit.STATE_DIR, "auth_key"), "LOGIN-KEY")
        os.symlink(os.path.join(self.cfg, backupkit.STATE_DIR, "auth_key"), os.path.join(self.cfg, "custom_components/x/key"))
        members = self.create()
        self.assertNotIn("custom_components/x/key", members)
        self.assertFalse(any(b"LOGIN-KEY" in data for data in members.values()))

    def test_a_linked_tree_is_not_followed(self):
        shutil.rmtree(os.path.join(self.cfg, "custom_components"))
        os.makedirs(os.path.join(self.outside, "cc", "x"))
        _write(os.path.join(self.outside, "cc", "x", "__init__.py"), "OUTSIDE-CONTENT")
        os.symlink(os.path.join(self.outside, "cc"), os.path.join(self.cfg, "custom_components"))
        members = self.create()
        self.assertFalse(any(n.startswith("custom_components/") for n in members))
        self.assertTrue(any("custom_components" in line and "symbolic link" in line for line in self.lines), self.lines)

    def test_regular_members_keep_their_mode_and_time(self):
        path = os.path.join(self.cfg, ".storage", "s2")
        os.chmod(path, 0o640)
        os.utime(path, (1_700_000_000, 1_700_000_000))
        rec = backupkit.create(self.cfg, "t", log=self.lines.append)
        with zipfile.ZipFile(os.path.join(self.cfg, backupkit.BACKUP_DIR, rec["name"])) as zf:
            info = zf.getinfo(".storage/s2")
            self.assertEqual(info.external_attr >> 16 & 0o777, 0o640)
            self.assertEqual(info.date_time, time.localtime(1_700_000_000)[:6])
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
        backupkit.validate(os.path.join(self.cfg, backupkit.BACKUP_DIR, rec["name"]))


def _members_of(cfg, name):
    with zipfile.ZipFile(os.path.join(cfg, backupkit.BACKUP_DIR, name)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds))


class LastRestoreProtectionTest(unittest.TestCase):
    """3, 4: the backup a restore came from stayed protected for good, also when the restore was dropped or failed."""

    def installer(self, last_restore):
        inst = object.__new__(Installer)
        inst.state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, inst.state_dir, True)
        inst.state = SimpleNamespace(installed={}, rollback_backup=None)
        with open(os.path.join(inst.state_dir, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"last_restore": last_restore}, fh)
        return inst

    def test_a_restore_dropped_at_the_boot_protects_nothing(self):
        dropped = {"at": _ago(60), "ok": False, "backup": "src.zip", "parts": ["storage"], "for_version": None,
                   "error": "the scheduled restore of src.zip was dropped: its copy of the archive is gone from the volume; nothing was restored"}
        self.assertEqual(set(), self.installer(dropped).protected_backups())

    def test_a_failed_restore_protects_only_its_pre_restore_copy_for_7_days(self):
        failed = {"at": _ago(60), "ok": False, "backup": "src.zip", "pre_restore": "pre.zip", "rolled_back_to": "pre.zip"}
        self.assertEqual({"pre.zip"}, self.installer(failed).protected_backups())
        self.assertEqual(set(), self.installer({**failed, "at": _ago(8 * 86400)}).protected_backups())

    def test_an_applied_restore_protects_its_pre_restore_copy_for_7_days_and_never_its_source(self):
        applied = {"at": _ago(60), "ok": True, "backup": "src.zip", "pre_restore": "pre.zip"}
        self.assertEqual({"pre.zip"}, self.installer(applied).protected_backups())
        self.assertEqual(set(), self.installer({**applied, "at": _ago(8 * 86400)}).protected_backups())

    def test_the_source_of_the_last_restore_can_be_deleted(self):
        cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, True)
        _zip(cfg, "src.zip", {"ha_version": "2026.8.3"})
        _zip(cfg, "pre.zip", {"ha_version": "2026.8.3"})
        inst = self.installer({"at": _ago(60), "ok": True, "backup": "src.zip", "pre_restore": "pre.zip"})
        view = backup_views.BackupActionView(_hass(cfg), inst)
        view.json = lambda d: d
        post = backup_views.BackupActionView.post.__wrapped__
        self.assertEqual(asyncio.run(post(view, None, {}, "src.zip", "delete")), {"ok": True})
        refused = asyncio.run(post(view, None, {}, "pre.zip", "delete"))
        self.assertFalse(refused["ok"])
        self.assertIn("still needed", refused["error"])
        self.assertIn("copy taken before a restore in the last 7 days", refused["error"])


class _FullFile:
    """A file on a volume with no space left: every write fails, and so does the close that flushes the buffer."""

    def __init__(self, real):
        self.real = real

    def write(self, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self):
        raise OSError(errno.ENOSPC, "No space left on device")

    def fileno(self):
        return self.real.fileno()

    @property
    def closed(self):
        return self.real.closed

    def close(self):
        if not self.real.closed:
            self.real.close()
            raise OSError(errno.ENOSPC, "No space left on device")


def _upload_request(data, filename):
    chunks = [data, b""]

    async def read_chunk(_n):
        return chunks.pop(0) if chunks else b""

    field = SimpleNamespace(name="file", filename=filename, read_chunk=read_chunk)

    async def multipart():
        async def nxt():
            return field
        return SimpleNamespace(next=nxt)

    return SimpleNamespace(headers={"X-Requested-With": "fetch"}, multipart=multipart)


def _backup_bytes():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr(backupkit.MARKER, "{}")
        zf.writestr(".storage/core.config_entries", "{}")
    return payload.getvalue()


class UploadOnAFullVolumeTest(unittest.TestCase):
    """5: HTTP 500 and a .upload-*.zip.tmp left behind."""

    def test_a_backup_upload_answers_the_reason_and_leaves_nothing(self):
        cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, True)
        view = backup_views.BackupUploadView(_hass(cfg))
        view.json = lambda d: d
        real_fdopen = os.fdopen
        with mock.patch.object(backup_views.os, "fdopen", side_effect=lambda fd, mode: _FullFile(real_fdopen(fd, mode))):
            res = asyncio.run(view.post(_upload_request(_backup_bytes(), "b.zip")))
        self.assertFalse(res["ok"])
        self.assertIn("No space left", res["error"])
        self.assertEqual(os.listdir(os.path.join(cfg, backupkit.BACKUP_DIR)), [])

    def test_a_home_assistant_backup_upload_answers_the_reason_and_leaves_nothing(self):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        hass = _hass(cfg)
        hass.config.path = lambda *p: os.path.join(cfg, *p)
        view = import_views.ImportUploadView(hass)
        view.json = lambda d: d
        with mock.patch.object(import_views, "open", create=True, side_effect=lambda path, mode: _FullFile(open(path, mode))):
            res = asyncio.run(view.post(_upload_request(b"x" * 1000, "backup.tar")))
        self.assertFalse(res["ok"])
        self.assertIn("No space left", res["error"])
        self.assertEqual(os.listdir(os.path.dirname(os.path.join(cfg, ha_import.IMPORT_TAR))), [])


class LongUploadNameTest(unittest.TestCase):
    """11: a name at the length limit was taken once; the second upload read the whole file, then refused the name."""

    def test_the_same_long_name_uploaded_twice_gets_a_suffix_that_fits(self):
        cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, True)
        filename = "a" * (backup_views._NAME_MAX - len("upload-.zip")) + ".zip"
        view = backup_views.BackupUploadView(_hass(cfg))
        view.json = lambda d: d
        names = []
        for _ in range(3):
            res = asyncio.run(view.post(_upload_request(_backup_bytes(), filename)))
            self.assertTrue(res["ok"], res)
            names.append(res["name"])
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(len(n) <= backup_views._NAME_MAX and backup_views._name_ok(n) for n in names), names)
        self.assertEqual(names[1][-len("-2.zip"):], "-2.zip")
        self.assertEqual(sorted(os.listdir(os.path.join(cfg, backupkit.BACKUP_DIR))), sorted(names))


class KeepAfterManualBackupTest(unittest.TestCase):
    """7: backup_keep N kept N + 1 after a manual backup, and protected backups did not count."""

    def test_the_new_backup_counts_toward_keep_and_is_kept(self):
        cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, True)
        for i, name in enumerate(("a.zip", "b.zip", "c.zip")):
            path = _zip(cfg, name, {"created": time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 3600 * (3 - i)))})
            os.utime(path, (time.time() - 3600 * (3 - i),) * 2)

        async def backup(label):
            return await asyncio.get_running_loop().run_in_executor(None, backupkit.create, cfg, label)

        installer = SimpleNamespace(settings=SimpleNamespace(backup_keep=2), protected_backups=set, async_backup_exclusive=backup)
        view = backup_views.BackupCreateView(_hass(cfg), installer)
        view.json = lambda d: d
        res = asyncio.run(backup_views.BackupCreateView.post.__wrapped__(view, None, {"label": "manual"}))
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["pruned"]), ["a.zip", "b.zip"])
        self.assertEqual(sorted(os.listdir(os.path.join(cfg, backupkit.BACKUP_DIR))), sorted(["c.zip", res["backup"]["name"]]))

    def test_a_protected_backup_counts_but_is_never_removed(self):
        cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, True)
        for i, name in enumerate(("a.zip", "b.zip", "c.zip", "d.zip")):
            path = _zip(cfg, name, {"created": time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 3600 * (4 - i)))})
            os.utime(path, (time.time() - 3600 * (4 - i),) * 2)
        # newest first: d (protected), c, b, a
        self.assertEqual(sorted(backupkit.prune(cfg, 2, {"d.zip", "a.zip"})), ["b.zip"])


if __name__ == "__main__":
    unittest.main()
