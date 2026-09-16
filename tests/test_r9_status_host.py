"""The status page served while Home Assistant installs applies the same host rule as the manager."""

import tempfile
import unittest
from unittest import mock

from tests.fakes import entrypoint_for


class StatusHostTrailingDotTest(unittest.TestCase):
    def test_a_trailing_dot_is_the_same_host(self):
        ep = entrypoint_for(self, tempfile.mkdtemp())
        with mock.patch.object(ep.socket, "gethostname", return_value="box"):
            self.assertTrue(ep.status_host_ok("localhost.:8087"))
            self.assertTrue(ep.status_host_ok("box."))
            self.assertFalse(ep.status_host_ok("evil.example."))
            self.assertFalse(ep.status_host_ok("."))


if __name__ == "__main__":
    unittest.main()
