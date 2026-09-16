"""External review, round 4 (install path): overlapping full rollbacks, a start that fails after deploying, dev-mode
copies (links, caps, leftovers), diffs without context lines, and smaller cleanups."""

import asyncio
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import catalog, installer as inst_mod, manage_views, patches
from custom_components.integration_manager.installer import Installer, State
from tests.test_review_backup import _volume, _zip as _backup_zip


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-r4-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _yielding_hass(**extra):
    async def job(fn, *args):
        await asyncio.sleep(0)  # a real executor hands control back to the loop
        return fn(*args)
    return SimpleNamespace(async_add_executor_job=job, **extra)


# ----- HRI-02 -------------------------------------------------------------------------------------

class OverlappingFullRollbackTest(unittest.TestCase):
    def installer(self, start=None):
        cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        _backup_zip(cfg, "pre.zip", {"ha_version": "2026.8.3"})
        inst = Installer.__new__(Installer)
        inst.hass = _yielding_hass(config=SimpleNamespace(config_dir=cfg))
        inst.config_dir = cfg
        inst.busy = False
        inst.state = SimpleNamespace(domain="demo", installed={"demo": {"previous_tag": "v1", "pre_update_backup": "pre.zip", "versions": {"v1": {}}}},
                                     pending_change=None, pending_smoke=None, restart_required=False, last_action="", rollback_backup=None)
        inst._cancel_smoke = lambda: None
        inst._save_state = lambda: None
        started = []

        async def fake_start(domain, tag, own_restore=None):  # start()'s own gates, as in installer.start
            if inst.busy:
                return {"ok": False, "error": "another action is running"}
            archive = backupkit.pending_archive(cfg)
            if archive is not None and os.path.basename(archive) != own_restore:
                return {"ok": False, "error": "a restore is scheduled for the next restart"}
            inst.busy = True
            try:
                for _ in range(5):
                    await asyncio.sleep(0)
                started.append(own_restore)
                return {"ok": True}
            finally:
                inst.busy = False

        inst.start = start or fake_start
        return inst, cfg, started

    def test_two_rollbacks_never_leave_a_success_without_its_restore(self):
        inst, cfg, started = self.installer()

        async def both():
            return await asyncio.gather(inst.rollback_full(), inst.rollback_full())

        with mock.patch.object(inst_mod.events, "emit"):
            results = asyncio.run(both())
        oks = [r for r in results if r.get("ok")]
        self.assertEqual(len(oks), 1, results)
        self.assertTrue(backupkit.pending(cfg), results)  # the success is still applied at the restart
        self.assertEqual(os.path.basename(backupkit.pending_archive(cfg)), started[-1])
        refused = next(r for r in results if not r.get("ok"))
        self.assertIn("already", refused["error"])

    def test_a_failed_start_does_not_cancel_a_schedule_it_does_not_own(self):
        other = {}

        async def start(domain, tag, own_restore=None):
            # someone else's schedule replaced this rollback's while it started
            _backup_zip(inst.config_dir, "other.zip", {"ha_version": "2026.8.3"})
            other["zip"] = os.path.basename(backupkit.schedule_restore(inst.config_dir, "other.zip"))
            return {"ok": False, "error": "boom"}

        inst, cfg, _ = self.installer(start)
        res = asyncio.run(inst.rollback_full())
        self.assertFalse(res["ok"])
        self.assertTrue(backupkit.pending(cfg))
        self.assertEqual(os.path.basename(backupkit.pending_archive(cfg)), other["zip"])
        self.assertFalse(inst._rollback_running)


# ----- HRI-09 -------------------------------------------------------------------------------------

def _store(inst, domain, tag, text):
    _write(os.path.join(inst._version_dir(domain, tag), "manifest.json"), json.dumps({"domain": domain, "version": tag}))
    _write(os.path.join(inst._version_dir(domain, tag), "__init__.py"), text)


class StartExceptionAfterDeployTest(unittest.TestCase):
    def installer(self):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(domain="demo", installed={"demo": {"running_tag": "1.0", "versions": {
            "1.0": {"installed_at": "a", "version": "1.0"}, "2.0": {"installed_at": "b", "version": "2.0"}}}})
        _store(inst, "demo", "1.0", "old = 1\n")
        _store(inst, "demo", "2.0", "new = 1\n")
        inst._ensure_deployed("demo", "1.0")
        inst.busy = False
        inst.hass = _yielding_hass(config=SimpleNamespace(components=set()))
        inst.settings = SimpleNamespace(backup_keep=5)
        inst.async_backup = mock.AsyncMock(return_value={"name": "pre.zip"})
        inst.protected_backups = lambda: set()
        inst._save_state = lambda: None
        inst._requirements_for = mock.AsyncMock(side_effect=RuntimeError("metadata unreadable"))
        return inst

    def test_old_files_come_back(self):
        inst = self.installer()
        with mock.patch.object(backupkit, "prune"), mock.patch.object(inst_mod.events, "emit"), \
                self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = asyncio.run(inst.start("demo", "2.0"))
        self.assertFalse(res["ok"])
        self.assertEqual(inst.state.installed["demo"]["running_tag"], "1.0")
        self.assertEqual(_read(os.path.join(inst._component_dir("demo"), "__init__.py")), "old = 1\n")
        self.assertFalse(inst._ensure_deployed("demo", "1.0"))  # the marker names the old tag again

    def test_restart_required_when_the_old_files_cannot_come_back(self):
        inst = self.installer()
        real = inst._ensure_deployed

        def ensure(domain, tag, force=False):
            if tag == "1.0":
                raise OSError("disk full")
            return real(domain, tag, force)

        inst._ensure_deployed = ensure
        with mock.patch.object(backupkit, "prune"), mock.patch.object(inst_mod.events, "emit"), \
                self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = asyncio.run(inst.start("demo", "2.0"))
        self.assertFalse(res["ok"])
        self.assertTrue(inst.state.restart_required)


# ----- HRI-10 + double copy -----------------------------------------------------------------------

class DevInstallTest(unittest.TestCase):
    def installer(self, running=False):
        d = _tmp(self)
        dev = os.path.join(d, "dev")
        src = os.path.join(dev, "custom_components", "demo")
        _write(os.path.join(src, "manifest.json"), json.dumps({"domain": "demo", "version": "0.1"}))
        _write(os.path.join(src, "__init__.py"), "x = 1\n")
        _write(os.path.join(src, "sub", "a.py"), "a = 1\n")
        _write(os.path.join(d, "outside", "secrets.yaml"), "password: hunter2\n")
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.user_registry_file = os.path.join(inst.state_dir, "registry.json")
        inst.state = State()
        inst.busy = False
        inst.hass = _yielding_hass()
        inst.settings = SimpleNamespace(dev_source_dir=dev)
        inst._save_state = lambda: None
        inst._replace_current = mock.AsyncMock(return_value={})
        inst._requirements_for = mock.AsyncMock(return_value=[])
        if running:
            inst.add_to_registry("demo", "", local=True)
            inst.state = State(domain="demo", installed={"demo": {"running_tag": "local", "versions": {"local": {"installed_at": "old"}}}})
            _store(inst, "demo", "local", "x = 0\n")
            inst._ensure_deployed("demo", "local")
        return inst, src

    def install(self, inst):
        with mock.patch.object(inst_mod.events, "emit"):
            return asyncio.run(inst.install_local("demo"))

    def test_links_are_skipped(self):
        inst, src = self.installer()
        os.symlink(".", os.path.join(src, "loop"))
        os.symlink(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(src))), "outside", "secrets.yaml"), os.path.join(src, "secrets.yaml"))
        with self.assertLogs("custom_components.integration_manager.installer", "WARNING") as logs:
            res = self.install(inst)
        self.assertTrue(res["ok"], res)
        dest = inst._version_dir("demo", "local")
        self.assertEqual(sorted(os.listdir(dest)), ["__init__.py", "manifest.json", "sub"])
        self.assertIn("secrets.yaml", "\n".join(logs.output))

    def test_caps(self):
        inst, _ = self.installer()
        with mock.patch.object(inst_mod, "UNPACK_MAX_MEMBERS", 2), self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = self.install(inst)
        self.assertFalse(res["ok"])
        self.assertIn("files", res["error"])
        self.assertFalse(os.path.lexists(os.path.join(inst.versions_dir, "demo")))
        inst, _ = self.installer()
        with mock.patch.object(inst_mod, "UNPACK_MAX_BYTES", 10), self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = self.install(inst)
        self.assertFalse(res["ok"])
        self.assertIn("MB", res["error"])

    def test_failure_after_the_copy_leaves_nothing(self):
        inst, _ = self.installer()
        inst._replace_current = mock.AsyncMock(side_effect=RuntimeError("backup failed"))
        with self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = self.install(inst)
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.lexists(os.path.join(inst.versions_dir, "demo")))
        self.assertNotIn("demo", inst.registry())

    def test_failed_reinstall_keeps_the_recorded_copy(self):
        inst, _ = self.installer(running=True)
        inst._replace_current = mock.AsyncMock(side_effect=RuntimeError("backup failed"))
        with self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = self.install(inst)
        self.assertFalse(res["ok"])
        self.assertEqual(_read(os.path.join(inst._version_dir("demo", "local"), "__init__.py")), "x = 0\n")
        self.assertEqual([n for n in os.listdir(os.path.join(inst.versions_dir, "demo")) if n.startswith(".")], [])
        self.assertIn("demo", inst.registry())

    def test_running_copy_refreshed_with_one_copy(self):
        inst, _ = self.installer(running=True)
        with mock.patch.object(inst, "_deploy", wraps=inst._deploy) as deploy:
            res = self.install(inst)
        self.assertTrue(res["ok"], res)
        self.assertEqual(deploy.call_count, 1)
        self.assertEqual(_read(os.path.join(inst._component_dir("demo"), "__init__.py")), "x = 1\n")
        self.assertEqual([n for n in os.listdir(os.path.join(inst.versions_dir, "demo")) if n.startswith(".")], [])


class DeployTest(unittest.TestCase):
    def test_failed_swap_keeps_the_deployed_files(self):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(installed={"demo": {"versions": {"1.0": {}, "2.0": {}}}})
        _store(inst, "demo", "1.0", "old = 1\n")
        _store(inst, "demo", "2.0", "new = 1\n")
        inst._deploy("demo", "1.0")
        real = os.replace

        def replace(src, dst):
            if src.endswith(".deploying"):
                raise OSError("no space")
            return real(src, dst)

        with mock.patch.object(inst_mod.os, "replace", side_effect=replace), self.assertRaises(OSError):
            inst._deploy("demo", "2.0")
        self.assertEqual(_read(os.path.join(inst._component_dir("demo"), "__init__.py")), "old = 1\n")
        self.assertEqual(sorted(os.listdir(os.path.join(d, "custom_components"))), ["demo"])


# ----- zip escape ---------------------------------------------------------------------------------

class UnpackEscapeTest(unittest.TestCase):
    def test_member_escaping_the_component_dir_is_refused(self):
        d = _tmp(self)
        dest = os.path.join(d, "store", "out")
        buf = io.BytesIO()
        top = "owner-repo-abc/custom_components/demo/"
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(top + "manifest.json", json.dumps({"domain": "demo", "version": "1.0"}))
            zf.writestr(top + "../../../evil.py", "boom = 1\n")
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            object.__new__(Installer)._unpack(buf.getvalue(), "demo", dest)
        self.assertFalse(os.path.lexists(dest))
        self.assertFalse(os.path.lexists(os.path.join(d, "store", "evil.py")))
        self.assertFalse(os.path.lexists(os.path.join(d, "evil.py")))


# ----- HRI-11 + prefix ----------------------------------------------------------------------------

class DiffWithoutContextTest(unittest.TestCase):
    def setUp(self):
        self.root = _tmp(self)
        self.comp = os.path.join(self.root, "custom_components", "demo")
        _write(os.path.join(self.comp, "mod.py"), "a = 1\nb = 2\nc = 4\n")
        self.ctx = patches.PatchContext(self.root, "demo", os.path.join(self.root, "site"), self.comp)

    def test_insertion_and_deletion_without_context_are_refused(self):
        for text in ("--- a/mod.py\n+++ b/mod.py\n@@ -1,0 +2 @@\n+x = 9\n",
                     "--- a/mod.py\n+++ b/mod.py\n@@ -2 +1,0 @@\n-b = 2\n"):
            self.assertIn("context", patches.validate("z.patch", text) or "")
            self.assertIn("context", self.check(text))
        self.assertEqual(_read(os.path.join(self.comp, "mod.py")), "a = 1\nb = 2\nc = 4\n")

    def test_a_replacement_without_context_still_works(self):
        text = "--- a/mod.py\n+++ b/mod.py\n@@ -2 +2 @@\n-b = 2\n+b = 3\n"
        self.assertIsNone(patches.validate("z.patch", text))
        self.assertEqual(patches._diff_apply(text, self.ctx), "applied")
        self.assertEqual(patches._diff_status(text, self.ctx), "applied")

    def check(self, text):
        return patches.check(self.root, "demo", self.ctx.site_packages, self.comp, None, "z.patch", text).get("error", "")

    def test_only_one_git_prefix_is_stripped(self):
        _write(os.path.join(self.comp, "a", "x.py"), "nested\n")
        _write(os.path.join(self.comp, "x.py"), "top\n")
        self.assertEqual(patches._resolve("b/a/x.py", self.ctx), os.path.join(self.comp, "a", "x.py"))
        self.assertEqual(patches._resolve("a/x.py", self.ctx), os.path.join(self.comp, "x.py"))


# ----- catalog cap --------------------------------------------------------------------------------

async def _agen(chunks):
    for c in chunks:
        yield c


class CatalogCapTest(unittest.TestCase):
    def rows(self, length, chunks):
        d = _tmp(self)
        cat = object.__new__(catalog.Catalog)
        cat.hass = _yielding_hass()
        cat.path, cat.error, cat.fetched_at, cat._rows, cat._at, cat._lock = os.path.join(d, "c.json"), "", None, None, 0.0, asyncio.Lock()
        resp = SimpleNamespace(content_length=length, raise_for_status=lambda: None, read=mock.AsyncMock(return_value=b"".join(chunks)),
                               content=SimpleNamespace(iter_chunked=lambda n: _agen(chunks)))

        class Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *a):
                return False
        session = SimpleNamespace(get=lambda *a, **kw: Ctx())
        with mock.patch.object(catalog, "async_get_clientsession", return_value=session), \
                self.assertLogs("custom_components.integration_manager.catalog", "WARNING"):
            return asyncio.run(cat.rows()), cat

    def test_declared_size_refused(self):
        rows, cat = self.rows(10 ** 12, [b"{}"])
        self.assertEqual(rows, [])
        self.assertIn("MB", cat.error)

    def test_streamed_size_refused(self):
        with mock.patch.object(catalog, "MAX_BYTES", 4):
            rows, cat = self.rows(None, [b"{", b"    ", b"}"])
        self.assertIn("MB", cat.error)


# ----- manage_views: file operations off the loop -------------------------------------------------

class PatchFilesOffTheLoopTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _tmp(self)
        self.inside = False
        self.on_loop = []

        async def job(fn, *args):
            self.inside = True
            try:
                return fn(*args)
            finally:
                self.inside = False
        self.hass = SimpleNamespace(async_add_executor_job=job, config=SimpleNamespace(config_dir=self.cfg, components=set()))
        real_isfile, real_remove, real_makedirs = os.path.isfile, os.remove, os.makedirs

        def watch(real):
            def wrapper(*a, **kw):
                if not self.inside:
                    self.on_loop.append(real.__name__)
                return real(*a, **kw)
            return wrapper
        for name, real in (("isfile", real_isfile),):
            p = mock.patch.object(manage_views.os.path, name, side_effect=watch(real))
            p.start()
            self.addCleanup(p.stop)
        for name, real in (("remove", real_remove), ("makedirs", real_makedirs)):
            p = mock.patch.object(manage_views.os, name, side_effect=watch(real))
            p.start()
            self.addCleanup(p.stop)

    def test_upload(self):
        chunks = [b"def apply(ctx):\n    return 'applied'\n", b"def status(ctx):\n    return 'applied'\n", b""]

        async def read_chunk(_n):
            return chunks.pop(0)
        field = SimpleNamespace(name="file", filename="fix.py", read_chunk=read_chunk)

        async def multipart():
            async def nxt():
                return field
            return SimpleNamespace(next=nxt)
        view = manage_views.PatchUploadView(self.hass, SimpleNamespace())
        res = json.loads(asyncio.run(view.post(SimpleNamespace(headers={"X-Requested-With": "fetch"}, multipart=multipart), "demo")).body)
        self.assertTrue(res["ok"], res)
        self.assertIn("def status", _read(os.path.join(patches.patch_dir(self.cfg, "demo"), "fix.py")))
        self.assertEqual(self.on_loop, [])

    def test_save_create_and_delete(self):
        _write(os.path.join(patches.patch_dir(self.cfg, "demo"), "fix.patch"), "x")
        self.on_loop.clear()  # the fixture itself
        edit = manage_views.PatchEditView(self.hass, SimpleNamespace())
        body = {"name": "fix.patch", "text": "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a\n+b\n", "create": True}
        req = SimpleNamespace(headers={"X-Requested-With": "fetch"}, query={}, content_type="application/json", json=mock.AsyncMock(return_value=body))
        res = json.loads(asyncio.run(edit.post(req, "demo", "save")).body)
        self.assertFalse(res["ok"])
        self.assertIn("exists already", res["error"])
        action = manage_views.PatchActionView(self.hass, SimpleNamespace(running=None, dismiss_patch_notification=mock.Mock()))
        req = SimpleNamespace(headers={}, query={}, content_type="application/json", json=mock.AsyncMock(return_value={}))
        res = json.loads(asyncio.run(action.post(req, "demo", "fix.patch", "delete")).body)
        self.assertTrue(res["ok"], res)
        self.assertFalse(os.path.exists(os.path.join(patches.patch_dir(self.cfg, "demo"), "fix.patch")))
        self.assertEqual(self.on_loop, [])


if __name__ == "__main__":
    unittest.main()
