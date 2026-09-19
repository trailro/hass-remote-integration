"""The install page's host rule and the manager's are one policy written twice.

``entrypoint.status_host_ok`` serves the page while Home Assistant installs, so
it runs before Home Assistant exists and cannot import
``hostguard`` (aiohttp, homeassistant.core).  The rule is therefore duplicated,
and the duplication cannot be removed.  What was missing is anything making the
two agree: a change to one that is not made to the other opens the install page
to a name the manager refuses, or refuses one it allows, and no test noticed.

These compare the two implementations host for host on one corpus.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from custom_components.integration_manager import hostguard
from tests.fakes import entrypoint_for

HOSTNAME = "box"

# every shape a Host header arrives in, and the ones that must stay refused
CORPUS = [
    "localhost", "localhost.", "localhost:8087", "LOCALHOST", "  localhost  ",
    HOSTNAME, f"{HOSTNAME}.", f"{HOSTNAME}:8087", HOSTNAME.upper(),
    "127.0.0.1", "127.0.0.1:8087", "192.168.1.5", "10.0.0.1:80", "0.0.0.0",
    "::1", "[::1]", "[::1]:8087", "[fe80::1]:8087",
    "hri.local", "hri.local.", "hri.local:8087", "HRI.LOCAL",
    "x.lan", "x.home", "x.internal", "x.home.arpa", "x.localdomain",
    "deep.sub.hri.local",
    "evil.example.com", "evil.example.com.", "evil.example.com:8087",
    "hri.locale", "localhostx", "xlocalhost", "local", ".local",
    "hri.local.evil.com", "evil.com/hri.local",
    "", " ", ".", "..", ":", ":8087", "[", "[]", "[]:1",
    "999.999.999.999", "1.2.3", "1.2.3.4.5",
]

EXTRA_RAW = "hri.example.com, Other.Test , trailing.dot.name."
EXTRA_CORPUS = [
    "hri.example.com", "HRI.EXAMPLE.COM", "hri.example.com.", "hri.example.com:8087",
    "other.test", "trailing.dot.name", "trailing.dot.name.",
    "nope.example.com", "hri.example.com.evil.com",
]


class HostGuardParityTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.state = os.path.join(self.cfg, "integration_manager")
        os.makedirs(self.state, exist_ok=True)
        self.ep = entrypoint_for(self, self.cfg)
        hostguard._own_hostname.cache_clear()
        self.addCleanup(hostguard._own_hostname.cache_clear)

    def _write_allowed(self, raw):
        with open(os.path.join(self.state, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"allowed_hosts": raw}, fh)

    def _compare(self, hosts, raw=""):
        extra = hostguard._allowed(raw)
        disagreed = []
        for host in hosts:
            manager = hostguard._host_ok(host, extra)
            page = self.ep.status_host_ok(host)
            if manager != page:
                disagreed.append(f"{host!r}: manager={manager} install page={page}")
        self.assertEqual(disagreed, [], "the two host rules disagree:\n  " + "\n  ".join(disagreed))

    def test_the_two_rules_agree_without_extra_names(self):
        self._write_allowed("")
        with mock.patch.object(self.ep.socket, "gethostname", return_value=HOSTNAME), \
             mock.patch.object(hostguard.socket, "gethostname", return_value=HOSTNAME):
            hostguard._own_hostname.cache_clear()
            self._compare(CORPUS)

    def test_the_two_rules_agree_on_allowed_hosts(self):
        self._write_allowed(EXTRA_RAW)
        with mock.patch.object(self.ep.socket, "gethostname", return_value=HOSTNAME), \
             mock.patch.object(hostguard.socket, "gethostname", return_value=HOSTNAME):
            hostguard._own_hostname.cache_clear()
            self._compare(CORPUS + EXTRA_CORPUS, EXTRA_RAW)

    def test_the_two_rules_agree_when_the_container_name_ends_in_a_dot(self):
        """gethostname() is not guaranteed to come back bare: the manager strips
        a trailing dot from it, the install page did not."""
        self._write_allowed("")
        with mock.patch.object(self.ep.socket, "gethostname", return_value="box."), \
             mock.patch.object(hostguard.socket, "gethostname", return_value="box."):
            hostguard._own_hostname.cache_clear()
            self._compare(["box", "box.", "box:8087"])

    def test_both_carry_the_same_safe_suffixes(self):
        self.assertEqual(tuple(self.ep.SAFE_HOST_SUFFIXES), tuple(hostguard.SAFE_SUFFIXES))

    def test_a_settings_file_the_page_cannot_read_refuses_the_extra_names(self):
        """The page reads allowed_hosts off disk on every request; a broken file
        must fall back to no extra names, not to allowing everything."""
        with open(os.path.join(self.state, "settings.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with mock.patch.object(self.ep.socket, "gethostname", return_value=HOSTNAME):
            self.assertFalse(self.ep.status_host_ok("hri.example.com"))
            self.assertTrue(self.ep.status_host_ok("hri.local"))


if __name__ == "__main__":
    unittest.main()
