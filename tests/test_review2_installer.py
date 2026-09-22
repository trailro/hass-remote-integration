"""Second external review, installer: a dev install with a refused requirement, a registry repo that is not
owner/name, status() reading the volume on the loop, the commit lookup's body cap, the three copies of the
boot-failure take-back, and a link where the manager's own component is copied at boot."""

import asyncio
import json
import os
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import run
from custom_components.integration_manager import build_views
from custom_components.integration_manager import installer as inst_mod
from custom_components.integration_manager.installer import Installer, State
from tests.fakes import entrypoint_for


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-review2-inst-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _installer(test, cfg=None, **hass_extra):
    cfg = cfg or _tmp(test)
    os.makedirs(os.path.join(cfg, "integration_manager"), exist_ok=True)

    async def executor(fn, *args):
        return fn(*args)

    hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components=set()), async_add_executor_job=executor, **hass_extra)
    return Installer(hass)


class DevInstallRequirementTest(unittest.TestCase):
    """M-07: POST /api/dev/install answered ok for a manifest the release path refuses; only start said no."""

    def setUp(self):
        patch = mock.patch.object(inst_mod.events, "emit")
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = _tmp(self)
        self.src = os.path.join(self.cfg, "dev", "demo")

    def install(self, requirements):
        _write(os.path.join(self.src, "manifest.json"), json.dumps({"domain": "demo", "version": "1.0", "requirements": requirements}))
        _write(os.path.join(self.src, "__init__.py"), "")
        inst = _installer(self, self.cfg)
        inst.dev_candidates = lambda: {"dir": os.path.dirname(self.src), "exists": True, "candidates": [{"domain": "demo", "path": self.src}]}
        with self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            return inst, asyncio.run(inst.install_local("demo"))

    def test_a_refused_requirement_fails_the_dev_install(self):
        for req in ("--index-url https://evil.example/simple", "pkg @ file:///config/x.tar.gz"):
            with self.subTest(req=req):
                inst, res = self.install(["requests>=2", req])
                self.assertFalse(res["ok"], res)
                self.assertIn("demo local: requirement", res["error"])
                self.assertNotIn("demo", inst.state.installed, "nothing recorded")
                self.assertFalse(os.path.lexists(inst._version_dir("demo", "local")), "nothing stored")
                self.assertEqual(os.listdir(os.path.join(inst.versions_dir)), [], "no staging left behind")
                self.assertNotIn("demo", inst.registry(), "the dev-mode entry it registered is gone again")


class RegistryFileRepoTest(unittest.TestCase):
    """M-08: an entry read from registry.json (bundled or the user's) is held to the rule add_to_registry applies."""

    BAD = ("o/n/../../../user", "o/n?per_page=1", "o/n#x", "../n", "o/..", "o", "o/n/extra", 7, ["o/n"])

    def test_a_repo_that_is_not_owner_name_is_ignored(self):
        cfg = _tmp(self)
        builtin = os.path.join(cfg, "builtin.json")
        entries = {f"bad_{i}": {"repo": r} for i, r in enumerate(self.BAD)}
        _write(builtin, json.dumps({"integrations": {**entries, "good_b": {"repo": "owner/name.py"}}}))
        inst = _installer(self, cfg)
        _write(inst.user_registry_file, json.dumps({"integrations": {"user_bad": {"repo": "o/n/../../user"},
                                                                     "good_b": {"repo": "o/n?x=1", "name": "shadow"},
                                                                     "good_u": {"repo": "a/b"},
                                                                     "dev": {"repo": "", "local": True}}}))
        with mock.patch.object(inst_mod, "BUILTIN_REGISTRY", builtin), \
                self.assertLogs("custom_components.integration_manager.installer", "WARNING") as logs:
            reg = inst.registry()
        self.assertEqual(sorted(reg), ["dev", "good_b", "good_u"])
        self.assertEqual(reg["good_b"], {"repo": "owner/name.py"}, "a bad user entry does not replace the bundled one")
        self.assertEqual(len([m for m in logs.output if "is ignored" in m]), len(self.BAD) + 2)


class StatusOffTheLoopTest(unittest.TestCase):
    """M-09: status() is async and polled by the UI; what it reads from the volume goes to the executor."""

    def test_status_reads_the_volume_in_the_executor(self):
        cfg = _tmp(self)
        in_executor = threading.local()

        async def executor(fn, *args):
            in_executor.on = True
            try:
                return fn(*args)
            finally:
                in_executor.on = False

        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, components=set()), async_add_executor_job=executor,
                               config_entries=SimpleNamespace(async_entries=lambda domain: []), data={})
        os.makedirs(os.path.join(cfg, "integration_manager"))
        inst = Installer(hass)
        _write(os.path.join(cfg, "custom_components", "demo", "manifest.json"), json.dumps({"domain": "demo", "version": "1", "requirements": []}))
        for tag in ("v1", "v2"):
            os.makedirs(inst._version_dir("demo", tag))
        inst.state = State(domain="demo", installed={"demo": {"running_tag": "v1", "versions": {"v1": {}, "v2": {}, "gone": {}}}})
        on_loop = []
        real_isdir, real_manifest, real_registry = os.path.isdir, Installer.installed_manifest, Installer.registry

        def watch(name, fn):
            def wrapped(*args, **kwargs):
                if not getattr(in_executor, "on", False):
                    on_loop.append(name)
                return fn(*args, **kwargs)
            return wrapped

        with mock.patch.object(inst_mod.os.path, "isdir", watch("isdir", real_isdir)), \
                mock.patch.object(Installer, "installed_manifest", watch("installed_manifest", real_manifest)), \
                mock.patch.object(Installer, "registry", watch("registry", real_registry)), \
                mock.patch.object(inst_mod.importlib.util, "find_spec", watch("find_spec", inst_mod.importlib.util.find_spec)):
            out = asyncio.run(inst.status())
        self.assertEqual(on_loop, [], "read on the event loop")
        self.assertEqual({t: v["dir_present"] for t, v in out["running"]["versions"].items()}, {"v1": True, "v2": True, "gone": False})
        self.assertEqual(out["running"]["code_version"], "1")
        self.assertEqual(out["running"]["requirements_ok"], True)


class _Resp:
    def __init__(self, body, status=200):
        self.status, self._body, self.content_length = status, body, None
        self.content = SimpleNamespace(iter_chunked=self._chunks)

    async def _chunks(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    async def json(self):  # what the lookup used before: reads the whole body, whatever its size
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class CommitLookupCapTest(unittest.TestCase):
    """C-10: the commit lookup is a GitHub read like the others: its body is capped at METADATA_MAX_BYTES."""

    def lookup(self, body):
        inst = SimpleNamespace(spec=lambda d: {"repo": "o/n"}, settings=SimpleNamespace(github_headers=lambda: {}))
        session = SimpleNamespace(get=lambda *a, **k: _Resp(body))
        with mock.patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=session), \
                mock.patch.object(build_views, "METADATA_MAX_BYTES", 1000, create=True):
            return asyncio.run(build_views._commit_of(None, inst, "demo", "main"))

    def test_the_sha_is_read(self):
        self.assertEqual(self.lookup(json.dumps({"sha": "a" * 40}).encode()), "a" * 40)

    def test_an_oversized_body_is_no_answer(self):
        self.assertEqual(self.lookup(json.dumps({"sha": "a" * 40, "pad": "x" * 2000}).encode()), "")


class BootFailureTakeBackParityTest(unittest.TestCase):
    """C-01: three copies take a boot's failure back (run.py before the imports, run.py's stop path, the
    installer outside run.py).  They write different things for a value that is not a count (run.py's stop
    path writes 0, the other two leave it), but what matters is what entrypoint.py reads next boot: the same."""

    VALUES = (None, 0, 1, 3, "2", True, -4, 2.9, "x", float("inf"), [1])

    def setUp(self):
        self.cfg = _tmp(self)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        self.path = os.path.join(self.cfg, "integration_manager", "ha.json")
        self.ep = entrypoint_for(self, self.cfg)
        patch = mock.patch.object(self.ep, "log")  # "is not a count" for every bad value
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, value):
        _write(self.path, json.dumps({"current": "x"} if value is None else {"current": "x", "boot_failures": value}))

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return self.ep._count(json.load(fh).get("boot_failures"))

    def early(self):
        with mock.patch.dict(os.environ, {"HRI_CONFIG": self.cfg}), mock.patch.object(os, "_exit", lambda code: None):
            run._early_stop(15, None)

    def stop(self):
        with mock.patch.object(run, "CONFIG_DIR", self.cfg), mock.patch.object(run, "_boot_settled", False), \
                mock.patch.object(run._LOGGER, "warning"):
            run._undo_boot_failure()

    def installer(self):
        inst = _installer(self, self.cfg, data={})
        inst._undo_boot_failure()

    def test_all_three_leave_the_same_count(self):
        for value in self.VALUES:
            self.write(value)
            wanted = max(0, self.ep._count(value) - 1)
            for name, undo in (("run._early_stop", self.early), ("run._undo_boot_failure", self.stop), ("installer", self.installer)):
                with self.subTest(value=value, path=name):
                    self.write(value)
                    undo()
                    self.assertEqual(self.read(), wanted)


class ManagerComponentLinkTest(unittest.TestCase):
    """U-01: a link where the manager's component is copied at boot stopped every boot (rmtree refuses links)."""

    def test_the_link_is_replaced_and_its_target_kept(self):
        cfg = _tmp(self)
        src = os.path.join(cfg, "image", "integration_manager")
        _write(os.path.join(src, "manifest.json"), "{}")
        target = os.path.join(cfg, "checkout")
        _write(os.path.join(target, "keep.py"), "x = 1\n")
        os.makedirs(os.path.join(cfg, "custom_components"))
        link = os.path.join(cfg, "custom_components", "integration_manager")
        os.symlink(target, link)
        with mock.patch.object(run, "CONFIG_DIR", cfg), mock.patch.object(run, "MANAGER_SRC", src):
            run._sync_manager_component()
        self.assertFalse(os.path.islink(link))
        self.assertTrue(os.path.isfile(os.path.join(link, "manifest.json")))
        self.assertTrue(os.path.isfile(os.path.join(target, "keep.py")), "what the link pointed at is untouched")


if __name__ == "__main__":
    unittest.main()
