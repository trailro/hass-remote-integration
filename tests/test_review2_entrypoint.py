"""The install page agrees with the manager on whether a password is set.

auth._configured_password takes a password of spaces or tabs as a password (it locks the UI with a random
one and says why); only line ends mean none.  password_configured() decides whether the install page shows
its log to anyone, and read a blank password as no password at all.
"""

import tempfile
import unittest

from tests.fakes import entrypoint_for


class PasswordConfiguredTest(unittest.TestCase):
    def _configured(self, **env):
        ep = entrypoint_for(self, tempfile.mkdtemp(), **{"HRI_PASSWORD": "", "HRI_PASSWORD_FILE": "", **env})
        return ep.password_configured()

    def test_a_password_of_spaces_or_tabs_is_one(self):
        self.assertTrue(self._configured(HRI_PASSWORD="   "))
        self.assertTrue(self._configured(HRI_PASSWORD=" \t "))

    def test_only_line_ends_are_none(self):
        self.assertFalse(self._configured(HRI_PASSWORD=""))
        self.assertFalse(self._configured(HRI_PASSWORD="\r"))
        self.assertFalse(self._configured(HRI_PASSWORD="\r\n"))

    def test_a_real_password_or_a_password_file(self):
        self.assertTrue(self._configured(HRI_PASSWORD="hunter2"))
        self.assertTrue(self._configured(HRI_PASSWORD_FILE="/run/secrets/hri"))


if __name__ == "__main__":
    unittest.main()
