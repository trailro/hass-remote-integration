"""Review of b4cd1a1.  S5-5: the "Not in a backup" list of docs/backups.md left out .storage/core.uuid and
mqtt_undiscover.json, which backupkit keeps live (KEEP_LIVE_GLOBS): a restore never rolls them back either.  End-to-end
run: docs/mqtt.md said an entity excluded while the container was down is removed at the next connection, where the
orphan sweep removes it five minutes after the start."""

import os
import re
import unittest

import backupkit
from custom_components.integration_manager import mqtt_publisher as mp

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


class ExcludedWhileDownTest(unittest.TestCase):
    def test_the_rules_table_names_the_orphan_sweep(self):
        with open(os.path.join(ROOT, "docs", "mqtt.md"), encoding="utf-8") as fh:
            [row] = [line for line in fh if line.startswith("| `exclude` |")]
        self.assertNotIn("next connection", row)
        self.assertIn(f"{mp.ORPHAN_SWEEP_DELAY_S // 60} minutes after the start", row.replace("five", "5"))


class DamagedRulesFileTest(unittest.TestCase):
    """docs/files.md lists what happens to each damaged file: mqtt_rules.json and its .corrupt copies were missing."""

    def test_the_rules_file_has_its_row(self):
        from custom_components.integration_manager import mqtt_rules

        with open(os.path.join(ROOT, "docs", "files.md"), encoding="utf-8") as fh:
            [row] = [line for line in fh if line.startswith("| `mqtt_rules.json` | Unreadable")]
        self.assertIn("mqtt_rules.json.corrupt-<stamp>", row)
        self.assertIn(f"newest {('one', 'two', 'three', 'four')[mqtt_rules.CORRUPT_KEEP - 1]}", row)
        for recovery in ("put it back", "Reconnect", "remove"):
            self.assertIn(recovery, row)


if __name__ == "__main__":
    unittest.main()
