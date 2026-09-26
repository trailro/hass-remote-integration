"""*Check* in the environment builder is a question, not a change: it used to add the repository to the
registry (preflight looked it up there), so checking an unregistered repo left it registered and rewrote a
damaged registry.json.  Here: Check persists nothing, Prepare still registers, and both still work."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import build_views, preflight
from custom_components.integration_manager.installer import Installer

REPORT = {"ok": True, "blockers": [], "warnings": []}


def _request(body):
    return SimpleNamespace(headers={}, query={}, content_type="application/json", json=mock.AsyncMock(return_value=body))


def _body(response):
    return json.loads(response.body)


async def _executor_job(func, *args):
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


class BuilderRegistryCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hri-builder-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        os.makedirs(os.path.join(self.dir, "integration_manager"))
        self.inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=self.dir)))
        self.reg = self.inst.user_registry_file
        self.check = object.__new__(build_views.BuildCheckView)
        self.check.hass, self.check.installer, self.check._checks = SimpleNamespace(async_add_executor_job=_executor_job), self.inst, {}
        self.check.updater = SimpleNamespace(validate=mock.AsyncMock())
        self.check._pf = SimpleNamespace(_lock=asyncio.Lock())

    def registry_text(self):
        with open(self.reg, encoding="utf-8") as fh:
            return fh.read()

    def registry_files(self):
        return sorted(f for f in os.listdir(os.path.dirname(self.reg)) if f.startswith("registry.json"))

    def run_check(self, body):
        self.pf = mock.AsyncMock(return_value=dict(REPORT))
        with mock.patch.object(preflight, "run", self.pf), \
                mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value="c" * 40)):
            return _body(asyncio.run(self.check.post(_request(body))))

    def prepare(self, body, check_id):
        prep = object.__new__(build_views.BuildPrepareView)
        prep.hass, prep.installer, prep.publisher, prep._check = None, self.inst, None, self.check
        prep.updater = SimpleNamespace(status=mock.AsyncMock(return_value={"current": build_views.HA_VERSION}))
        self.inst.install = mock.AsyncMock(return_value={"ok": True, "tag": body["ref"]})
        with mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value="c" * 40)):
            return _body(asyncio.run(prep.post(_request({**body, "check_id": check_id}))))


class CheckPersistsNothingTest(BuilderRegistryCase):
    def test_check_of_an_unregistered_repo_writes_no_registry_and_still_reports(self):
        res = self.run_check({"domain": "demo", "repo": "owner/demo", "ref": "1.0.0", "name": "Demo"})
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(res["report"]["ok"])
        self.assertTrue(res["check_id"])
        self.assertEqual(self.registry_files(), [], "Check registered the repository as a side effect")
        self.assertNotIn("demo", self.inst.registry())
        self.assertEqual(self.pf.await_args.kwargs.get("repo"), "owner/demo")  # passed, not looked up

    def test_a_damaged_registry_is_neither_rewritten_nor_copied_aside(self):
        with open(self.reg, "w", encoding="utf-8") as fh:
            fh.write('{"integrations": [broken')
        res = self.run_check({"domain": "demo", "repo": "owner/demo", "ref": "1.0.0"})
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(self.registry_files(), ["registry.json"], "a .corrupt copy was kept for a check")
        self.assertEqual(self.registry_text(), '{"integrations": [broken')

    def test_check_of_a_registered_domain_still_works_and_leaves_the_file_as_it_is(self):
        self.inst.add_to_registry("demo", "owner/demo", "Demo")
        before = self.registry_text()
        mtime = os.path.getmtime(self.reg)
        res = self.run_check({"domain": "demo", "ref": "2.0.0"})  # no repo in the body: the registry has it
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(self.pf.await_args.kwargs.get("repo"), "owner/demo")
        self.assertEqual(self.registry_text(), before)
        self.assertEqual(os.path.getmtime(self.reg), mtime)

    def test_an_unregistered_domain_without_a_repo_is_still_refused(self):
        res = self.run_check({"domain": "demo", "ref": "1.0.0"})
        self.assertFalse(res["ok"])
        self.assertIn("not in the registry", res["error"])
        self.assertEqual(self.registry_files(), [])

    def test_a_repo_that_contradicts_the_registered_one_is_still_refused(self):
        self.inst.add_to_registry("demo", "owner/demo")
        res = self.run_check({"domain": "demo", "repo": "other/demo", "ref": "1.0.0"})
        self.assertFalse(res["ok"])
        self.assertIn("another domain name", res["error"])


class PrepareStillRegistersTest(BuilderRegistryCase):
    def test_prepare_registers_what_check_only_looked_at(self):
        body = {"domain": "demo", "repo": "owner/demo", "ref": "1.0.0", "name": "Demo"}
        check = self.run_check(body)
        self.assertEqual(self.registry_files(), [])
        res = self.prepare(body, check["check_id"])
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(self.inst.registry()["demo"], {"name": "Demo", "repo": "owner/demo"})
        self.inst.install.assert_awaited()

    def test_prepare_with_a_stale_check_id_installs_nothing(self):
        body = {"domain": "demo", "repo": "owner/demo", "ref": "1.0.0"}
        res = self.prepare(body, "not-a-check-id")
        self.assertFalse(res["ok"])
        self.assertIn("run Check", res["error"])
        self.inst.install.assert_not_awaited()


class PreflightRepoOverrideTest(unittest.TestCase):
    """preflight.run without the registry entry: the override is used, and nothing else changed."""

    def test_an_unregistered_domain_without_an_override_still_raises(self):
        inst = SimpleNamespace(spec=lambda domain: {})
        with self.assertRaisesRegex(ValueError, "no GitHub repository known"):
            asyncio.run(preflight.run(None, inst, "demo", "1.0.0"))

    def test_the_registry_is_not_consulted_when_a_repo_is_given(self):
        def no_spec(domain):
            raise AssertionError("the registry was read although the repo was passed")

        inst = SimpleNamespace(spec=no_spec)
        with self.assertRaises(Exception) as caught:  # fails later, in the download: the lookup is what matters
            asyncio.run(preflight.run(None, inst, "demo", "1.0.0", repo="owner/demo"))
        self.assertNotIsInstance(caught.exception, AssertionError)


if __name__ == "__main__":
    unittest.main()
