"""Review round 13 (lifecycle), U1: a reinstall of a stored tag killed between the swap and the record.

_store_version / _store_local set the stored copy aside (.old-<tag>) and move the new one in; install() and
install_local() then record the new metadata and only after that drop the aside copy.  Killed between the swap and
the record, the setup sweep put the aside copy back only when the version directory was missing: the new, unrecorded
copy stayed in the store under the old record (its requirements, its installed_at), and the aside copy was removed
once it was an hour old.  The boot reconcile compares the deployed marker with that old record, so it deployed
nothing, and the next deploy of the tag brought in code no record describes, with the old requirements."""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import installer as inst_mod
from custom_components.integration_manager.installer import Installer


class Killed(BaseException):
    """The process ends here: nothing after it runs, no except Exception clause sees it."""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ReinstallKilledMidwayTest(unittest.TestCase):

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-store-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.src = os.path.join(self.cfg, "dev", "demo")
        patch = mock.patch.object(inst_mod.events, "emit")
        patch.start()
        self.addCleanup(patch.stop)
        inst = self.boot()
        self.dev_copy("old", ["lib==1"])
        self.assertTrue(self.install_local(inst)["ok"])
        inst.state.domain = "demo"
        inst.state.installed["demo"]["running_tag"] = "local"
        inst._ensure_deployed("demo", "local")
        inst._save_state()
        self.first = dict(inst.state.installed["demo"]["versions"]["local"])
        old = time.time() - 7200  # stored two hours ago: older than the sweep's scratch age
        os.utime(inst._version_dir("demo", "local"), (old, old))
        time.sleep(1.1)  # the reinstall gets another installed_at (seconds)

    def boot(self):
        async def job(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, components=set()), async_add_executor_job=job)
        return Installer(hass)

    def dev_copy(self, code, requirements):
        _write(os.path.join(self.src, "manifest.json"), json.dumps({"domain": "demo", "name": "Demo", "version": "1.0", "requirements": requirements}))
        _write(os.path.join(self.src, "__init__.py"), f"code = {code!r}\n")

    def install_local(self, inst):
        inst.dev_candidates = lambda: {"dir": os.path.dirname(self.src), "exists": True, "candidates": [{"domain": "demo", "path": self.src}]}
        inst.registry = lambda: {"demo": {"repo": "", "local": True}}
        inst._requirements_for = mock.AsyncMock(return_value=[])
        inst._install_requirements = lambda reqs, force=False: []
        return asyncio.run(inst.install_local("demo"))

    def reinstall_killed(self, where):
        inst = self.boot()
        self.dev_copy("new", ["lib==2"])
        with mock.patch.object(*where), self.assertRaises(Killed):
            self.install_local(inst)

    def assert_store_matches_the_record(self, inst, code):
        rec = inst.state.installed["demo"]["versions"]["local"]
        stored = inst._version_dir("demo", "local")
        self.assertEqual(_read(os.path.join(stored, "__init__.py")), f"code = {code!r}\n", "the store holds a copy the record does not describe")
        self.assertEqual(inst._manifest_at(stored)["requirements"], rec["requirements"])
        inst._ensure_deployed("demo", "local")  # the boot reconcile of the running tag
        self.assertEqual(_read(os.path.join(inst._component_dir("demo"), "__init__.py")), f"code = {code!r}\n")
        self.assertEqual([n for n in os.listdir(os.path.dirname(stored)) if n.startswith(".old-")], [])
        self.assertFalse(os.path.exists(os.path.join(inst._component_dir("demo"), inst_mod.STORE_STAMP)), "store bookkeeping deployed as code")

    def test_a_copy_stored_before_stamps_is_left_as_it_was(self):
        inst = self.boot()
        os.remove(os.path.join(inst._version_dir("demo", "local"), inst_mod.STORE_STAMP))
        inst.state.installed["demo"]["versions"]["local"].pop("stored")
        inst._save_state()
        aside = inst._aside_dir("demo", "local")
        shutil.copytree(inst._version_dir("demo", "local"), aside)
        _write(os.path.join(aside, "__init__.py"), "code = 'aside'\n")
        self.boot()
        # no stamp: nothing tells which copy the record describes, the version directory stays (as before stamps)
        self.assertEqual(_read(os.path.join(inst._version_dir("demo", "local"), "__init__.py")), "code = 'old'\n")

    def test_a_release_reinstall_killed_before_its_record_is_put_back_too(self):
        import io
        import zipfile

        def blob(code):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("repo-abc/custom_components/demo/manifest.json", json.dumps({"domain": "demo", "version": "2.0"}))
                zf.writestr("repo-abc/custom_components/demo/__init__.py", f"code = {code!r}\n")
            return buf.getvalue()

        inst = self.boot()
        inst._store_version(blob("recorded"), "demo", "main", "s1")
        inst.state.installed["demo"]["versions"]["main"] = {"installed_at": "x", "version": "2.0", "stored": "s1"}
        inst._save_state()
        inst._store_version(blob("unrecorded"), "demo", "main", "s2")  # killed here, before install() records "s2"
        inst = self.boot()
        self.assertEqual(_read(os.path.join(inst._version_dir("demo", "main"), "__init__.py")), "code = 'recorded'\n")
        self.assertFalse(os.path.exists(inst._aside_dir("demo", "main")))

    def test_killed_before_the_record_the_recorded_copy_comes_back(self):
        self.reinstall_killed((Installer, "_replace_current", mock.AsyncMock(side_effect=Killed)))
        inst = self.boot()
        self.assertEqual(inst.state.installed["demo"]["versions"]["local"], self.first)
        self.assert_store_matches_the_record(inst, "old")

    def test_killed_after_the_record_the_new_copy_stays(self):
        real = inst_mod._rmtree_under

        def rmtree_under(path, base):
            if os.path.basename(path).startswith(".old-"):
                raise Killed
            return real(path, base)

        self.reinstall_killed((inst_mod, "_rmtree_under", rmtree_under))
        inst = self.boot()
        rec = inst.state.installed["demo"]["versions"]["local"]
        self.assertNotEqual(rec["installed_at"], self.first["installed_at"])
        self.assertEqual(rec["requirements"], ["lib==2"])
        self.assert_store_matches_the_record(inst, "new")


if __name__ == "__main__":
    unittest.main()
