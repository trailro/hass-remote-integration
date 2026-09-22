"""External review, round 3 (install path): the builder's commit, the start gate on the stored copy, a failed
reinstall, state.json field types, direct-URL requirements, parser limits, unconfigured smoke verdicts,
scratch leftovers, anchored names, remove_version order."""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
import io
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import build_views, installer as inst_mod, manage_views, preflight
from custom_components.integration_manager.installer import Installer, State


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-r3-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def _request(body):
    return SimpleNamespace(headers={}, query={}, content_type="application/json", json=mock.AsyncMock(return_value=body))


def _body(resp):
    return json.loads(resp.body)


def _hass(calls=None):
    async def job(fn, *args):
        if calls is not None:
            calls.append(getattr(fn, "__name__", repr(fn)))
        return fn(*args)
    return SimpleNamespace(async_add_executor_job=job, is_running=True, loop=mock.Mock(), async_create_task=mock.Mock())


def _zip(files, manifest=None, top="owner-repo-abc/custom_components/demo/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(top + "manifest.json", json.dumps(manifest or {"domain": "demo", "version": "1.0"}))
        for name, text in files.items():
            zf.writestr(top + name, text)
    return buf.getvalue()


async def _agen(chunks):
    for c in chunks:
        yield c


class _Resp:
    status = 200

    def __init__(self, blob):
        self.content_length = None
        self.content = SimpleNamespace(iter_chunked=lambda n: _agen([blob]))

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, blob):
        self.blob, self.urls = blob, []

    def get(self, url, **kw):
        self.urls.append(url)
        resp = _Resp(self.blob)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ----- 1 ------------------------------------------------------------------------------------------

class CheckResolvesTheCommitFirstTest(unittest.TestCase):
    A, B = "a" * 40, "b" * 40

    def test_a_branch_that_moves_during_check_is_not_prepared(self):
        installer = SimpleNamespace(installed_domain="demo", install=mock.AsyncMock(return_value={"ok": True}))
        check = object.__new__(build_views.BuildCheckView)
        check.hass, check.installer, check.updater = None, installer, None
        check._pf, check._checks = SimpleNamespace(_lock=asyncio.Lock()), {}
        check._resolve = mock.AsyncMock(return_value=("demo", "main", "", "owner/demo"))
        run = mock.AsyncMock(return_value={"ok": True, "blockers": [], "warnings": []})
        # the branch points at A until the download happened, at B afterwards
        commit_of = mock.AsyncMock(side_effect=lambda *a: self.B if run.await_count else self.A)
        with mock.patch.object(preflight, "run", run), mock.patch.object(build_views, "_commit_of", commit_of):
            res = _body(asyncio.run(check.post(_request({"domain": "demo", "ref": "main"}))))
        self.assertTrue(res["ok"])
        self.assertEqual(run.await_args.kwargs.get("archive_ref"), self.A)  # downloaded by the commit the check id names

        prepare = object.__new__(build_views.BuildPrepareView)
        prepare.hass, prepare.installer, prepare.publisher, prepare._check = None, installer, None, check
        prepare.updater = SimpleNamespace(status=mock.AsyncMock(return_value={"current": build_views.HA_VERSION}))
        with mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value=self.B)):
            res = _body(asyncio.run(prepare.post(_request({"domain": "demo", "ref": "main", "check_id": res["check_id"]}))))
        self.assertFalse(res["ok"])
        installer.install.assert_not_awaited()


# ----- 2 ------------------------------------------------------------------------------------------

def _preflight_installer(test, stored_files, running="1.0", target="2.0"):
    d = _tmp(test)
    inst = object.__new__(Installer)
    inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
    inst.versions_dir = os.path.join(inst.state_dir, "versions")
    inst.constraints = ""
    inst._req_versions_cache = {}
    inst.state = State(domain="demo", installed={"demo": {"running_tag": running, "versions": {
        running: {}, target: {"installed_at": "2026-09-01T10:00:00", "min_ha": None}}}})
    inst.spec = lambda dom: {"repo": "owner/repo"}
    inst.settings = SimpleNamespace(github_headers=lambda: {})
    inst.installed_manifest = lambda dom=None: {"version": running}
    inst._entries_of = lambda dom: []
    inst.site_packages_for = lambda dom: d
    stored = inst._version_dir("demo", target)
    _write(os.path.join(stored, "manifest.json"), json.dumps({"domain": "demo", "version": target}))
    for name, text in stored_files.items():
        _write(os.path.join(stored, name), text)
    return inst


class GateChecksTheStoredCopyTest(unittest.TestCase):
    def _gate(self, inst, calls=None):
        preflight._REPORTS.clear()
        session = _Session(_zip({"__init__.py": "x = 1\n"}, {"domain": "demo", "version": "2.0"}))  # GitHub's ref today: clean
        with mock.patch.object(preflight, "async_get_clientsession", return_value=session), \
                mock.patch.object(preflight, "_pip_dry_run", return_value={"ok": True, "install": [], "stderr": ""}):
            return asyncio.run(preflight.gate(_hass(calls), inst, "demo", "2.0")), session

    def test_the_verdict_is_about_the_files_start_deploys(self):
        inst = _preflight_installer(self, {"__init__.py": "print 'py2'\n"})
        calls = []
        res, session = self._gate(inst, calls)
        self.assertTrue(res["blocked"])
        self.assertIn("does not compile", "; ".join(res["report"]["blockers"]))
        self.assertEqual(session.urls, [])  # nothing downloaded
        self.assertTrue(os.path.isfile(os.path.join(inst._version_dir("demo", "2.0"), "__init__.py")))  # the store is untouched
        self.assertIn("_requirement_versions", calls)  # importlib.metadata off the loop (C8)

    def test_a_reinstalled_copy_is_checked_again(self):
        inst = _preflight_installer(self, {"__init__.py": "x = 1\n"})
        preflight._REPORTS.clear()
        run = mock.AsyncMock(return_value={"ok": True})
        with mock.patch.object(preflight, "run", run):
            asyncio.run(preflight.gate(None, inst, "demo", "2.0"))
            asyncio.run(preflight.gate(None, inst, "demo", "2.0"))
            self.assertEqual(run.await_count, 1)
            inst.state.installed["demo"]["versions"]["2.0"]["installed_at"] = "2026-09-02T10:00:00"
            asyncio.run(preflight.gate(None, inst, "demo", "2.0"))
        self.assertEqual(run.await_count, 2)
        self.assertEqual(run.await_args.kwargs["source_dir"], inst._version_dir("demo", "2.0"))

    def test_reinstalled_while_the_check_ran(self):
        inst = _preflight_installer(self, {"__init__.py": "x = 1\n"})
        preflight._REPORTS.clear()

        async def run(*a, **kw):
            inst.state.installed["demo"]["versions"]["2.0"]["installed_at"] = "later"
            return {"ok": True, "blockers": []}
        with mock.patch.object(preflight, "run", run):
            res = asyncio.run(preflight.gate(None, inst, "demo", "2.0"))
        self.assertTrue(res["blocked"])
        self.assertEqual(preflight._REPORTS, {})


# ----- 3 ------------------------------------------------------------------------------------------

class ReinstallKeepsTheCheckedFilesTest(unittest.TestCase):
    def _installer(self, blob):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(installed={"demo": {"versions": {"1.0": {"installed_at": "old", "version": "1.0", "requirements": []}}, "running_tag": None}})
        _write(os.path.join(inst._version_dir("demo", "1.0"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._version_dir("demo", "1.0"), "__init__.py"), "old = 1\n")
        inst.busy = False
        inst.hass = _hass()
        inst.spec = lambda dom: {"repo": "owner/repo"}
        inst.settings = SimpleNamespace(github_headers=lambda: {})
        inst.updates, inst._releases_cache = {}, {}
        inst._save_state = lambda: None
        return inst, _Session(blob)

    def _install(self, inst, session):
        with mock.patch.object(inst_mod, "async_get_clientsession", return_value=session), \
                self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            return asyncio.run(inst.install("1.0", domain="demo"))

    def _leftovers(self, inst):
        return [n for n in os.listdir(os.path.join(inst.versions_dir, "demo")) if n.startswith(".")]

    def test_refused_requirement(self):
        inst, session = self._installer(_zip({"__init__.py": "new = 1\n"}, {"domain": "demo", "version": "1.1", "requirements": ["--index-url=http://x"]}))
        self.assertFalse(self._install(inst, session)["ok"])
        self.assertEqual(_read(os.path.join(inst._version_dir("demo", "1.0"), "__init__.py")), "old = 1\n")
        self.assertEqual(self._leftovers(inst), [])

    def test_failure_after_the_swap(self):
        inst, session = self._installer(_zip({"__init__.py": "new = 1\n"}, {"domain": "demo", "version": "1.1"}))
        inst._replace_current = mock.AsyncMock(side_effect=RuntimeError("backup failed"))
        self.assertFalse(self._install(inst, session)["ok"])
        self.assertEqual(_read(os.path.join(inst._version_dir("demo", "1.0"), "__init__.py")), "old = 1\n")
        self.assertEqual(inst.state.installed["demo"]["versions"]["1.0"]["installed_at"], "old")
        self.assertEqual(self._leftovers(inst), [])

    def test_success_replaces_and_leaves_nothing_aside(self):
        inst, session = self._installer(_zip({"__init__.py": "new = 1\n"}, {"domain": "demo", "version": "1.1"}))
        with mock.patch.object(inst_mod, "async_get_clientsession", return_value=session):
            self.assertTrue(asyncio.run(inst.install("1.0", domain="demo"))["ok"])
        self.assertEqual(_read(os.path.join(inst._version_dir("demo", "1.0"), "__init__.py")), "new = 1\n")
        self.assertEqual(inst.state.installed["demo"]["versions"]["1.0"]["version"], "1.1")
        self.assertEqual(self._leftovers(inst), [])


# ----- 4 ------------------------------------------------------------------------------------------

class StateFieldTypesTest(unittest.TestCase):
    def installer(self, state):
        d = _tmp(self)
        os.makedirs(os.path.join(d, "integration_manager"))
        _write(os.path.join(d, "integration_manager", "state.json"), json.dumps(state))
        with self.assertLogs("custom_components.integration_manager.installer", "WARNING") as logs:
            inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=d)))
        return inst, "\n".join(logs.output)

    def test_wrong_types_are_repaired(self):
        inst, logs = self.installer({
            "domain": "demo", "release_updates": "abc", "pending_start": "demo", "last_error": 5, "restart_required": "yes",
            "suspended_entries": ["e1", 2], "last_smoke": [], "last_release_check": "x", "rollback_backup": ["b.zip"],
            "installed": {"demo": {"running_tag": "1.0", "pre_update_backup": ["b.zip"], "versions": {"1.0": {}, "2.0": ["x"]}}}})
        self.assertEqual(inst.updates, {})
        self.assertIsNone(inst.state.pending_start)
        self.assertEqual((inst.state.last_error, inst.state.restart_required, inst.state.last_release_check), ("", False, 0))
        self.assertEqual(inst.state.suspended_entries, ["e1"])
        self.assertIsNone(inst.state.last_smoke)
        self.assertIsNone(inst.state.rollback_backup)
        self.assertIsNone(inst.state.installed["demo"]["pre_update_backup"])
        self.assertEqual(list(inst.state.installed["demo"]["versions"]), ["1.0"])
        self.assertEqual(inst.running_tag, "1.0")
        self.assertIn("release_updates", logs)
        self.assertFalse(inst.pending_start_applies())
        self.assertIsNone(inst.min_ha_of("demo", "2.0"))
        with mock.patch.object(inst_mod.jsonio, "read_json", return_value={}):
            self.assertEqual(inst.protected_backups(), set())

    def test_well_typed_values_survive(self):
        d = _tmp(self)
        os.makedirs(os.path.join(d, "integration_manager"))
        state = {"domain": "demo", "release_updates": {"demo": "2.0", "bad": 3}, "pending_start": {"domain": "demo", "tag": "1.0", "ha": None},
                 "suspended_entries": None, "installed": {"demo": {"running_tag": "1.0", "pre_update_backup": "b.zip", "versions": {"1.0": {}}}}}
        _write(os.path.join(d, "integration_manager", "state.json"), json.dumps(state))
        with self.assertLogs("custom_components.integration_manager.installer", "WARNING"):  # the "bad" update only
            inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=d)))
        self.assertEqual(inst.updates, {"demo": "2.0"})
        self.assertEqual(inst.state.pending_start, {"domain": "demo", "tag": "1.0", "ha": None})
        self.assertIsNone(inst.state.suspended_entries)
        self.assertEqual(inst.state.installed["demo"]["pre_update_backup"], "b.zip")


# ----- 5 ------------------------------------------------------------------------------------------

class DirectUrlRequirementsTest(unittest.TestCase):
    REPORT = {"install": [
        {"metadata": {"name": "fromgit", "version": "1.0"}, "is_direct": True,
         "download_info": {"url": "https://github.com/o/fromgit", "vcs_info": {"vcs": "git", "commit_id": "abc"}}},
        {"metadata": {"name": "fromdir", "version": "1.0"}, "is_direct": True, "download_info": {"url": "file:///src/fromdir", "dir_info": {}}},
        {"metadata": {"name": "tarball", "version": "2.0"}, "is_direct": True,
         "download_info": {"url": "https://example.com/dl/tarball-2.0.tar.gz", "archive_info": {}}},
        {"metadata": {"name": "wheel", "version": "3.0"}, "download_info": {"url": "https://files/wheel-3.0-py3-none-any.whl", "archive_info": {}}},
    ]}

    def test_only_archives_are_built_and_from_their_url(self):
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(self.REPORT), stderr="")
        with mock.patch.object(preflight, "_run_pip", return_value=proc):
            # a manifest may not name a URL (installer.bad_requirement); pip's report can still carry direct entries
            res = preflight._pip_dry_run("python", ["fromgit"], None)
        self.assertEqual({r["name"]: r["source_only"] for r in res["install"]}, {"fromgit": False, "fromdir": False, "tarball": True, "wheel": False})
        with mock.patch.object(preflight, "_run_pip", return_value=SimpleNamespace(returncode=0, stderr="")) as run:
            built = preflight._build_from_source("python", res["install"], None)
        self.assertEqual([b["name"] for b in built], ["tarball"])
        cmd = run.call_args.args[0]
        self.assertIn("https://example.com/dl/tarball-2.0.tar.gz", cmd)
        self.assertNotIn("tarball==2.0", cmd)


# ----- 6 ------------------------------------------------------------------------------------------

class ParserLimitsTest(unittest.TestCase):
    def _component(self, files):
        d = _tmp(self)
        for name, text in files.items():
            _write(os.path.join(d, name), text)
        return d

    def test_too_complex_to_parse_is_a_blocker(self):
        d = self._component({"__init__.py": "x = 1\n", "deep.py": "x = " + "-" * 200000 + "1\n", "config_flow.py": "x = " + "-" * 200000 + "1\n"})
        errors, _ = preflight._code_checks(d)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("too complex" in e for e in errors), errors)
        self.assertIsNone(preflight._config_flow_version(d))

    def test_too_large_to_check(self):
        d = self._component({"__init__.py": "x = 1\n", "big.py": "#" * 200 + "\n"})
        with mock.patch.object(preflight, "MAX_CHECK_BYTES", 100):
            errors, _ = preflight._code_checks(d)
        self.assertEqual(errors, ["big.py: too large to check (201 bytes)"])


# ----- 8 ------------------------------------------------------------------------------------------

class UnconfiguredSmokeTest(unittest.TestCase):
    def _installer(self, yaml=False):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.state_dir = d
        if yaml:
            _write(inst.yaml_path("demo"), "host: x\n")
        inst.state = State(domain="demo", installed={"demo": {"versions": {"1.0": {}, "2.0": {}}, "running_tag": "2.0"}},
                           pending_smoke={"domain": "demo", "tag": "2.0", "can_rollback": True})
        inst.hass = _hass()
        inst.settings = SimpleNamespace(int_=lambda key, lo, hi: 300, bool_=lambda key: True)
        inst.busy = False
        inst._smoke_handle, inst._smoke_pending, inst._smoke_waiting, inst._smoke_rechecked = None, {"domain": "demo"}, {}, set()
        inst._entries_of = lambda dom: []
        inst.health_source = lambda grace: {"state": "error", "reason": "not loaded (no config entry, no YAML setup)"}
        inst._save_state = lambda: None
        inst.rollback_full = mock.AsyncMock(return_value={"ok": True, "tag": "1.0", "restore": "b.zip"})
        inst.restart = mock.AsyncMock()
        inst.announce_smoke = mock.Mock()
        return inst

    def test_no_entries_and_no_yaml_is_not_rolled_back(self):
        inst = self._installer()
        asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.rollback_full.assert_not_awaited()
        inst.restart.assert_not_awaited()
        self.assertEqual((inst.state.last_smoke["state"], inst.state.last_smoke["action"]), ("unconfigured", "none"))
        self.assertEqual(inst.state.last_error, "")
        self.assertIsNone(inst.state.pending_smoke)

    def test_yaml_integration_that_does_not_load_still_is(self):
        inst = self._installer(yaml=True)
        asyncio.run(inst._smoke_check("demo", "2.0", True))
        inst.rollback_full.assert_awaited_once()


# ----- C6 / C7 / C9 -------------------------------------------------------------------------------

class ScratchSweepTest(unittest.TestCase):
    def test_old_leftovers_go_at_setup(self):
        d = _tmp(self)
        base = os.path.join(d, "integration_manager", "versions", "demo")
        old = time.time() - 7200
        for name in (".staging-1.0", ".old-1.0", ".preflight-123", "1.0", ".staging-2.0"):
            _write(os.path.join(base, name, "manifest.json"), "{}")
        for name in (".staging-1.0", ".old-1.0", ".preflight-123", "1.0"):
            os.utime(os.path.join(base, name), (old, old))
        Installer(SimpleNamespace(config=SimpleNamespace(config_dir=d)))
        self.assertEqual(sorted(os.listdir(base)), [".staging-2.0", "1.0"])


class AnchoredNamesTest(unittest.TestCase):
    def test_trailing_newline_refused(self):
        self.assertFalse(inst_mod.tag_ok("1.0\n"))
        self.assertIsNone(inst_mod._DOMAIN_RE.match("demo\n"))
        self.assertFalse(manage_views._tag_ok("1.0\n"))
        self.assertIsNone(manage_views._DOMAIN_RE.match("demo\n"))
        self.assertFalse(build_views._ok_ref("main\n"))
        for rx, value in ((build_views._DOMAIN_RE, "demo"), (build_views._REPO_RE, "o/r"), (build_views._HA_RE, "2026.9.0"), (build_views._SHA_RE, "abcdef0")):
            self.assertIsNotNone(rx.match(value))
            self.assertIsNone(rx.match(value + "\n"))


class RemoveVersionOrderTest(unittest.TestCase):
    def test_record_dropped_and_saved_before_the_tree_goes(self):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(installed={"demo": {"versions": {"1.0": {}, "2.0": {}}, "running_tag": "2.0", "previous_tag": "1.0"}}, domain="demo")
        inst.hass = _hass()
        inst.busy = False
        saved, seen = [], []
        inst._save_state = lambda: saved.append("1.0" in inst.state.installed["demo"]["versions"])
        with mock.patch.object(inst_mod, "_rmtree_under", side_effect=lambda p, b: seen.append(list(saved))):
            self.assertTrue(asyncio.run(inst.remove_version("demo", "1.0"))["ok"])
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0] and seen[0][-1] is False)
        self.assertIsNone(inst.state.installed["demo"]["previous_tag"])
        self.assertFalse(inst.busy)


if __name__ == "__main__":
    unittest.main()
