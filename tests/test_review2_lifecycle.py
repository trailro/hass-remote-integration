"""Review 2, lifecycle items M-03, M-11, C-05, C-07, C-02, C-15.

M-03: a backup label with letters outside ASCII became a file name the backup endpoints refused (download 404,
restore and delete "bad name"): only pruning could remove it.  M-11: a partial restore that brings back the
manager of one integration over the config entries of another.  C-05: a restore schedule without a "force" key
was applied as forced.  C-07: the venvs System lists as installed include ones the entrypoint removes at the next
boot (another Python).  C-02: one Home Assistant version pattern in ha_updater, the one the entrypoint uses.
C-15: a second install in one boot truncated ha-install.log, and with it the pip output of the failed first one."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.fakes import entrypoint_for
from tests.test_review_backup import _hass, _volume, _zip


def _view(cfg, running=None):
    from custom_components.integration_manager import backup_views

    installer = SimpleNamespace(busy=False, protected_backups=set, running=running, state=SimpleNamespace(rollback_backup=None))
    view = backup_views.BackupActionView(_hass(cfg), installer)
    view.json = lambda d: d
    return view


def _post(view, name, action, body=None):
    from custom_components.integration_manager import backup_views

    return asyncio.run(backup_views.BackupActionView.post.__wrapped__(view, None, body or {}, name, action))


class AsciiBackupNameTest(unittest.TestCase):
    """M-03"""

    def setUp(self):
        from custom_components.integration_manager import backup_views

        self.bv = backup_views
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)

    def test_a_romanian_label_gives_a_name_the_endpoints_take(self):
        rec = backupkit.create(self.cfg, "înainte de update", "2026.8.3")
        self.assertTrue(rec["name"].endswith("-inainte-de-update.zip"), rec["name"])
        self.assertTrue(self.bv._name_ok(rec["name"]))
        self.assertIsNotNone(self.bv._NAME_RE.match(rec["name"]))
        self.assertEqual(backupkit.describe(self.cfg, rec["name"])["label"], "înainte de update")  # kept as typed

    def test_comma_below_letters_and_other_alphabets(self):
        self.assertEqual(backupkit.file_label("șțȘȚ ăâî"), "stST-aai")
        self.assertEqual(backupkit.file_label("café"), "cafe")
        self.assertEqual(backupkit.file_label("日本語"), "")
        rec = backupkit.create(self.cfg, "日本語", "2026.8.3")
        self.assertIsNotNone(self.bv._NAME_RE.match(rec["name"]), rec["name"])

    def _legacy(self, name="20260101-000000-înainte.zip"):
        src = backupkit.create(self.cfg, "x", "2026.8.3")["name"]
        os.rename(os.path.join(self.cfg, backupkit.BACKUP_DIR, src), os.path.join(self.cfg, backupkit.BACKUP_DIR, name))
        return name

    def test_an_older_backup_with_such_a_name_can_be_downloaded_and_deleted(self):
        name = self._legacy()
        self.assertIn(name, [b["name"] for b in backupkit.list_backups(self.cfg)])
        view = _view(self.cfg)
        resp = asyncio.run(view.get(None, name, "download"))
        disposition = resp.headers["Content-Disposition"]
        self.assertTrue(disposition.isascii(), disposition)
        self.assertIn("filename*=UTF-8''20260101-000000-%C3%AEnainte.zip", disposition)
        self.assertEqual(_post(view, name, "delete"), {"ok": True})
        self.assertFalse(os.path.exists(os.path.join(self.cfg, backupkit.BACKUP_DIR, name)))

    def test_paths_stay_refused(self):
        for bad in ("../x.zip", "a/b.zip", "é/..zip", "é..zip", ".é.zip", "é\n.zip", "é.zip\n", "é" * 130 + ".zip", "é.tar"):
            self.assertFalse(self.bv._name_ok(bad), bad)


def _backup_of(cfg, name, domain):
    bdir = os.path.join(cfg, backupkit.BACKUP_DIR)
    os.makedirs(bdir, exist_ok=True)
    with zipfile.ZipFile(os.path.join(bdir, name), "w") as zf:
        zf.writestr(backupkit.MARKER, json.dumps({"domain": domain, "installed": {domain: {}} if domain else {}}))
        zf.writestr(".storage/core.config_entries", json.dumps({"from": name}))
        zf.writestr("backup-info.json", json.dumps({"ha_version": "2026.8.3"}))


class ManagerOfAnotherIntegrationTest(unittest.TestCase):
    """M-11"""

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        _backup_of(self.cfg, "x.zip", "xdom")

    def test_manager_only_of_another_integration_is_refused(self):
        r = _post(_view(self.cfg, running="ydom"), "x.zip", "restore", {"parts": ["manager"]})
        self.assertFalse(r.get("ok"), r)
        self.assertIn("xdom", r["error"])
        self.assertFalse(backupkit.pending(self.cfg))

    def test_storage_only_of_another_integration_is_refused(self):
        r = _post(_view(self.cfg, running="ydom"), "x.zip", "restore", {"parts": ["storage"]})
        self.assertFalse(r.get("ok"), r)
        self.assertFalse(backupkit.pending(self.cfg))

    def test_both_together_everything_or_the_same_integration_are_scheduled(self):
        for parts, running in ((["storage", "manager"], "ydom"), (None, "ydom"), (["manager"], "xdom"), (["storage"], "xdom"),
                               (["custom_components"], "ydom"), (["yaml"], None)):
            r = _post(_view(self.cfg, running=running), "x.zip", "restore", {"parts": parts} if parts else {})
            self.assertTrue(r.get("ok"), (parts, running, r))
            backupkit.cancel_restore(self.cfg)

    def test_nothing_ran_then_and_something_runs_now(self):
        _backup_of(self.cfg, "none.zip", None)
        r = _post(_view(self.cfg, running="ydom"), "none.zip", "restore", {"parts": ["manager"]})
        self.assertFalse(r.get("ok"), r)
        self.assertIn("no integration ran", r["error"])

    def test_an_archive_that_does_not_say_is_not_refused(self):
        _zip(self.cfg, "old.zip", {"ha_version": "2026.8.3"})  # state.json "{}": no domain key...
        with zipfile.ZipFile(os.path.join(self.cfg, backupkit.BACKUP_DIR, "odd.zip"), "w") as zf:
            zf.writestr(backupkit.MARKER, "not json")
            zf.writestr(".storage/core.config_entries", "{}")
            zf.writestr("backup-info.json", json.dumps({"ha_version": "2026.8.3"}))
        self.assertIs(backupkit.backup_domain(self.cfg, "odd.zip"), backupkit.UNKNOWN_DOMAIN)
        self.assertIsNone(backupkit.backup_domain(self.cfg, "old.zip"))  # ...which is what "nothing ran" looks like
        self.assertEqual(backupkit.backup_domain(self.cfg, "x.zip"), "xdom")
        r = _post(_view(self.cfg, running="ydom"), "odd.zip", "restore", {"parts": ["manager"]})
        self.assertTrue(r.get("ok"), r)


class UnforcedScheduleTest(unittest.TestCase):
    """C-05"""

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        _zip(self.cfg, "nov.zip", {"created": "20200101-000000"})

    def _without_force_key(self):
        backupkit.schedule_restore(self.cfg, "nov.zip", ["storage"], force=True)
        path = os.path.join(self.cfg, backupkit.PENDING_META)
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        del meta["force"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)

    def test_a_schedule_without_the_force_key_is_not_forced(self):
        self._without_force_key()
        self.assertFalse(backupkit.pending_forced(self.cfg))

    def test_the_boot_drops_it_with_a_message(self):
        self._without_force_key()
        ep = entrypoint_for(self, self.cfg)
        state = {"current": "2026.8.3"}
        with mock.patch.object(ep, "log"), mock.patch.object(ep, "save_state", return_value=True):
            ep.apply_config_changes(state, "2026.8.3", "2026.8.3")
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertIn("not forced", state.get("last_error", ""))
        with open(os.path.join(self.cfg, ".storage", "s0"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "orig0")


class InstalledVenvsTest(unittest.TestCase):
    """C-07, C-02"""

    def setUp(self):
        from custom_components.integration_manager import ha_updater

        self.ha_updater = ha_updater
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p)))
        self.updater = ha_updater.HaUpdater(hass)

    def _venv(self, version, python):
        venv = os.path.join(self.cfg, f"venv-{version}")
        pkg = os.path.join("lib", f"python{python}", "site-packages", "homeassistant")
        for folder in ("bin", pkg):
            os.makedirs(os.path.join(venv, folder))
        for marker in (".ok", "bin/python", os.path.join(pkg, "__init__.py")):
            open(os.path.join(venv, marker), "w").close()

    def test_a_venv_of_another_python_is_not_listed(self):
        major, minor = sys.version_info[:2]
        self._venv("2026.8.3", f"{major}.{minor}")
        self._venv("2026.6.1", f"{major}.{minor - 1}")  # built before the image's Python bump: the next boot removes it
        self.assertEqual(self.updater._installed_venvs(), ["2026.8.3"])

    def test_one_version_pattern_the_entrypoint_uses_too(self):
        ep = entrypoint_for(self, self.cfg)
        self.assertEqual(self.ha_updater._VERSION.pattern, ep.VERSION_RE.pattern)
        for bad in ("2026.9", "x", "2026.9.0-1", "2026.9.0b"):
            with self.assertRaises(ValueError):
                self.updater.set_desired(bad)
        self.assertEqual(self.updater.set_desired("2026.9.0b1")["desired"], "2026.9.0b1")


class InstallLogTest(unittest.TestCase):
    """C-15: the image's own version installed after the wanted one failed kept the file, and so the reason"""

    def test_a_second_install_of_the_boot_keeps_the_first_ones_pip_output(self):
        cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        ep = entrypoint_for(self, cfg)

        def run_pip(cmd, out, **_kw):
            if "homeassistant==2026.9.9" in cmd:
                out.write("ERROR: No matching distribution found for homeassistant==2026.9.9\n")
                raise subprocess.CalledProcessError(1, cmd)
            out.write("Successfully installed homeassistant-2026.8.3\n")

        def run(cmd, **_kw):
            os.makedirs(cmd[-1], exist_ok=True)  # the venv
            return subprocess.CompletedProcess(cmd, 0)

        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b""
        with open(ep.LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write("the install of an earlier boot\n")
        with mock.patch.object(ep.subprocess, "run", run), mock.patch.object(ep.urllib.request, "urlopen", return_value=resp), \
                mock.patch.object(ep, "_run_pip", run_pip), mock.patch.object(ep, "_install_log_started", False, create=True), \
                mock.patch.object(ep.os, "sync"), mock.patch.object(ep, "_write_requirements_stamp"):
            self.assertFalse(ep.install("2026.9.9"))
            self.assertTrue(ep.install("2026.8.3"))
        with open(ep.LOG_FILE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("No matching distribution found for homeassistant==2026.9.9", text)
        self.assertIn("Successfully installed homeassistant-2026.8.3", text)
        self.assertNotIn("an earlier boot", text)  # still one boot's installs per file


if __name__ == "__main__":
    unittest.main()
