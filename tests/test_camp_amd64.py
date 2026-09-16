"""Test campaign findings: a backup that carries the pinned http port, and the crash loop a restore of it left behind."""

import inspect
import json
import os
import shutil
import tempfile
import unittest
import zipfile

import backupkit
import run


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-camp-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def _http_store(port, key="stable"):
    data = {"stable": None, "pending": None, "yaml_migration_done": True}
    data[key] = {"server_port": port, "ip_ban_enabled": True}
    return json.dumps({"version": 2, "minor_version": 2, "key": "http", "data": data})


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class HttpPortIsNotBackedUpTest(unittest.TestCase):
    """The pinned port must not travel to another container inside a backup."""

    def test_storage_http_is_left_out(self):
        cfg = _tmp(self)
        _write(os.path.join(cfg, backupkit.MARKER), "{}")
        _write(os.path.join(cfg, ".storage", "http"), _http_store(8087))
        _write(os.path.join(cfg, ".storage", "http.auth"), "{}")
        _write(os.path.join(cfg, ".storage", "core.config_entries"), "{}")
        rec = backupkit.create(cfg)
        with zipfile.ZipFile(os.path.join(cfg, backupkit.BACKUP_DIR, rec["name"])) as zf:
            names = zf.namelist()
        self.assertNotIn(".storage/http", names)
        self.assertIn(".storage/http.auth", names)  # only the port pin goes, not the rest of .storage
        self.assertIn(".storage/core.config_entries", names)


class ForeignHttpPortHealsTest(unittest.TestCase):
    """A backup made on another HRI_PORT used to end every boot at the port check: a crash loop with no UI."""

    def store(self, cfg):
        return os.path.join(cfg, ".storage", "http")

    def test_a_foreign_pin_is_removed(self):
        cfg = _tmp(self)
        _write(self.store(cfg), _http_store(8123))
        self.assertEqual(run.drop_foreign_http_port(cfg, 8087), 8123)
        self.assertFalse(os.path.exists(self.store(cfg)))

    def test_a_pending_pin_counts_too(self):
        cfg = _tmp(self)
        _write(self.store(cfg), _http_store(9999, key="pending"))
        self.assertEqual(run.drop_foreign_http_port(cfg, 8087), 9999)

    def test_our_own_store_is_kept(self):
        cfg = _tmp(self)
        _write(self.store(cfg), _http_store(8087))
        self.assertIsNone(run.drop_foreign_http_port(cfg, 8087))
        self.assertTrue(os.path.exists(self.store(cfg)))

    def test_no_store_and_a_broken_one_are_no_problem(self):
        cfg = _tmp(self)
        self.assertIsNone(run.drop_foreign_http_port(cfg, 8087))
        _write(self.store(cfg), "{not json")
        self.assertIsNone(run.drop_foreign_http_port(cfg, 8087))

    def test_the_boot_clears_the_store_before_http_is_set_up(self):
        src = inspect.getsource(run._boot)
        self.assertLess(src.index("drop_foreign_http_port"), src.index('"http", "integration_manager"'),
                        "the store is cleared too late to help this boot")


if __name__ == "__main__":
    unittest.main()
