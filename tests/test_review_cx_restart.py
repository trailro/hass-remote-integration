"""External review, round CX, F1: reinstalling a running mutable reference (``main``, a branch, a local dev
build) answered "no restart required" and smoke-tested the code the process still had imported.

install() force-deploys the replacement itself, so start()'s ``_ensure_deployed`` has nothing left to do
(``deployed`` is False) and the tag did not change (``loaded == tag``).  The third clause of ``needs_restart``
was therefore False although the new files on disk differ from the code this process imported: the answer said
no restart, the loaded-code bookkeeping advanced to the new hash, and the smoke test ran against the old code.

The distinguishing fact is the content: the code on disk differs from the code we know this process imported.
The adoption path (boot, or an integration Home Assistant set up before the manager knew about it) has no such
knowledge - ``_code_hash`` has no entry - and must keep asking for no restart."""

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

from custom_components.integration_manager import build_views, installer as inst_mod
from custom_components.integration_manager.installer import Installer, State


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _zipball(text, version="1.0", top="owner-repo-abc/custom_components/demo/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(top + "manifest.json", json.dumps({"domain": "demo", "version": version}))
        zf.writestr(top + "__init__.py", text)
    return buf.getvalue()


class _Resp:
    status = 200

    def __init__(self, blob):
        self.content_length = None

        async def chunks(_n):
            yield blob

        self.content = SimpleNamespace(iter_chunked=chunks)

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, blob):
        self.blob = blob

    def get(self, url, **kw):
        resp = _Resp(self.blob)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *a):
                return False

        return _Ctx()


class ReinstalledRunningReferenceTest(unittest.TestCase):
    """main is loaded, main gets new code, main is prepared and started again."""

    def setUp(self):
        cfg = self.cfg = tempfile.mkdtemp(prefix="hri-cx1-")
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        for patch in (mock.patch.object(inst_mod.events, "emit"),
                      mock.patch.object(inst_mod.change_report, "snapshot", lambda hass, domain: {"entities": {}, "services": []})):
            patch.start()
            self.addCleanup(patch.stop)

        async def job(fn, *args):
            await asyncio.sleep(0)
            return fn(*args)

        inst = self.inst = object.__new__(Installer)
        inst.hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components={"demo"}), async_add_executor_job=job,
                                    is_running=True, loop=mock.Mock(), async_create_task=mock.Mock())
        inst.config_dir, inst.state_dir = cfg, os.path.join(cfg, "integration_manager")
        inst.state_file = os.path.join(inst.state_dir, "state.json")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(domain="demo", installed={"demo": {
            "versions": {"main": {"installed_at": "first", "version": "1.0", "requirements": []}}, "running_tag": "main"}})
        _write(os.path.join(inst._version_dir("demo", "main"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._version_dir("demo", "main"), "__init__.py"), "code = 'A'\n")
        inst.busy = False
        inst.settings = SimpleNamespace(backup_keep=50, int_=lambda key, lo=0, hi=0: 300, bool_=lambda key: False,
                                        github_headers=lambda: {})
        inst.spec = lambda dom: {"repo": "owner/repo"}
        inst.updates, inst._releases_cache = {}, {}
        inst._smoke_handle = inst._smoke_pending = None
        inst._smoke_waiting, inst._smoke_rechecked = {}, set()
        inst._abandoned_switch, inst._restart_before_uninstall = {}, {}
        inst._rollback_undo = None
        inst._requirements_for = mock.AsyncMock(return_value=[])
        inst._install_requirements = lambda reqs, force=False: []
        inst._apply_patches = lambda domain: "n/a"
        inst._loadable = mock.AsyncMock(return_value=True)
        inst._enable_entries = mock.AsyncMock(return_value=[])
        inst._entries_of = lambda domain: []
        inst.async_backup = mock.AsyncMock(return_value={"name": "pre.zip"})
        inst.protected_backups = lambda: set()
        inst._ensure_deployed("demo", "main")
        # code A is what this process imported and runs
        self.hash_a = inst._tree_hash("demo")
        inst._loaded_tags, inst._code_hash = {"demo": "main"}, {"demo": self.hash_a}
        inst._save_state()

    def deployed_code(self):
        return _read(os.path.join(self.inst._component_dir("demo"), "__init__.py"))

    def install(self, text):
        with mock.patch.object(inst_mod, "async_get_clientsession", return_value=_Session(_zipball(text))):
            return asyncio.run(self.inst.install("main", domain="demo"))

    def start(self):
        with mock.patch.object(self.inst, "_schedule_smoke", wraps=self.inst._schedule_smoke) as smoke:
            res = asyncio.run(self.inst.start("demo", "main"))
        return res, smoke

    def test_new_code_under_the_loaded_reference_needs_a_restart(self):
        res = self.install("code = 'B'\n")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["redeployed"], "install refreshed the running copy itself, so start() finds nothing to deploy")
        self.assertEqual(self.deployed_code(), "code = 'B'\n")

        res, smoke = self.start()
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["restart_required"], "the process still has code A imported; B only runs after a restart")
        self.assertTrue(self.inst.state.restart_required)
        self.assertIn("restart required", self.inst.state.last_action)

    def test_the_loaded_hash_stays_on_the_old_code_until_the_restart(self):
        self.install("code = 'B'\n")
        self.start()
        self.assertEqual(self.inst._code_hash["demo"], self.hash_a, "the new code is not running yet: do not record it as loaded")
        self.assertNotEqual(self.inst._tree_hash("demo"), self.hash_a)
        self.inst._enable_entries.assert_not_awaited()  # entries stay as they are; the next boot enables them

    def test_the_smoke_test_waits_for_the_restart(self):
        self.install("code = 'B'\n")
        res, smoke = self.start()
        smoke.assert_not_called()  # a verdict now would test code A
        self.assertIsNone(self.inst._smoke_pending)
        self.assertEqual(self.inst.state.pending_smoke, {"domain": "demo", "tag": "main", "can_rollback": False})
        self.assertEqual(res["smoke_test"], {"domain": "demo", "tag": "main", "can_rollback": False})

    def test_the_same_code_deployed_again_is_still_free(self):
        # an uninstall + install of the loaded tag, or a Prepare of a reference that did not move: byte for byte
        # the same code, so nothing has to restart and the smoke test can run now
        res = self.install("code = 'A'\n")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.inst._tree_hash("demo"), self.hash_a)
        res, smoke = self.start()
        self.assertFalse(res["restart_required"], res)
        smoke.assert_called_once()  # the deployed code is what runs: the verdict is worth having now
        self.assertEqual(self.inst._code_hash["demo"], self.hash_a)

    def test_an_adopted_integration_does_not_demand_a_restart(self):
        # boot, or an integration Home Assistant set up before the manager knew about it: nothing was recorded
        # about what this process imported, and the deployed files are the tag's own
        self.inst._loaded_tags, self.inst._code_hash = {}, {}
        res, smoke = self.start()
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["restart_required"], "the deployed files are what is running; a restart would be pure noise")
        self.assertFalse(self.inst.state.restart_required)
        self.assertEqual(self.inst._code_hash["demo"], self.hash_a)
        self.assertEqual(self.inst._loaded_tags["demo"], "main")


class PrepareAnswersWithTheRestartTest(unittest.TestCase):
    """The builder's answer drives the restart button on the page."""

    def view(self, install_result, start_result=None):
        view = object.__new__(build_views.BuildPrepareView)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir="/tmp"))
        view.hass, view.publisher = hass, None
        view.installer = SimpleNamespace(hass=hass, busy=False, install=mock.AsyncMock(return_value=install_result),
                                         start=mock.AsyncMock(return_value=start_result or {"ok": True, "restart_required": False}),
                                         state=SimpleNamespace(pending_start=None), _save_state=lambda: None)
        running = build_views.HA_VERSION
        view.updater = SimpleNamespace(status=mock.AsyncMock(return_value={"current": running, "pending": False}))
        view._check = SimpleNamespace(_resolve=mock.AsyncMock(return_value=("demo", "main", running, "owner/demo")), checked=lambda *a: True)
        view.json = lambda d: d
        return view

    def post(self, view, body):
        with mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value="0" * 40)), \
                mock.patch.object(build_views.events, "emit"):
            return asyncio.run(build_views.BuildPrepareView.post.__wrapped__(view, None, {"domain": "demo", "ref": "main", **body}))

    def test_a_prepare_that_refreshed_the_running_copy_says_so(self):
        view = self.view({"ok": True, "domain": "demo", "tag": "main", "redeployed": True})
        res = self.post(view, {})
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["restart_required"], "the running copy was refreshed under the process: the page must offer the restart")

    def test_a_prepare_that_deployed_nothing_running_asks_for_nothing(self):
        view = self.view({"ok": True, "domain": "demo", "tag": "main", "redeployed": False})
        self.assertFalse(self.post(view, {})["restart_required"])

    def test_a_started_prepare_keeps_the_restart_the_start_asked_for(self):
        view = self.view({"ok": True, "domain": "demo", "tag": "main", "redeployed": True},
                         {"ok": True, "restart_required": True})
        self.assertTrue(self.post(view, {"start": True})["restart_required"])


if __name__ == "__main__":
    unittest.main()
