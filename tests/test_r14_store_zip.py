"""Review round 14, store: the member cap of a zip is checked before zipfile reads the central directory."""

import io
import os
import shutil
import struct
import time
import unittest
import zipfile
from unittest import mock

import backupkit
from tests.test_review_backup import _volume, _zip

A = "2026.8.3"


def _forge_count(path, count):
    """Rewrite the member counts of the end record (the last 22 bytes of an archive without a comment)."""
    with open(path, "r+b") as fh:
        fh.seek(-22, 2)
        rec = bytearray(fh.read(22))
        assert rec[:4] == b"PK\x05\x06"
        struct.pack_into("<2H", rec, 8, count, count)
        fh.seek(-22, 2)
        fh.write(rec)


def _add(path, n):
    with zipfile.ZipFile(path, "a") as zf:
        for i in range(n):
            zf.writestr(f"custom_components/x/{i}", b"")


class _NoZipFile:
    """Fails the test when zipfile.ZipFile is constructed (it reads every member header)."""

    def __init__(self, test):
        self.test = test
        self.real = zipfile.ZipFile.__init__
        self.opened = []

    def __enter__(self):
        opened, real = self.opened, self.real

        def init(zf, file, *args, **kwargs):
            opened.append(file)
            return real(zf, file, *args, **kwargs)

        self.patch = mock.patch.object(zipfile.ZipFile, "__init__", init)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        return False


class MemberCapBeforeZipFileTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        backupkit._DESCRIBED.clear()

    def refused(self, path, cap):
        with mock.patch.object(backupkit, "MAX_MEMBERS", cap), _NoZipFile(self) as nz, self.assertRaises(ValueError) as ctx:
            backupkit.validate(path)
        self.assertIn(f"more than {cap} files", str(ctx.exception))
        self.assertEqual(nz.opened, [])

    def test_a_declared_count_over_the_cap_is_refused_without_zipfile(self):
        path = _zip(self.cfg, "forged.zip", {"ha_version": A})
        _forge_count(path, 60000)
        self.refused(path, 50000)

    def test_a_forged_low_count_does_not_hide_the_real_members(self):
        path = _zip(self.cfg, "many.zip", {"ha_version": A})
        _add(path, 60)
        _forge_count(path, 3)
        with zipfile.ZipFile(path) as zf:
            self.assertEqual(len(zf.infolist()), 63)  # zipfile reads every header whatever the record declares
        self.refused(path, 50)

    def test_an_archive_with_a_comment(self):
        path = _zip(self.cfg, "comment.zip", {"ha_version": A})
        _add(path, 60)
        with zipfile.ZipFile(path, "a") as zf:
            zf.comment = b"PK" + b"x" * 5000
        self.refused(path, 50)
        with mock.patch.object(backupkit, "MAX_MEMBERS", 62):
            self.assertEqual(backupkit.validate(path)["files"], 63)

    def test_zip64_end_records(self):
        path = os.path.join(self.cfg, backupkit.BACKUP_DIR, "z64.zip")
        with mock.patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 2):  # zipfile writes the zip64 end records past this
            _zip(self.cfg, "z64.zip", {"ha_version": A})
            _add(path, 5)
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertIn(b"PK\x06\x06", data)
        self.assertIn(b"PK\x06\x07", data)
        _forge_count(path, 1)  # zipfile takes the count and the directory from the zip64 record
        with open(path, "rb") as fh:
            self.assertEqual(backupkit._central_directory(fh)[0], 8)
            self.assertFalse(backupkit.zip_has_more_members(fh, 8))
            self.assertTrue(backupkit.zip_has_more_members(fh, 7))
        self.refused(path, 6)
        with mock.patch.object(backupkit, "MAX_MEMBERS", 7):
            self.assertEqual(backupkit.validate(path)["files"], 8)

    def test_junk_is_still_refused_by_zipfile(self):
        path = os.path.join(self.cfg, backupkit.BACKUP_DIR, "junk.zip")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 10**9, 1)
        for blob in (b"", b"PK\x05\x06", b"x" * 100 + b"PK\x05\x06" + b"\0" * 18 + b"PK\x06\x07",
                     b"x" * 100 + locator + b"PK\x05\x06" + b"\0" * 18,
                     b"PK\x05\x06" + struct.pack("<4H2LH", 0, 0, 1, 1, 10**6, 0, 0)):
            with open(path, "wb") as fh:
                fh.write(blob)
            with open(path, "rb") as fh:
                self.assertFalse(backupkit.zip_has_more_members(fh, 0))
            with self.assertRaises(ValueError):
                backupkit.validate(path)

    def test_describe_does_not_open_an_archive_over_the_cap(self):
        path = _zip(self.cfg, "listed.zip", {"ha_version": A, "label": "big"})
        _forge_count(path, 60000)
        with mock.patch.object(backupkit, "MAX_MEMBERS", 50000), _NoZipFile(self) as nz:
            record = backupkit.describe(self.cfg, "listed.zip")
        self.assertEqual(nz.opened, [])
        self.assertEqual((record["ha_version"], record["label"]), (None, ""))

    def test_a_large_real_directory_is_refused_quickly(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(20000):
                zf.writestr(f"m/{i}", b"")
        started = time.monotonic()
        self.assertTrue(backupkit.zip_has_more_members(buf, 1000))
        self.assertFalse(backupkit.zip_has_more_members(buf, 20000))
        self.assertLess(time.monotonic() - started, 1.0)


class DescribeMemoInodeTest(unittest.TestCase):
    """A same-size backup moved over another within one mtime tick was listed with the old record."""

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        backupkit._DESCRIBED.clear()

    def test_a_same_size_rewrite_with_the_same_mtime_is_read_again(self):
        path = _zip(self.cfg, "a.zip", {"ha_version": A, "label": "aaaa"})
        st = os.stat(path)
        self.assertEqual(backupkit.describe(self.cfg, "a.zip")["label"], "aaaa")
        other = _zip(self.cfg, "b.tmp", {"ha_version": A, "label": "bbbb"})
        with zipfile.ZipFile(path) as za, zipfile.ZipFile(other) as zb:  # the same names and sizes: the same layout
            self.assertEqual([(i.filename, i.file_size) for i in za.infolist()], [(i.filename, i.file_size) for i in zb.infolist()])
        os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(other, path)
        st2 = os.stat(path)
        self.assertEqual((st2.st_size, st2.st_mtime_ns), (st.st_size, st.st_mtime_ns))
        self.assertEqual(backupkit.describe(self.cfg, "a.zip")["label"], "bbbb")


if __name__ == "__main__":
    unittest.main()
