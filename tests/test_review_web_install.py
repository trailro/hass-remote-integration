"""External review (web UI, install path): masking, login lockout keys, state.json names, requirement options,
version ordering, smoke verdicts, patch module loading, archive limits, the builder's commit, request headers."""

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import entrypoint
from jsonio import ha_vkey, tag_key, vkey
from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import build_views, installer as inst_mod, patches, preflight
from custom_components.integration_manager.diagnostics import DiagnosticsView, scrub
from custom_components.integration_manager.installer import Installer, State
from custom_components.integration_manager.logfiles_page import LogFileTailView
from custom_components.integration_manager.manage_views import ReleasePreviewView
from custom_components.integration_manager.views import InstallView

SECRET = "SYNTH3TICvalue9"


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-web-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def _request(headers=None, body=None, query=None):
    return SimpleNamespace(headers=headers or {}, query=query or {}, content_type="application/json",
                           json=mock.AsyncMock(return_value=body or {}))


def _body(resp):
    return json.loads(resp.body)


def _zip(members, manifest=None, top="owner-repo-abc/custom_components/demo/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(top + "manifest.json", json.dumps(manifest or {"domain": "demo", "version": "1.0"}))
        for m in members:
            if isinstance(m, zipfile.ZipInfo):
                zf.writestr(m, "../../../etc/passwd")
            else:
                zf.writestr(top + m[0], m[1])
    return buf.getvalue()


async def _agen(chunks):
    for c in chunks:
        yield c


class _Resp:
    status = 200

    def __init__(self, chunks, length=None):
        self.content_length = length
        self.content = SimpleNamespace(iter_chunked=lambda n: _agen(chunks))

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, resp):
        self.resp, self.urls = resp, []

    def get(self, url, **kw):
        self.urls.append(url)
        resp = self.resp

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _hass():
    async def job(fn, *args):
        return fn(*args)
    return SimpleNamespace(async_add_executor_job=job, is_running=True, loop=mock.Mock(), async_create_task=mock.Mock())


# ----- 11 -----------------------------------------------------------------------------------------

class SecretNamesTest(unittest.TestCase):
    NAMES = ("network_key", "s0_legacy_key", "s2_access_control_key", "s2_authenticated_key", "bindkey", "aes_key", "ssl_key",
             "otp", "passkey", "hmac", "webhook_id", "cloudhook_url", "pin_code", "Authorization")

    def test_dict_keys(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(scrub({name: SECRET})[name], "***")

    def test_text(self):
        for name in self.NAMES:
            for text in (f"{name}={SECRET}", f'"{name}": "{SECRET}"', f"{name}: {SECRET}"):
                with self.subTest(text=text):
                    self.assertNotIn(SECRET, scrub(text))

    def test_authorization_whole_value_and_unpadded_basic(self):
        self.assertNotIn(SECRET, scrub(f"Authorization: Digest {SECRET}"))
        self.assertNotIn("dXNlcjpwYXNz", scrub("header Basic dXNlcjpwYXNz sent"))
        self.assertEqual(scrub("Basic information"), "Basic information")
        self.assertEqual(scrub({"translation_key": "t", "use_ssl": True}), {"translation_key": "t", "use_ssl": True})


# ----- 12 -----------------------------------------------------------------------------------------

class LockoutKeyTest(unittest.TestCase):
    def test_normalised_keys(self):
        self.assertEqual(auth_mod.client_key("::ffff:10.0.0.5"), "10.0.0.5")
        self.assertEqual(auth_mod.client_key("2001:db8::1"), auth_mod.client_key("2001:db8::ffff:1"))
        self.assertEqual(auth_mod.client_key("2001:db8::1"), "2001:db8::/64")
        self.assertEqual(auth_mod.client_key("10.0.0.5"), "10.0.0.5")
        self.assertEqual(auth_mod.client_key(None), "unknown")

    def test_locked_address_survives_a_full_table(self):
        auth = auth_mod.Auth("pw")
        for _ in range(auth_mod.MAX_FAILURES):
            auth.failed("10.9.9.9")
        self.assertGreater(auth.locked_for("10.9.9.9"), 0)
        for i in range(1001):
            auth.failed(f"10.1.{i // 250}.{i % 250}")
        self.assertGreater(auth.locked_for("10.9.9.9"), 0)
        self.assertLessEqual(len(auth._failures), 1000)


# ----- 13 -----------------------------------------------------------------------------------------

class StateNamesTest(unittest.TestCase):
    def test_dotdot_entries_dropped(self):
        d = _tmp(self)
        os.makedirs(os.path.join(d, "integration_manager"))
        with open(os.path.join(d, "integration_manager", "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"domain": "..", "installed": {"..": {"versions": {"1.0": {}}}, "demo": {
                "versions": {"..": {}, "1.0": {}, "a/../b": {}}, "running_tag": "..", "previous_tag": "1.0"}}}, fh)
        with self.assertLogs("custom_components.integration_manager.installer", "WARNING"):
            inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=d)))
        self.assertEqual(list(inst.state.installed), ["demo"])
        self.assertEqual(list(inst.state.installed["demo"]["versions"]), ["1.0"])
        self.assertIsNone(inst.state.installed["demo"]["running_tag"])
        self.assertEqual(inst.state.installed["demo"]["previous_tag"], "1.0")
        self.assertIsNone(inst.state.domain)

    def test_rmtree_stays_inside(self):
        d = _tmp(self)
        base = os.path.join(d, "custom_components")
        os.makedirs(os.path.join(base, "demo"))
        with self.assertLogs("custom_components.integration_manager.installer", "WARNING"):
            inst_mod._rmtree_under(os.path.join(base, ".."), base)
        self.assertTrue(os.path.isdir(base))
        inst_mod._rmtree_under(os.path.join(base, "demo"), base)
        self.assertFalse(os.path.exists(os.path.join(base, "demo")))

    def test_install_view_checks_the_domain(self):
        installer = SimpleNamespace(install=mock.AsyncMock(return_value={"ok": True}))
        view = InstallView(installer)
        res = _body(asyncio.run(view.post(_request(body={"tag": "1.0", "domain": "../x"}))))
        self.assertEqual(res, {"ok": False, "error": "invalid domain"})
        installer.install.assert_not_awaited()


class RemoveDomainOrderTest(unittest.TestCase):
    def test_state_saved_before_trees_go(self):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(installed={"demo": {"versions": {}}})
        inst.hass = _hass()
        inst.dismiss_patch_notification = mock.Mock()
        inst._stays_loaded_until_restart = lambda dom: False
        inst._entries_of = lambda dom: []
        inst.on_domain_removed = None
        saved, order = [], []
        inst._save_state = lambda: saved.append("demo" in inst.state.installed)
        with mock.patch.object(inst_mod, "_rmtree_under", side_effect=lambda p, b: order.append((bool(saved), saved[-1] if saved else None))):
            asyncio.run(inst._remove_domain("demo"))
        self.assertEqual(len(order), 3)
        self.assertTrue(all(was_saved and not still_there for was_saved, still_there in order))


# ----- 14 -----------------------------------------------------------------------------------------

class RequirementOptionsTest(unittest.TestCase):
    def test_bad_requirement(self):
        for req in ("--index-url=http://evil.example/simple", "-e git+https://x/y", " --extra-index-url http://x", "not a requirement !!", 5):
            with self.subTest(req=req):
                self.assertIsNotNone(inst_mod.bad_requirement(req))
        for req in ("requests>=2.0", "pkg[extra]==1.0; python_version>'3.8'"):
            self.assertIsNone(inst_mod.bad_requirement(req))

    def test_pip_dry_run_refuses_options(self):
        with mock.patch.object(preflight, "_run_pip") as run:
            res = preflight._pip_dry_run(sys.executable, ["ok==1.0", "--index-url=http://evil.example"], None)
        run.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertIn("--index-url=http://evil.example", res["stderr"])

    def test_install_requirements_never_reaches_pip(self):
        inst = object.__new__(Installer)
        inst.constraints = ""
        with mock.patch.object(inst_mod.pkg_util, "install_package") as pip, mock.patch.object(inst_mod.pkg_util, "is_installed", return_value=True), \
                self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            self.assertEqual(inst._install_requirements(["-r /etc/x", "requests>=2"]), ["-r /etc/x"])
        pip.assert_not_called()

    def _start_installer(self, requirements, min_ha=None):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(installed={"demo": {"versions": {"2.0": {"requirements": requirements, "min_ha": min_ha}}, "running_tag": None}})
        inst.busy = False
        inst.hass = _hass()  # start() reads restore-pending.json in the executor
        os.makedirs(inst._version_dir("demo", "2.0"))
        return inst

    def test_start_refuses(self):
        res = asyncio.run(self._start_installer(["--extra-index-url http://evil.example"]).start("demo", "2.0"))
        self.assertFalse(res["ok"])
        self.assertIn("refused", res["error"])
        self.assertIn("--extra-index-url", res["error"])


class InstallCleanupTest(unittest.TestCase):
    def _installer(self, blob):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        os.makedirs(inst.versions_dir)
        inst.state = State()
        inst.busy = False
        inst.hass = _hass()
        inst.spec = lambda dom: {"repo": "owner/repo"}
        inst.settings = SimpleNamespace(github_headers=lambda: {})
        inst.updates, inst._releases_cache = {}, {}
        inst._save_state = lambda: None
        session = _Session(_Resp([blob]))
        return inst, session

    def test_refused_requirement_leaves_no_orphan(self):
        inst, session = self._installer(_zip([], {"domain": "demo", "version": "1.0", "requirements": ["--index-url=http://evil.example"]}))
        with mock.patch.object(inst_mod, "async_get_clientsession", return_value=session), \
                self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            res = asyncio.run(inst.install("1.0", domain="demo", archive_ref="0123abcd"))
        self.assertFalse(res["ok"])
        self.assertIn("--index-url", res["error"])
        self.assertTrue(session.urls[0].endswith("/zipball/0123abcd"))
        self.assertFalse(os.path.exists(os.path.join(inst.versions_dir, "demo")))
        self.assertEqual(inst.state.installed, {})


# ----- 15 -----------------------------------------------------------------------------------------

class VersionOrderTest(unittest.TestCase):
    def test_padding_and_stable_first(self):
        self.assertEqual(vkey("1.2.0"), vkey("v1.2"))
        self.assertFalse(vkey("1.2.0") > vkey("v1.2"))
        self.assertEqual(max(["1.0.0", "2.0.0b1", "main", "local"], key=tag_key), "1.0.0")
        self.assertEqual(max(["main", "local"], key=tag_key), "main")

    def test_ha_versions(self):
        self.assertLess(ha_vkey("2026.9.0b1"), ha_vkey("2026.9.0"))
        self.assertLessEqual(ha_vkey("2024.1"), ha_vkey("2024.1.0"))

    def test_entrypoint_venvs_and_python(self):
        with mock.patch.object(entrypoint.os, "listdir", return_value=["venv-2026.9.0", "venv-2026.9.0b2", "venv-2026.8.3", "venv-current"]), \
                mock.patch.object(entrypoint, "venv_ok", return_value=True):
            self.assertEqual(entrypoint.installed_versions(), ["2026.8.3", "2026.9.0b2", "2026.9.0"])
        major, minor = sys.version_info[:2]
        self.assertTrue(entrypoint._python_fits(f">={major}.{minor}"))
        self.assertTrue(entrypoint._python_fits(f"=={major}.{minor}"))
        self.assertFalse(entrypoint._python_fits(f">={major}.{minor + 1}"))

    def test_start_min_ha_uses_ha_order(self):
        t = RequirementOptionsTest()
        t.addCleanup = self.addCleanup
        with mock.patch("homeassistant.const.__version__", "2026.9.0b1"):
            res = asyncio.run(t._start_installer(["-x"], min_ha="2026.9.0").start("demo", "2.0"))
        self.assertIn("needs Home Assistant 2026.9.0", res["error"])
        with mock.patch("homeassistant.const.__version__", "2026.9.0"):
            res = asyncio.run(t._start_installer(["-x"], min_ha="2026.9.0b1").start("demo", "2.0"))
        self.assertIn("refused", res["error"])  # past the gate: a beta minimum is met by its release

    def test_start_without_tag_picks_stable(self):
        t = RequirementOptionsTest()
        t.addCleanup = self.addCleanup
        inst = t._start_installer(["-x"])
        inst.state.installed["demo"]["versions"] = {"1.0": {"requirements": ["-x"]}, "2.0b1": {}}
        os.makedirs(inst._version_dir("demo", "1.0"))
        self.assertIn("demo 1.0 is refused", asyncio.run(inst.start("demo"))["error"])

    def test_gate_target_prefers_stable(self):
        installer = SimpleNamespace(LOCAL_TAG="local", state=SimpleNamespace(installed={"probe": {"running_tag": None, "versions": {"v1.0.0": {}, "v2.0.0b1": {}}}}),
                                    spec=lambda dom: {"repo": "o/r"}, _version_dir=lambda dom, tag: f"/v/{dom}/{tag}")
        preflight._REPORTS.clear()
        run = mock.AsyncMock(return_value={"ok": True})
        with mock.patch.object(preflight, "run", run):
            asyncio.run(preflight.gate(None, installer, "probe", None))
        self.assertEqual(run.await_args.args[3], "v1.0.0")


# ----- 16 -----------------------------------------------------------------------------------------

class SmokeVerdictTest(unittest.TestCase):
    def _installer(self, entry_state, health_state):
        inst = object.__new__(Installer)
        inst.state = State(domain="demo", installed={"demo": {"versions": {"1.0": {}, "2.0": {}}, "running_tag": "2.0"}},
                           pending_smoke={"domain": "demo", "tag": "2.0", "can_rollback": True})
        inst.hass = _hass()
        inst.settings = SimpleNamespace(int_=lambda key, lo, hi: 300, bool_=lambda key: True)
        inst.busy = False
        inst._smoke_handle, inst._smoke_pending, inst._smoke_waiting, inst._smoke_rechecked = None, {"domain": "demo"}, {}, set()
        inst.entry = SimpleNamespace(state=SimpleNamespace(value=entry_state), disabled_by=None, title="Hub")
        inst._entries_of = lambda dom: [inst.entry]
        inst.health_source = lambda grace: {"state": health_state, "reason": "3 of 3 entities unavailable" if health_state == "degraded" else "r"}
        inst._save_state = lambda: None
        inst.rollback_full = mock.AsyncMock(return_value={"ok": True, "tag": "1.0", "restore": "b.zip"})
        inst.restart = mock.AsyncMock()
        inst.announce_smoke = mock.Mock()
        return inst

    def test_degraded_keeps_the_version(self):
        inst = self._installer("loaded", "degraded")
        asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.rollback_full.assert_not_awaited()
        inst.restart.assert_not_awaited()
        self.assertEqual((inst.state.last_smoke["state"], inst.state.last_smoke["action"]), ("degraded", "none"))
        self.assertIn("degraded", inst.state.last_error)
        self.assertIsNone(inst.state.pending_smoke)
        inst.announce_smoke.assert_called_once()

    def test_setup_retry_rechecks_then_rolls_back(self):
        inst = self._installer("setup_retry", "error")
        asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.rollback_full.assert_not_awaited()
        self.assertIsNone(inst.state.last_smoke)
        self.assertIsNotNone(inst.state.pending_smoke)
        self.assertEqual(inst.hass.loop.call_later.call_args.args[0], 300)
        asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.rollback_full.assert_awaited_once()
        inst.restart.assert_awaited_once()
        self.assertTrue(inst.state.last_smoke["action"].startswith("full rollback to 1.0"))

    def test_setup_retry_that_loads_is_not_rolled_back(self):
        inst = self._installer("setup_retry", "error")
        asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.entry.state.value = "loaded"
        inst.health_source = lambda grace: {"state": "ok", "reason": ""}
        inst.async_finish_change_report = mock.AsyncMock()
        inst._dismiss_smoke_notification = mock.Mock()
        inst._notify_yaml_imported = mock.Mock()
        asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.rollback_full.assert_not_awaited()
        self.assertEqual(inst.state.last_smoke["state"], "ok")

    def test_setup_error_rolls_back(self):
        inst = self._installer("setup_error", "error")
        asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.rollback_full.assert_awaited_once()
        inst.hass.loop.call_later.assert_not_called()

    def test_degraded_notification_text(self):
        inst = object.__new__(Installer)
        inst.hass = object()
        inst.state = State(last_smoke={"domain": "demo", "tag": "2.0", "at": "t", "state": "degraded", "reason": "no entity report", "action": "none"})
        inst._save_state = lambda: None
        with mock.patch("homeassistant.components.persistent_notification.async_create") as create:
            inst.announce_smoke()
        text = create.call_args.args[1]
        self.assertIn("degraded (no entity report)", text)
        self.assertIn("kept", text)
        self.assertEqual(create.call_args.kwargs["notification_id"], "hri_smoke_demo")


# ----- 17 + C8d -----------------------------------------------------------------------------------

PATCH_MODULE = """\
from __future__ import annotations
import time
from dataclasses import dataclass
time.sleep(0.02)


@dataclass
class Target:
    path: str


def status(ctx) -> str:
    return "pending"


def apply(ctx) -> str:
    return "applied"
"""


class PatchModulesTest(unittest.TestCase):
    def test_concurrent_loads(self):
        d = _tmp(self)
        path = os.path.join(d, "fix.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(PATCH_MODULE)
        errors, mods = [], []

        def load():
            try:
                mods.append(patches._load_module(path))
            except Exception as err:  # noqa: BLE001
                errors.append(err)
        threads = [threading.Thread(target=load) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({m.__name__ for m in mods}), 8)
        self.assertFalse(any(m.__name__ in sys.modules for m in mods))

    def test_py_status_cached_until_the_file_changes(self):
        d = _tmp(self)
        os.makedirs(patches.patch_dir(d, "demo"))
        path = os.path.join(patches.patch_dir(d, "demo"), "fix.py")  # patch_path falls back to the bundled copy while the file is missing
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(PATCH_MODULE)
        comp = os.path.join(d, "custom_components", "demo")
        os.makedirs(comp)
        patches._PY_STATUS.clear()
        with mock.patch.object(patches, "_load_module", wraps=patches._load_module) as load:
            for _ in range(3):
                self.assertEqual(patches.status(d, "demo", "", comp, "1.0")[0]["status"], "pending")
            self.assertEqual(load.call_count, 1)
            patches.status(d, "demo", "", comp, "2.0")
            self.assertEqual(load.call_count, 2)
            st = os.stat(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            patches.status(d, "demo", "", comp, "1.0")
            self.assertEqual(load.call_count, 3)
            patches.apply_all(d, "demo", "", comp, "1.0")
            patches.status(d, "demo", "", comp, "1.0")
            self.assertEqual(load.call_count, 5)


# ----- 18 -----------------------------------------------------------------------------------------

class ArchiveLimitsTest(unittest.TestCase):
    def unpack(self, blob):
        dest = os.path.join(_tmp(self), "out")
        return object.__new__(Installer)._unpack(blob, "demo", dest), dest

    def test_symlink_member_skipped(self):
        link = zipfile.ZipInfo("owner-repo-abc/custom_components/demo/link.py")
        link.external_attr = 0o120777 << 16
        with self.assertLogs("custom_components.integration_manager.installer", "WARNING") as logs:
            manifest, dest = self.unpack(_zip([("__init__.py", "x = 1\n"), link]))
        self.assertEqual(manifest["domain"], "demo")
        self.assertTrue(os.path.isfile(os.path.join(dest, "__init__.py")))
        self.assertFalse(os.path.lexists(os.path.join(dest, "link.py")))
        self.assertIn("link.py", "\n".join(logs.output))

    def test_too_many_members_or_bytes(self):
        with mock.patch.object(inst_mod, "UNPACK_MAX_MEMBERS", 3), self.assertRaisesRegex(RuntimeError, "members"):
            self.unpack(_zip([(f"m{i}.py", "") for i in range(4)]))
        with mock.patch.object(inst_mod, "UNPACK_MAX_BYTES", 1000), self.assertRaisesRegex(RuntimeError, "MB"):
            self.unpack(_zip([("big.py", "#" * 2000)]))

    def test_download_cap(self):
        with self.assertRaisesRegex(RuntimeError, "bytes"):
            asyncio.run(inst_mod.read_capped(_Resp([b"x"], length=200), "zip", limit=100))
        with self.assertRaisesRegex(RuntimeError, "allowed"):
            asyncio.run(inst_mod.read_capped(_Resp([b"x" * 60] * 3), "zip", limit=100))
        self.assertEqual(asyncio.run(inst_mod.read_capped(_Resp([b"ab", b"cd"], length=4), "zip", limit=100)), b"abcd")


# ----- 19 -----------------------------------------------------------------------------------------

class PrepareCommitTest(unittest.TestCase):
    def prepare(self, commit, ref="main"):
        view = object.__new__(build_views.BuildPrepareView)
        view.hass, view.publisher = None, None
        view.installer = SimpleNamespace(install=mock.AsyncMock(return_value={"ok": False, "error": "stop"}))
        view.updater = SimpleNamespace(status=mock.AsyncMock(return_value={"current": build_views.HA_VERSION}))
        view._check = SimpleNamespace(_resolve=mock.AsyncMock(return_value=("demo", ref, "", "owner/demo")), checked=lambda *a: True)
        with mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value=commit)):
            return _body(asyncio.run(view.post(_request(body={"domain": "demo", "ref": ref})))), view.installer.install

    def test_unresolved_branch_refused(self):
        res, install = self.prepare("")
        self.assertFalse(res["ok"])
        self.assertIn("commit", res["error"])
        install.assert_not_awaited()

    def test_downloads_the_checked_commit(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        _, install = self.prepare(sha)
        self.assertEqual(install.await_args.args, ("main",))
        self.assertEqual(install.await_args.kwargs["archive_ref"], sha)

    def test_sha_ref_without_answer(self):
        _, install = self.prepare("", ref="0123abcd")
        self.assertEqual(install.await_args.kwargs["archive_ref"], "0123abcd")


# ----- C4 / C8e / C9 ------------------------------------------------------------------------------

class HeaderRequiredTest(unittest.TestCase):
    def test_get_without_header(self):
        views = [ReleasePreviewView(SimpleNamespace(preview=mock.AsyncMock())), object.__new__(DiagnosticsView), object.__new__(LogFileTailView)]
        for view in views:
            with self.subTest(view=type(view).__name__):
                resp = asyncio.run(view.get(_request(query={"domain": "demo", "tag": "1.0"})))
                self.assertEqual(resp.status, 400)
                self.assertIn("X-Requested-With", _body(resp)["message"])


class ParentUrlUserinfoTest(unittest.TestCase):
    def test_userinfo_refused(self):
        from custom_components.integration_manager.manage_views import SettingsView

        view = SettingsView(SimpleNamespace(settings=SimpleNamespace(data={})))
        for url in ("http://user:pw@ha.lan:8123", "http://user@ha.lan"):
            res = _body(asyncio.run(view.post(_request(body={"parent_ha_url": url}))))
            self.assertFalse(res["ok"])
            self.assertIn("user@", res["error"])


class PreviewWithoutRepoTest(unittest.TestCase):
    def test_dev_mode_domain(self):
        inst = object.__new__(Installer)
        inst.spec = lambda dom: {"name": "demo", "local": True, "repo": ""}
        with self.assertRaisesRegex(ValueError, "dev-mode"):
            asyncio.run(inst.preview("demo", "local"))


if __name__ == "__main__":
    unittest.main()
