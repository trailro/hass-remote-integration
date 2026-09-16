"""Review round 12 (m1): the uploaded .tar of a Home Assistant backup is read with extended headers bounded too."""

import json
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

from tests.test_r9_boot import _write_member


class OuterTarHeaderTest(unittest.TestCase):
    """m1: the uploaded .tar itself was opened without the 1 MB bound on extended headers: tarfile read a long-name
    header of any size into memory whole (up to the 2 GB upload)."""

    def setUp(self):
        from custom_components.integration_manager import ha_import

        self.ha_import = ha_import
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.path = os.path.join(self.cfg, ha_import.IMPORT_TAR)
        os.makedirs(os.path.dirname(self.path))

    def write(self, first, then):
        meta = json.dumps({"name": "t", "compressed": True, "protected": False}).encode()
        with open(self.path, "wb") as out:
            for member in first:
                _write_member(out, *member)
            _write_member(out, "backup.json", len(meta), fill=lambda k: meta[:k])
            for member in then:
                _write_member(out, *member)
            out.write(bytes(1024))

    def inspect(self, spy_name):
        read = []
        real = getattr(tarfile.TarInfo, spy_name)

        def spy(info, tar):
            read.append(info.size)  # tarfile reads the whole header data into memory here
            return real(info, tar)

        with mock.patch.object(tarfile.TarInfo, spy_name, spy), self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        return str(ctx.exception), read

    def test_a_huge_long_name_header_first_is_refused_before_it_is_read(self):
        self.write([("././@LongLink", 8 * 1024**2, tarfile.GNUTYPE_LONGNAME)], [])
        error, read = self.inspect("_proc_gnulong")
        self.assertIn("extended tar header", error)
        self.assertEqual(read, [])
        self.assertFalse(os.path.exists(os.path.join(self.cfg, self.ha_import.EXTRACT_DIR)))

    def test_a_huge_pax_header_later_is_refused_before_it_is_read(self):
        self.write([], [("PaxHeader", 8 * 1024**2, tarfile.XHDTYPE), ("homeassistant.tar.gz", 0)])
        error, read = self.inspect("_proc_pax")
        self.assertIn("extended tar header", error)
        self.assertEqual(read, [])

    def test_a_small_long_name_header_is_still_read(self):
        name = ("x" * 200 + "/backup.json").encode()
        self.write([("././@LongLink", len(name) + 1, tarfile.GNUTYPE_LONGNAME, lambda k: (name + b"\0")[:k]), ("ignored", 0)], [])
        error, read = self.inspect("_proc_gnulong")
        self.assertEqual(read, [len(name) + 1])
        self.assertIn("homeassistant.tar.gz", error)  # got past the headers to what the backup lacks


if __name__ == "__main__":
    unittest.main()
