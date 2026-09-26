"""Review of b4cd1a1, installer / watchdog / patches / loop IO:

S2-1  a YAML-only integration whose stored YAML did not load reported "no config entry, no YAML setup", the
      reason the watchdog leaves alone: it was never acted on.
S2-2  two watchdog refusals never expired: a config entry setting up forever, and a deferred start kept
      blocked for a Home Assistant version that did not boot.
S2-3  an automatic restart on a ledger that could not be written: the boot reads the file without that
      restart, the daily cap and the backoff start again, and it loops.
S2-4  applying a .patch re-encoded the whole target (errors="replace", LF endings).
S2-5  a crafted hacs.json minimum Home Assistant version (thousands of digits) raised out of ha_vkey.
S2-6  the builder's Prepare with replace + a Home Assistant change could remove the integration there was and
      then have the change refused.
S2-7  a failed .patch apply could leave .tmp files next to the code.
S3-3  the health document and last_error carried a URL token unmasked.
S5-1  file reads on the event loop: protected_backups(), restore-pending.json, the rebuild plan, the import
      tar, manifest.json and the registries.
S5-2  one pre-restore backup named with an impossible date broke every prune.
"""

import asyncio
import errno
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import backup_views, build_views, import_views, patches, views
from custom_components.integration_manager import installer as installer_mod
from custom_components.integration_manager import scheduler as scheduler_mod
from custom_components.integration_manager.installer import Installer, State
from tests.test_patches import DIFF, PatchTestCase
from tests.test_watchdog import DOMAIN, _Base as WatchdogBase


def _full_disk(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def _job(func, *args):
    """A real executor: the job runs in a worker thread, where no event loop runs."""
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


class _Where:
    """Records, per call, whether it ran on the event loop."""

    def __init__(self, result=None):
        self.result, self.on_loop = result, []

    def __call__(self, *_args, **_kwargs):
        self.on_loop.append(_on_loop())
        return self.result() if callable(self.result) else self.result


def _request(body=None, headers=None):
    return SimpleNamespace(headers=headers or {}, query={}, content_type="application/json",
                           json=mock.AsyncMock(return_value=body if body is not None else {}))


def _body(response):
    return json.loads(response.body)


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-review4-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _start_installer(test):
    d = _tmp(test)
    inst = object.__new__(Installer)
    inst.hass = SimpleNamespace(async_add_executor_job=_job, config=SimpleNamespace(components=set()), data={})
    inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
    inst.versions_dir = os.path.join(inst.state_dir, "versions")
    inst.state = State(installed={"demo": {"versions": {"2.0": {"requirements": []}}, "running_tag": None}})
    inst.busy = False
    inst._rollback_undo = None
    inst.settings = SimpleNamespace(backup_keep=5)
    inst.async_backup = mock.AsyncMock(return_value={"name": "pre.zip"})
    inst._save_state = lambda: None
    inst.protected_backups = _Where(set)
    os.makedirs(inst._version_dir("demo", "2.0"))
    return inst


# ----- S2-1 -----------------------------------------------------------------------------------------

class YamlOnlyHealthTest(WatchdogBase):
    def _yaml_only(self, with_yaml):
        inst = self.installer()
        inst._entries_of = lambda dom: []
        inst._patch_cache = {}
        if with_yaml:
            os.makedirs(os.path.dirname(inst.yaml_path(DOMAIN)), exist_ok=True)
            with open(inst.yaml_path(DOMAIN), "w", encoding="utf-8") as fh:
                fh.write("demo:\n  host: 192.0.2.1\n")
        inst.health_source = lambda grace=True: inst.health()
        return inst

    def test_stored_yaml_that_did_not_load_is_acted_on(self):
        inst = self._yaml_only(with_yaml=True)
        h = inst.health()
        self.assertEqual(h["state"], "error")
        self.assertNotIn("no YAML setup", h["reason"])  # before the fix: the reason the watchdog leaves alone
        self.assertIn("YAML setup did not load it", h["reason"])
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_awaited_once()  # nothing to reload: the first step is the restart
        self.assertTrue(self.lines("no config entry to reload"), self.emitted)

    def test_without_yaml_it_is_still_left_alone(self):
        inst = self._yaml_only(with_yaml=False)
        self.assertIn("no config entry, no YAML setup", inst.health()["reason"])
        sch = self.scheduler(inst)
        for _ in range(120):
            self.tick(sch)
        inst.restart.assert_not_awaited()


# ----- S2-2 -----------------------------------------------------------------------------------------

class RefusalsExpireTest(WatchdogBase):
    def test_a_setup_that_never_ends_stops_holding_the_watchdog_off(self):
        inst = self.installer()
        inst._entries_of = lambda dom: [SimpleNamespace(state=SimpleNamespace(value="setup_in_progress"), disabled_by=None, title="Hub")]
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("still setting up"), self.emitted)
        for _ in range(Installer.SETUP_WAIT_S // 60 - 1):
            self.tick(sch)
        inst.restart.assert_not_awaited()  # the smoke test's hang limit has not passed yet
        for _ in range(3):
            self.tick(sch)
        inst.restart.assert_awaited_once()  # before the fix: refused for the life of the process

    def test_a_setup_that_ends_is_not_counted_against_the_next_one(self):
        inst = self.installer()
        setting_up = [SimpleNamespace(state=SimpleNamespace(value="setup_in_progress"), disabled_by=None, title="Hub")]
        inst._entries_of = lambda dom: setting_up
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        self.assertIsNotNone(sch._setup_since)
        self.verdict = {"state": "ok", "reason": ""}
        self.tick(sch)
        self.assertIsNone(sch._setup_since)

    def test_a_blocked_deferred_start_does_not_hold_it_off(self):
        inst = self.installer()
        inst.state.pending_start = {"domain": DOMAIN, "tag": "1.0", "ha": "2020.1.0",
                                    "blocked": "Home Assistant is 2026.9.0, not 2020.1.0 (the update failed or was rolled back)"}
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_awaited_once()  # before the fix: "a start is deferred" forever

    def test_a_deferred_start_that_still_applies_still_refuses(self):
        inst = self.installer()
        inst.state.pending_start = {"domain": DOMAIN, "tag": "1.0", "ha": None}
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("a start is deferred"))


# ----- S2-3 -----------------------------------------------------------------------------------------

class LedgerNotWrittenTest(WatchdogBase):
    def test_no_automatic_restart_on_a_ledger_that_was_not_written(self):
        inst = self.installer()
        inst._save_state = _full_disk
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_not_awaited()  # before the fix: restarted, and the boot read a record without it
        status = inst.watchdog_status()
        self.assertEqual((status["restarts_24h"], status["attempts"]), (0, 0))
        self.assertIn("not restarted", status["last"]["next"])
        self.assertIn("No space left on device", status["last"]["next"])
        self.assertEqual(len(self.lines("could not be written to state.json")), 1)

    def test_it_is_said_once_a_window_not_once_a_minute(self):
        inst = self.installer()
        inst._save_state = _full_disk
        sch = self.scheduler(inst)
        for _ in range(16 + 15):
            self.tick(sch)
        self.assertEqual(len(self.lines("could not be written to state.json")), 1)
        inst.restart.assert_not_awaited()

    def test_the_error_is_logged(self):
        inst = self.installer()
        inst._save_state = _full_disk
        sch = self.scheduler(inst)
        quiet = installer_mod._LOGGER
        with mock.patch.object(quiet, "error") as log:
            for _ in range(16):
                self.tick(sch)
        self.assertTrue(any("not restarting the process" in str(c.args[0]) for c in log.call_args_list))


# ----- S2-4 / S2-7 ----------------------------------------------------------------------------------

class PatchKeepsTheBytesTest(PatchTestCase):
    def write_bytes(self, name, data):
        with open(os.path.join(self.comp, name), "wb") as fh:
            fh.write(data)

    def read_bytes(self, name):
        with open(os.path.join(self.comp, name), "rb") as fh:
            return fh.read()

    def test_crlf_endings_are_kept(self):
        self.write_bytes("mod.py", b"a = 1\r\nb = 2\r\nc = 4\r\n")
        self.assertEqual(patches._diff_status(DIFF, self.ctx), "pending")
        self.assertEqual(patches._diff_apply(DIFF, self.ctx), "applied")
        self.assertEqual(self.read_bytes("mod.py"), b"a = 1\r\nb = 3\r\nc = 4\r\n")  # before the fix: LF
        self.assertEqual(patches._diff_status(DIFF, self.ctx), "applied")

    def test_mixed_endings_outside_the_hunk_are_kept(self):
        self.write_bytes("mod.py", b"x = 0\r\n\r\ny = 0\nz = 0\r\na = 1\nb = 2\nc = 4\ntail = 1\r\n")
        diff = "--- a/mod.py\n+++ b/mod.py\n@@ -5,3 +5,3 @@\n a = 1\n-b = 2\n+b = 3\n c = 4\n"
        self.assertEqual(patches._diff_apply(diff, self.ctx), "applied")
        self.assertEqual(self.read_bytes("mod.py"), b"x = 0\r\n\r\ny = 0\nz = 0\r\na = 1\nb = 3\nc = 4\ntail = 1\r\n")

    def test_a_file_that_is_not_utf8_is_refused_not_rewritten(self):
        data = b"a = 1\nb = 2\nc = 4\n# caf\xe9\n"
        self.write_bytes("mod.py", data)
        self.assertIn("not UTF-8", patches._diff_status(DIFF, self.ctx))
        self.assertIn("not UTF-8", patches._diff_apply(DIFF, self.ctx))
        self.assertEqual(self.read_bytes("mod.py"), data)  # before the fix: \xe9 became U+FFFD, status "applied"

    def test_a_failed_tmp_write_leaves_no_tmp_behind(self):
        self.write("two.py", "z\n")
        diff = DIFF + "--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-z\n+Z\n"
        real = open

        def failing_open(path, mode="r", *a, **kw):
            if str(path).endswith("two.py.tmp") and "w" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, mode, *a, **kw)

        with mock.patch("builtins.open", failing_open), self.assertRaises(OSError):
            patches._diff_apply(diff, self.ctx)
        self.assertEqual(sorted(os.listdir(self.comp)), ["mod.py", "two.py"])  # before the fix: mod.py.tmp stayed
        self.assertEqual(self.read("mod.py"), "a = 1\nb = 2\nc = 4\n")
        self.assertEqual(self.read("two.py"), "z\n")


# ----- S2-5 -----------------------------------------------------------------------------------------

def _hacs_zip(value):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("owner-repo-abc123/hacs.json", json.dumps({"name": "x", "homeassistant": value}))
    return buf.getvalue()


class MinHaTest(unittest.TestCase):
    CRAFTED = "2026.1." + "9" * 5000

    def test_a_crafted_value_is_dropped(self):
        self.assertIsNone(Installer._hacs_min_ha(_hacs_zip(self.CRAFTED)))
        for bad in ("2026", "latest", "2026.1.0; x", "2026.1.0" + " " * 40, True, ["2026.1.0"]):
            with self.subTest(bad=bad):
                self.assertIsNone(Installer._hacs_min_ha(_hacs_zip(bad)))
        for ok in ("2024.1", "2026.9.0", "2026.9.0b2"):
            with self.subTest(ok=ok):
                self.assertEqual(Installer._hacs_min_ha(_hacs_zip(ok)), ok)

    def test_a_crafted_value_already_stored_cannot_break_start_or_a_version_change(self):
        inst = object.__new__(Installer)
        inst.state = State(domain="demo", installed={"demo": {"running_tag": "1.0", "versions": {"1.0": {"min_ha": self.CRAFTED}}}})
        self.assertIsNone(inst.min_ha_of("demo", "1.0"))  # views.async_change_ha_version compares it with ha_vkey


# ----- S2-6 -----------------------------------------------------------------------------------------

class PrepareWithHaChangeTest(unittest.TestCase):
    TARGET = "2099.1.0"

    def build(self, install=None):
        view = object.__new__(build_views.BuildPrepareView)
        view.hass, view.publisher = None, None
        view.installer = SimpleNamespace(busy=False, install=install or mock.AsyncMock(return_value={"ok": True, "replaced": "old"}))
        view.updater = SimpleNamespace(status=mock.AsyncMock(return_value={"current": build_views.HA_VERSION}), validate=mock.AsyncMock())
        view._check = SimpleNamespace(_resolve=mock.AsyncMock(return_value=("demo", "v1", self.TARGET, "owner/demo")), checked=lambda *a: True)
        return view

    def run_prepare(self, view, hold_lock=False):
        self.changed = []

        async def change(installer, updater, target, mode, source):
            # the refusals async_change_ha_version checks before it does anything
            if views._ha_change_lock_taken():
                raise ValueError("a Home Assistant version change, a restore or a full rollback is being prepared")
            if installer.busy:
                raise ValueError("another action is running")
            async with views._HA_CHANGE_LOCK:
                self.changed.append(target)
                return {"desired": target, "backup": "pre-ha.zip"}

        async def go():
            request = _request({"domain": "demo", "ref": "v1", "ha": self.TARGET, "replace": True})
            if hold_lock:
                async with views._HA_CHANGE_LOCK:
                    return await view.post(request)
            return await view.post(request)

        with mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value="c" * 40)), \
                mock.patch.object(views, "async_change_ha_version", change), mock.patch.object(build_views.events, "emit"):
            return _body(asyncio.run(go()))

    def test_a_change_being_prepared_refuses_before_anything_is_installed(self):
        view = self.build()
        res = self.run_prepare(view, hold_lock=True)
        self.assertFalse(res["ok"])
        view.installer.install.assert_not_awaited()  # before the fix: installed (and replaced), then refused

    def test_no_other_change_can_start_while_the_install_runs(self):
        seen = []

        async def install(*a, **kw):
            seen.append(views._ha_change_lock_taken())
            return {"ok": True, "replaced": "old"}

        res = self.run_prepare(self.build(install=install))
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen, [True])  # before the fix: the System page could take the lock here
        self.assertEqual(self.changed, [self.TARGET])
        self.assertFalse(views._ha_change_lock_taken())

    def test_an_action_started_during_the_mqtt_reconnect_does_not_refuse_the_change(self):
        view = self.build()

        async def reconnect():
            view.installer.busy = True  # a start from the UI, while the reconnect awaited

        view.publisher = SimpleNamespace(async_reconnect=reconnect)
        res = self.run_prepare(view)
        self.assertTrue(res["ok"], res)  # before the fix: the reconnect ran first and the change was refused
        self.assertEqual(self.changed, [self.TARGET])

    def test_a_failed_install_releases_the_lock(self):
        view = self.build(install=mock.AsyncMock(return_value={"ok": False, "error": "GitHub said no"}))
        res = self.run_prepare(view)
        self.assertFalse(res["ok"])
        self.assertEqual(self.changed, [])
        self.assertFalse(views._ha_change_lock_taken())


# ----- S3-3 -----------------------------------------------------------------------------------------

SECRET = "hunter2secretvalue"


class HealthMaskedTest(WatchdogBase):
    def test_the_health_document_masks_reasons_and_the_last_error(self):
        inst = self.installer()
        inst._patch_cache = {}
        inst._entries_of = lambda dom: [SimpleNamespace(title="Hub", state=SimpleNamespace(value="setup_retry"), disabled_by=None,
                                                        reason=f"cannot connect to http://192.0.2.1/api?token={SECRET}")]
        inst.state.last_error = f"ConnectError: https://bob:{SECRET}@192.0.2.1/x"
        doc = json.dumps(inst.health())
        self.assertNotIn(SECRET, doc)  # before the fix: in entries[].reason, reason and last_error
        self.assertIn("token=***", doc)

    def test_a_failed_start_stores_a_masked_last_error(self):
        inst = _start_installer(self)
        inst._ensure_deployed = mock.Mock(side_effect=RuntimeError(f"cannot fetch https://192.0.2.1/x?token={SECRET}"))
        with mock.patch.object(backupkit, "prune", return_value=[]), mock.patch.object(installer_mod.events, "emit"), \
                self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = asyncio.run(inst.start("demo", "2.0"))
        self.assertFalse(res["ok"])
        self.assertNotIn(SECRET, inst.state.last_error)
        self.assertIn("token=***", inst.state.last_error)

    def test_a_refused_unload_stores_a_masked_last_error(self):
        inst = self.installer()
        inst._remove_domain = mock.AsyncMock(side_effect=RuntimeError(f"unload failed: password={SECRET}"))
        inst.rollback_restore_refusal = lambda: None
        res = asyncio.run(inst.uninstall(DOMAIN))
        self.assertFalse(res["ok"])
        self.assertNotIn(SECRET, inst.state.last_error)


# ----- S5-1 -----------------------------------------------------------------------------------------

class InstallerOffTheLoopTest(unittest.TestCase):
    def test_install_reads_restore_pending_in_the_executor(self):
        inst = _start_installer(self)
        inst.rollback_restore_refusal = lambda: None
        pending = _Where(True)
        with mock.patch.object(backupkit, "pending", pending):
            res = asyncio.run(inst.install("2.0", domain="demo"))
        self.assertIn("a restore is scheduled", res["error"])
        self.assertEqual(pending.on_loop, [False])
        self.assertFalse(inst.busy)

    def test_start_reads_restore_pending_and_prunes_in_the_executor(self):
        inst = _start_installer(self)
        inst._ensure_deployed = mock.Mock(side_effect=RuntimeError("stop here"))
        archive = _Where(None)
        prune = _Where([])
        with mock.patch.object(backupkit, "pending_archive", archive), mock.patch.object(backupkit, "prune", prune), \
                mock.patch.object(installer_mod.events, "emit"), self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            asyncio.run(inst.start("demo", "2.0"))
        self.assertEqual(archive.on_loop, [False])
        self.assertEqual(inst.protected_backups.on_loop, [False])  # before the fix: evaluated as prune's argument, on the loop
        self.assertFalse(inst.busy)

    def test_start_is_still_refused_while_a_restore_is_pending(self):
        inst = _start_installer(self)
        inst.rollback_restore_refusal = lambda: None
        with mock.patch.object(backupkit, "pending_archive", _Where("/x/backups/b.zip")):
            res = asyncio.run(inst.start("demo", "2.0"))
        self.assertIn("a restore is scheduled", res["error"])
        self.assertFalse(inst.busy)

    def test_replace_prunes_in_the_executor(self):
        inst = _start_installer(self)
        inst.state.installed = {"old": {"versions": {"1.0": {}}}}
        inst._remove_domain = mock.AsyncMock()
        with mock.patch.object(backupkit, "prune", _Where([])), mock.patch.object(installer_mod.events, "emit"):
            asyncio.run(inst._replace_current("demo"))
        self.assertEqual(inst.protected_backups.on_loop, [False])

    def test_requirements_read_the_manifest_in_the_executor(self):
        inst = _start_installer(self)
        inst.installed_manifest = _Where({"requirements": ["a==1"]})
        self.assertEqual(asyncio.run(inst._requirements_for("demo")), ["a==1"])
        self.assertEqual(inst.installed_manifest.on_loop, [False])

    def test_preview_reads_the_running_manifest_in_the_executor(self):
        inst = _start_installer(self)
        inst.state.installed["demo"]["running_tag"] = "2.0"
        inst.spec = lambda dom: {"repo": "owner/demo"}
        inst.settings = SimpleNamespace(github_headers=lambda: {})
        inst._releases_cache = {}
        inst._requirement_versions = lambda reqs: {}
        inst._manifest_at = _Where({"version": "2.0", "requirements": []})
        manifest = json.dumps({"domain": "demo", "version": "3.0", "requirements": []}).encode()

        class Resp:
            status = 200
            content_length = None
            content = SimpleNamespace(iter_chunked=lambda n: _chunks([manifest]))

        class Session:
            def get(self, url, **kw):
                class Ctx:
                    async def __aenter__(self):
                        return Resp()

                    async def __aexit__(self, *a):
                        return False
                return Ctx()

        with mock.patch.object(installer_mod, "async_get_clientsession", return_value=Session()):
            res = asyncio.run(inst.preview("demo", "3.0"))
        self.assertEqual(res["installed_version"], "2.0")
        self.assertEqual(inst._manifest_at.on_loop, [False])

    def test_registering_reads_and_writes_the_registries_in_the_executor(self):
        d = _tmp(self)
        os.makedirs(os.path.join(d, "integration_manager"))
        inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=d), async_add_executor_job=_job))
        seen = _Where({})
        real = installer_mod._registry_integrations

        def spy(path):
            seen(path)
            return real(path)

        with mock.patch.object(installer_mod, "_registry_integrations", spy):
            res = _body(asyncio.run(views.RegistryView(inst).post(_request({"domain": "demo", "repo": "owner/demo"}))))
        self.assertTrue(res["ok"], res)
        self.assertTrue(seen.on_loop)
        self.assertNotIn(True, seen.on_loop)  # before the fix: the built-in registry was read on the loop


async def _chunks(chunks):
    for c in chunks:
        yield c


class BackupViewsOffTheLoopTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _tmp(self)
        os.makedirs(os.path.join(self.cfg, backupkit.BACKUP_DIR))
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job)

    def test_create_prunes_with_protected_backups_in_the_executor(self):
        protected = _Where(set)
        installer = SimpleNamespace(async_backup_exclusive=mock.AsyncMock(return_value={"name": "new.zip"}),
                                    settings=SimpleNamespace(backup_keep=5), protected_backups=protected)
        with mock.patch.object(backupkit, "prune", return_value=[]):
            res = _body(asyncio.run(backup_views.BackupCreateView(self.hass, installer).post(_request({}))))
        self.assertTrue(res["ok"], res)
        self.assertEqual(protected.on_loop, [False])

    def test_delete_checks_protected_backups_in_the_executor(self):
        name = "20260101-000000-daily.zip"
        with open(os.path.join(self.cfg, backupkit.BACKUP_DIR, name), "wb") as fh:
            fh.write(b"x")
        protected = _Where(lambda: {name})
        installer = SimpleNamespace(protected_backups=protected, busy=False)
        with mock.patch.object(backupkit, "restore_needs", return_value=set()):
            res = _body(asyncio.run(backup_views.BackupActionView(self.hass, installer).post(_request({}), name=name, action="delete")))
        self.assertFalse(res["ok"])
        self.assertIn("still needed", res["error"])
        self.assertEqual(protected.on_loop, [False])

    def test_the_daily_backup_prunes_in_the_executor(self):
        protected = _Where(set)
        installer = SimpleNamespace(settings=SimpleNamespace(bool_=lambda key: key == "backup_daily", backup_keep=5, int_=lambda *a: 0),
                                    busy=False, backup_running=False, config_dir=self.cfg, protected_backups=protected,
                                    async_backup_exclusive=mock.AsyncMock(return_value={"name": "d.zip", "bytes": 1}),
                                    state=SimpleNamespace(last_release_check=time.time()))
        sch = scheduler_mod.Scheduler.__new__(scheduler_mod.Scheduler)
        sch.hass, sch.installer, sch._retry = self.hass, installer, None
        with mock.patch.object(backupkit, "prune", return_value=[]):
            asyncio.run(sch._daily(None))
        self.assertEqual(protected.on_loop, [False])


class ImportViewsOffTheLoopTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _tmp(self)
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p)),
                                    async_add_executor_job=_job)
        self.isfile = []
        real = os.path.isfile

        def spy(path):
            if str(path).startswith(self.cfg):
                self.isfile.append(_on_loop())
            return real(path)

        patcher = mock.patch("os.path.isfile", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stage_rebuild(self):
        path = os.path.join(self.cfg, import_views.ha_import.REBUILD_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")

    def assert_off_the_loop(self):
        self.assertTrue(self.isfile, "nothing was checked")
        self.assertNotIn(True, self.isfile)  # before the fix: os.path.isfile on the loop

    def test_inspect_get(self):
        with mock.patch.object(import_views.ha_import, "load_summary", return_value=None):
            res = _body(asyncio.run(import_views.ImportInspectView(self.hass, None).get(_request())))
        self.assertFalse(res["uploaded"])
        self.assert_off_the_loop()

    def test_inspect_post(self):
        res = _body(asyncio.run(import_views.ImportInspectView(self.hass, None).post(_request({}))))
        self.assertIn("upload a backup first", res["error"])
        self.assert_off_the_loop()

    def test_apply_and_apply_all_refuse_a_staged_rebuild(self):
        self.stage_rebuild()
        installer = SimpleNamespace(busy=False, running=None, state=SimpleNamespace(installed={}))
        res = _body(asyncio.run(import_views.ImportApplyView(self.hass, None, installer).post(_request({"domain": "demo", "entry_id": "abc"}))))
        self.assertEqual(res["error"], import_views._REBUILD_MSG)
        res = _body(asyncio.run(import_views.ImportApplyAllView(self.hass, None, installer).post(_request({}))))
        self.assertEqual(res["error"], import_views._REBUILD_MSG)
        self.assertFalse(installer.busy)
        self.assert_off_the_loop()

    def test_clear_refuses_a_staged_rebuild(self):
        self.stage_rebuild()
        with mock.patch.object(import_views.ha_import, "clear") as clear:
            res = _body(asyncio.run(import_views.ImportClearView(self.hass).post(_request({}))))
        self.assertEqual(res["error"], import_views._REBUILD_MSG)
        clear.assert_not_called()
        self.assert_off_the_loop()

    def test_upload_refuses_a_staged_rebuild(self):
        self.stage_rebuild()
        res = _body(asyncio.run(import_views.ImportUploadView(self.hass).post(_request(headers={"X-Requested-With": "fetch"}))))
        self.assertEqual(res["error"], import_views._REBUILD_MSG)
        self.assert_off_the_loop()


# ----- S5-2 -----------------------------------------------------------------------------------------

class PreRestoreNameTest(unittest.TestCase):
    def test_an_impossible_date_is_protected_not_a_crash(self):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.state_dir = os.path.join(d, "integration_manager")
        inst.state = State()
        os.makedirs(os.path.join(d, "backups"))
        for name in ("20260931-120000-pre-restore.zip", "20200101-120000-pre-restore.zip"):
            with open(os.path.join(d, "backups", name), "wb") as fh:
                fh.write(b"x")
        out = inst.protected_backups()  # before the fix: ValueError out of every prune, delete and pre-start backup
        self.assertIn("20260931-120000-pre-restore.zip", out)
        self.assertNotIn("20200101-120000-pre-restore.zip", out)  # a real date past the 7 days still ages out



# ----- follow-up: install_local off the loop ---------------------------------------------------

class InstallLocalOffTheLoopTest(unittest.TestCase):
    def test_install_local_reads_restore_pending_in_the_executor_under_busy(self):
        inst = _start_installer(self)
        inst.rollback_restore_refusal = lambda: None
        busy = []

        def pending(cfg):
            busy.append(inst.busy)
            return _on_loop()  # "pending" when read on the loop, so the refusal below also says where it ran

        with mock.patch.object(backupkit, "pending", pending):
            res = asyncio.run(inst.install_local("demo"))
        self.assertFalse(res["ok"])
        self.assertEqual(busy, [True])  # before the fix: read on the loop, busy not held
        self.assertFalse(inst.busy)

    def test_a_pending_restore_still_refuses_install_local(self):
        inst = _start_installer(self)
        inst.rollback_restore_refusal = lambda: None
        with mock.patch.object(backupkit, "pending", return_value=True):
            res = asyncio.run(inst.install_local("demo"))
        self.assertIn("a restore is scheduled", res["error"])
        self.assertFalse(inst.busy)

    def test_install_local_refuses_while_busy_without_reading(self):
        inst = _start_installer(self)
        inst.busy = True
        with mock.patch.object(backupkit, "pending", mock.Mock(side_effect=AssertionError("read while busy"))):
            res = asyncio.run(inst.install_local("demo"))
        self.assertIn("another action is running", res["error"])
        self.assertTrue(inst.busy)  # someone else's flag, left alone


if __name__ == "__main__":
    unittest.main()
