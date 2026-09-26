"""Review of b4cd1a1.  S5-3: a backup whose contents cannot be read was answered "wrong encryption key?" even when it
is not encrypted, where the cause is a damaged archive."""

import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest

from custom_components.integration_manager import ha_import
from tests.test_r9_boot import _ha_backup


def _plain_backup_with_a_broken_archive(cfg):
    path = os.path.join(cfg, ha_import.IMPORT_TAR)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tarfile.open(path, "w") as tf:
        for name, data in (("backup.json", json.dumps({"name": "t", "compressed": True, "protected": False}).encode()),
                           ("homeassistant.tar.gz", b"\x1f\x8b\x08\x00" + bytes(64))):  # a gzip header, then nothing sensible
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


class ReadFailureReasonTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)

    def test_an_unencrypted_backup_says_the_archive_is_damaged(self):
        _plain_backup_with_a_broken_archive(self.cfg)
        with self.assertRaises(ValueError) as ctx:
            ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertIn("corrupt or truncated archive", str(ctx.exception))
        self.assertNotIn("encryption key", str(ctx.exception))

    def test_an_encrypted_backup_still_suggests_the_key(self):
        _ha_backup(self.cfg, [("data/configuration.yaml", 100, tarfile.REGTYPE, None)], protected=True)
        with self.assertRaises(ValueError) as ctx:
            ha_import.inspect_backup(self.cfg, "not the key", {"demo"})
        self.assertIn("wrong encryption key?", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
