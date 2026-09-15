"""Full sweep review: install page host rule, oversized hunks, backup ordering, restore retries, temp files."""

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

import backupkit
from custom_components.integration_manager import patches


def _entrypoint(cfg):
    os.environ["HRI_CONFIG"] = cfg
    sys.modules.pop("entrypoint", None)
    return importlib.import_module("entrypoint")


def _backup(cfg, name, created, ha="2026.8.3"):
    bdir = os.path.join(cfg, backupkit.BACKUP_DIR)
    os.makedirs(bdir, exist_ok=True)
    with zipfile.ZipFile(os.path.join(bdir, name), "w") as zf:
        zf.writestr(backupkit.MARKER, json.dumps({"installed": {}}))
        zf.writestr(".storage/core.config_entries", json.dumps({"from": name}))
        zf.writestr("backup-info.json", json.dumps({"created": created, "ha_version": ha}))


class StatusPageTest(unittest.TestCase):
    def test_host_rule_matches_the_manager(self):
        ep = _entrypoint(tempfile.mkdtemp())
        for host in ("192.168.1.9:8087", "localhost", "[::1]:8087", "hass.lan", "box.home.arpa"):
            self.assertTrue(ep.status_host_ok(host), host)
        for host in ("attacker.example", "", "local.evil.com"):
            self.assertFalse(ep.status_host_ok(host), host)

    def test_allowed_hosts_from_settings(self):
        cfg = tempfile.mkdtemp()
        ep = _entrypoint(cfg)
        os.makedirs(ep.STATE_DIR, exist_ok=True)
        with open(os.path.join(ep.STATE_DIR, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"allowed_hosts": "hri.example.com:443"}, fh)
        self.assertTrue(ep.status_host_ok("hri.example.com"))


class OversizedHunkTest(unittest.TestCase):
    def test_extra_lines_after_a_complete_hunk_reject_the_diff(self):
        diff = "--- a/c.py\n+++ b/c.py\n@@ -1,1 +1,1 @@\n-ICON = 'a'\n+ICON = 'b'\n+EXTRA = 1\n"
        with self.assertRaises(ValueError):
            patches.parse_unified(diff)
        self.assertIn("more lines", patches.validate("c.patch", diff) or "")

    def test_format_patch_signature_is_not_a_line(self):
        diff = "--- a/c.py\n+++ b/c.py\n@@ -1,1 +1,1 @@\n-ICON = 'a'\n+ICON = 'b'\n-- \n2.39.0\n"
        self.assertEqual(len(patches.parse_unified(diff)), 1)


class BackupOrderTest(unittest.TestCase):
    def test_newest_is_when_it_was_made_not_the_file_time(self):
        cfg = tempfile.mkdtemp()
        _backup(cfg, "20260915-100000-own.zip", "20260915-100000")
        _backup(cfg, "upload-old.zip", "20260101-000000")  # an older backup uploaded now: newest file
        names = [b["name"] for b in backupkit.list_backups(cfg)]
        self.assertEqual(names, ["20260915-100000-own.zip", "upload-old.zip"])

    def test_config_backup_prefers_this_volumes_own(self):
        from custom_components.integration_manager.ha_updater import HaUpdater

        up = HaUpdater.__new__(HaUpdater)
        listed = [{"name": "upload-x.zip", "ha_version": "2026.8.3"}, {"name": "20260910-own.zip", "ha_version": "2026.8.3"}]
        self.assertEqual(up.config_backup_for("2026.8.3", listed)["name"], "20260910-own.zip")
        self.assertEqual(up.config_backup_for("2026.8.3", listed[:1])["name"], "upload-x.zip")


class RestoreRetryTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cfg, ".storage"))
        os.makedirs(os.path.join(self.cfg, backupkit.STATE_DIR))
        with open(os.path.join(self.cfg, ".storage", "core.config_entries"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"from": "current"}))
        with open(os.path.join(self.cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
            fh.write("{}")
        _backup(self.cfg, "b.zip", "20260101-000000")

    def test_retry_reuses_the_first_pre_restore_copy(self):
        backupkit.schedule_restore(self.cfg, "b.zip")
        first = backupkit.create(self.cfg, "pre-restore")
        meta_path = os.path.join(self.cfg, backupkit.PENDING_META)
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({**meta, "pre_restore": first["name"]}, fh)
        result = backupkit.apply_pending(self.cfg, log=lambda *_: None)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pre_restore"], first["name"])

    def test_applied_but_unrecorded_restore_is_not_applied_again(self):
        backupkit.schedule_restore(self.cfg, "b.zip")
        result = backupkit.apply_pending(self.cfg, log=lambda *_: None, record=lambda r: False)
        self.assertTrue(result["ok"], result)
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, backupkit.APPLIED_META)))
        ep = _entrypoint(self.cfg)
        state = {}
        ep.merge_applied_restore(state)
        self.assertTrue(state["last_restore"]["ok"])
        self.assertFalse(os.path.isfile(os.path.join(self.cfg, backupkit.APPLIED_META)))


class TempFileTest(unittest.TestCase):
    def test_interrupted_copies_and_uploads_are_cleaned_and_never_backed_up(self):
        cfg = tempfile.mkdtemp()
        state = os.path.join(cfg, backupkit.STATE_DIR)
        bdir = os.path.join(cfg, backupkit.BACKUP_DIR)
        os.makedirs(state)
        os.makedirs(bdir)
        for p in (os.path.join(state, "restore-pending-abc.zip.tmp"), os.path.join(state, ".restore-pending-def.zip.tmp")):
            open(p, "w").close()
        backupkit._drop_stale_pending(cfg)
        self.assertEqual([n for n in os.listdir(state) if n.endswith(".tmp")], [])
        self.assertTrue(backupkit._excluded("integration_manager/whatever.zip.tmp"))
        old = time.time() - backupkit.PARTIAL_STALE_S - 10
        up = os.path.join(bdir, "tmpab12.zip.tmp")
        open(up, "w").close()
        os.utime(up, (old, old))
        backupkit._drop_dead_partials(bdir)
        self.assertFalse(os.path.exists(up))


if __name__ == "__main__":
    unittest.main()
