"""Review round 12 (C6): an invalid HRI_PORT is said in the log."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.fakes import entrypoint_for


class PortTest(unittest.TestCase):
    """C6: a typo in HRI_PORT was a bare traceback at import, in a restart loop."""

    def test_an_invalid_port_is_said_in_the_log(self):
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        ep = entrypoint_for(self, cfg, HRI_PORT="80a")
        with mock.patch.object(ep, "restrict_umask"), self.assertRaises(SystemExit) as ctx:
            ep.main()
        self.assertEqual(ctx.exception.code, 2)
        with open(ep.LOG_FILE, encoding="utf-8") as fh:
            self.assertIn("HRI_PORT='80a' is not a TCP port", fh.read())

    def test_valid_ports(self):
        ep = entrypoint_for(self, tempfile.mkdtemp())
        self.assertEqual([ep._parse_port(v) for v in ("8087", "0", "65536", "-1", "")], [8087, None, None, None, None])  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
