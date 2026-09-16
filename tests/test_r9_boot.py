"""Review round 9, boot and import part: the import's unpack budget, the pre-HA status page, a restore during a
pending clean start, the preflight's pip process group."""

import gzip
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock


def _tmp(test):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _header(name, size, typ=tarfile.REGTYPE):
    ti = tarfile.TarInfo(name)
    ti.size, ti.type = size, typ
    return ti.tobuf(format=tarfile.USTAR_FORMAT)


def _write_member(out, name, size, typ=tarfile.REGTYPE, fill=None):
    """A member written block by block: a test archive of tens of MB is never held in memory."""
    out.write(_header(name, size, typ))
    left = size
    while left:
        k = min(left, 1 << 20)
        out.write(fill(k) if fill else bytes(k))
        left -= k
    out.write(bytes((512 - size % 512) % 512))


def _ha_backup(cfg, members, protected=False):
    """integration_manager/import.tar as Home Assistant writes it; ``members``: (name, size, type, fill) in the
    inner homeassistant.tar.gz, after the config entries of a "demo" integration."""
    from custom_components.integration_manager import ha_import

    inner = os.path.join(cfg, "inner.tar.gz")
    entries = json.dumps({"version": 1, "data": {"entries": [{"entry_id": "abc", "domain": "demo", "title": "Demo",
                                                              "data": {}, "options": {}}]}}).encode()
    with gzip.open(inner, "wb", compresslevel=6) as g:
        _write_member(g, "data/.storage/core.config_entries", len(entries), fill=lambda k: entries[:k])
        for name, size, typ, fill in members:
            _write_member(g, name, size, typ, fill)
        _write_member(g, "data/.storage/core.entity_registry", 0)
        g.write(bytes(1024))
    path = os.path.join(cfg, ha_import.IMPORT_TAR)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tarfile.open(path, "w") as tf:
        meta = json.dumps({"name": "t", "compressed": True, "protected": protected}).encode()
        ti = tarfile.TarInfo("backup.json")
        ti.size = len(meta)
        tf.addfile(ti, io.BytesIO(meta))
        tf.add(inner, "homeassistant.tar.gz")
    os.remove(inner)


class ImportUnpackBudgetTest(unittest.TestCase):
    """F3: members the import skips were inflated without counting toward any limit."""

    def setUp(self):
        from custom_components.integration_manager import ha_import

        self.ha_import = ha_import
        self.cfg = _tmp(self)

    def test_a_skipped_member_past_the_budget_is_refused_without_being_inflated(self):
        _ha_backup(self.cfg, [("data/www/huge.bin", 64 * 1024**2, tarfile.REGTYPE, None)])  # 64 MB of zeros, ~65 KB compressed
        headers = []
        real_next = tarfile.TarFile.next

        def spy(tar):
            member = real_next(tar)
            headers.append(member and member.name)
            return member

        with mock.patch.object(self.ha_import, "MAX_EXTRACT_BYTES", 8 * 1024**2), mock.patch.object(tarfile.TarFile, "next", spy), \
                self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertIn("unpacks to more than", str(ctx.exception))
        # stopped at the huge member's header: asking for the next header is what inflates its data
        self.assertEqual(headers[-1], "data/www/huge.bin")
        self.assertNotIn("data/.storage/core.entity_registry", headers)
        self.assertFalse(os.path.exists(os.path.join(self.cfg, self.ha_import.EXTRACT_DIR)))

    def test_a_large_database_that_compresses_like_real_data_is_still_read(self):
        # the recorder database is in every backup and is not what the import extracts: 16 MB that barely compresses
        _ha_backup(self.cfg, [("data/home-assistant_v2.db", 16 * 1024**2, tarfile.REGTYPE, os.urandom)])
        with mock.patch.object(self.ha_import, "MAX_EXTRACT_BYTES", 8 * 1024**2):
            summary = self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertEqual(list(summary["domains"]), ["demo"])

    def test_an_oversized_extended_header_is_refused(self):
        # tarfile reads a pax header's data into memory whole, inside next(): 8 MB here, gigabytes in a crafted upload
        _ha_backup(self.cfg, [("PaxHeader", 8 * 1024**2, tarfile.XHDTYPE, None), ("data/x", 0, tarfile.REGTYPE, None)])
        with self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertIn("extended tar header", str(ctx.exception))
        self.assertNotIn("wrong encryption key", str(ctx.exception))

    def test_an_oversized_long_name_header_is_refused_in_an_encrypted_backup_too(self):
        _ha_backup(self.cfg, [("././@LongLink", 8 * 1024**2, tarfile.GNUTYPE_LONGNAME, None), ("data/x", 0, tarfile.REGTYPE, None)],
                   protected=True)
        # stands in for securetar's decryption: what the import gets back is a tarfile it did not open itself
        with mock.patch.object(self.ha_import.securetar, "SecureTarFile", lambda path, gzip, password: tarfile.open(path, "r|gz")), \
                self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, "key", {"demo"})
        self.assertIn("extended tar header", str(ctx.exception))

    def test_a_plain_backup_is_still_imported(self):
        _ha_backup(self.cfg, [("data/configuration.yaml", 100, tarfile.REGTYPE, None)])
        summary = self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertEqual(summary["domains"]["demo"]["entries"][0]["entry_id"], "abc")


if __name__ == "__main__":
    unittest.main()
