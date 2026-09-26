"""Review of b4cd1a1.  S5-5: the "Not in a backup" list of docs/backups.md left out .storage/core.uuid and
mqtt_undiscover.json, which backupkit keeps live (KEEP_LIVE_GLOBS): a restore never rolls them back either."""

import os
import re
import unittest

import backupkit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _not_in_a_backup() -> str:
    with open(os.path.join(ROOT, "docs", "backups.md"), encoding="utf-8") as fh:
        text = fh.read()
    first = text.index("\n- ", text.index("Not in a backup"))
    return text[first:text.index("\n\n", first)]  # the bullets, up to the paragraph after them


class NotInABackupListTest(unittest.TestCase):
    def test_every_file_kept_live_is_listed(self):
        listed = _not_in_a_backup()
        for glob in backupkit.KEEP_LIVE_GLOBS:
            name = re.sub(r"\*+$", "", glob.removeprefix(f"{backupkit.STATE_DIR}/"))
            with self.subTest(glob=glob):
                self.assertIn(f"`{name}", listed)


if __name__ == "__main__":
    unittest.main()
