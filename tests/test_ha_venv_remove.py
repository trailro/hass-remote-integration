"""Removing a Home Assistant venv by hand (POST /api/ha/venv/remove), and what the boot does without it.

The previous version's venv is kept as the rollback target (0.7-1 GB).  Once the operator is happy with an upgrade
it can go: a later rollback installs it again from PyPI.  Refused while anything still needs it."""

import json
import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import views
from tests.test_ha_preflight_views import BLOCKED, CLEAN, TARGET, _GateBase
from tests.test_r4_lifecycle import A, B, EntrypointMainBase, make_venv


def _request(body, header="fetch"):
    async def body_json():
        return body

    return SimpleNamespace(content_type="application/json", json=body_json,
                           headers={"X-Requested-With": header} if header else {})


class RemoveViewTest(_GateBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.running_venv = make_venv(self.cfg, views.HA_VERSION)
        self.prev_venv = make_venv(self.cfg, TARGET)
        os.symlink(self.running_venv, os.path.join(self.cfg, "venv-current"))
        self.state({"current": views.HA_VERSION, "desired": views.HA_VERSION, "previous": TARGET})
        self.remove = views.HaVenvRemoveView(self.up, self.installer)
        self.emitted = []
        p = mock.patch.object(views.events, "emit", side_effect=lambda *a, **k: self.emitted.append(a))
        p.start()
        self.addCleanup(p.stop)

    def state(self, data):
        with open(os.path.join(self.cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    async def post(self, version=TARGET, confirm=None, header="fetch"):
        resp = await self.remove.post(_request({"version": version, "confirm": version if confirm is None else confirm}, header))
        return resp.status, json.loads(resp.body)

    async def status(self):
        self.up.available = mock.AsyncMock(return_value={})
        return await self.up.status()

    async def test_the_previous_venv_is_removed_and_the_status_says_a_rollback_reinstalls(self):
        before = await self.status()
        self.assertTrue(before["previous_installed"])
        status, r = await self.post()
        self.assertEqual(status, 200, r)
        self.assertTrue(r["ok"])
        self.assertFalse(os.path.exists(self.prev_venv))
        self.assertEqual([n for n in os.listdir(self.cfg) if n.startswith(".venv-removing-")], [])
        self.assertTrue(os.path.isdir(self.running_venv))
        self.assertTrue(any(TARGET in a[1] and "removed" in a[1] for a in self.emitted), self.emitted)
        after = await self.status()
        self.assertNotIn(TARGET, after["installed_venvs"])
        self.assertEqual(after["previous"], TARGET, "still the rollback target, through a reinstall")
        self.assertFalse(after["previous_installed"])
        self.assertFalse(self.installer.busy)

    async def test_a_rollback_afterwards_schedules_the_reinstall(self):
        await self.post()
        self._view(CLEAN)
        with mock.patch.object(backupkit, "prune", return_value=[]):
            r = await self.view.post(_request({"config": "keep"}), "rollback")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.up._read()["desired"], TARGET)  # the entrypoint installs what is not on the volume
        self.up.validate.assert_awaited_with(TARGET)
        self.up.dependency_check.assert_awaited_with(TARGET)

    async def test_a_rollback_that_reinstalls_a_version_that_cannot_install_needs_force(self):
        await self.post()
        self._view(BLOCKED)
        with mock.patch.object(backupkit, "prune", return_value=[]):
            r = await self.view.post(_request({"config": "keep"}), "rollback")
            self.assertTrue(r["needs_force"], r)
            self.installer.async_backup.assert_not_awaited()
            r = await self.view.post(_request({"config": "keep", "force": True}), "rollback")
        self.assertTrue(r["ok"], r)
        self.assertTrue(any("checks skipped (force)" in a[1] for a in self.emitted), self.emitted)

    async def test_a_rollback_to_an_installed_venv_runs_no_check(self):
        self._view(BLOCKED)
        with mock.patch.object(backupkit, "prune", return_value=[]):
            r = await self.view.post(_request({"config": "keep"}), "rollback")
        self.assertTrue(r["ok"], r)
        self.up.dependency_check.assert_not_awaited()

    async def test_a_removal_that_leaves_files_says_so(self):
        real = shutil.rmtree

        def partial(path, ignore_errors=False):
            os.remove(os.path.join(path, ".ok"))  # one file goes, the rest stays

        with open(os.path.join(self.prev_venv, "bin", "python"), "wb") as fh:
            fh.write(b"x" * 4096)
        with open(os.path.join(self.prev_venv, ".ok"), "wb") as fh:
            fh.write(b"y" * 100)
        with mock.patch.object(shutil, "rmtree", side_effect=partial):
            status, r = await self.post()
        self.assertEqual(status, 200, r)
        self.assertEqual((r["freed_bytes"], r["left_bytes"]), (100, 4096))
        self.assertIn("next boot", r["note"])
        self.assertFalse(os.path.exists(self.prev_venv))
        left = [n for n in os.listdir(self.cfg) if n.startswith(".venv-removing-")]
        self.assertEqual(len(left), 1, "renamed aside for the next boot's sweep")
        real(os.path.join(self.cfg, left[0]))

    async def test_the_typed_version_must_match(self):
        status, r = await self.post(confirm="2026.1")
        self.assertEqual(status, 400)
        self.assertTrue(os.path.isdir(self.prev_venv))

    async def test_a_request_without_the_fetch_header_is_refused(self):
        status, _ = await self.post(header=None)
        self.assertEqual(status, 400)
        self.assertTrue(os.path.isdir(self.prev_venv))

    async def test_the_running_version_is_refused(self):
        status, r = await self.post(views.HA_VERSION)
        self.assertEqual(status, 409)
        self.assertIn("running", r["error"])
        self.assertTrue(os.path.isdir(self.running_venv))

    async def _refused(self, state, needle):
        self.state({"current": views.HA_VERSION, "previous": TARGET, **state})
        status, r = await self.post()
        self.assertEqual(status, 409, r)
        self.assertIn(needle, r["error"])
        self.assertTrue(os.path.isdir(self.prev_venv))
        self.assertFalse(self.installer.busy)

    async def test_refused_while_anything_still_needs_it(self):
        await self._refused({"desired": TARGET}, "is scheduled")
        await self._refused({"desired": views.HA_VERSION, "change": {"to": views.HA_VERSION, "mode": "keep"}}, "version change")
        await self._refused({"smoke": {"version": views.HA_VERSION, "from": TARGET}}, "smoke test")
        await self._refused({"fallback_from": "2099.1.0"}, "switch back")
        await self._refused({"recovery": {"for": TARGET, "backup": "b.zip"}}, "restoring")

    async def test_refused_while_a_restore_scheduled_for_it_waits(self):
        with mock.patch.object(backupkit, "pending_for_version", return_value=TARGET):
            status, r = await self.post()
        self.assertEqual(status, 409)
        self.assertIn("restore scheduled", r["error"])

    async def test_refused_while_another_action_or_a_version_change_runs(self):
        self.installer.busy = True
        status, _ = await self.post()
        self.assertEqual(status, 409)
        self.installer.busy = False
        async with views._HA_CHANGE_LOCK:
            status, _ = await self.post()
        self.assertEqual(status, 409)
        self.assertTrue(os.path.isdir(self.prev_venv))

    async def test_a_link_is_never_followed(self):
        shutil.rmtree(self.prev_venv)
        elsewhere = tempfile.mkdtemp(prefix="hri-venv-target-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        open(os.path.join(elsewhere, "keep"), "w").close()
        os.symlink(elsewhere, self.prev_venv)
        status, r = await self.post()
        self.assertEqual(status, 409)
        self.assertIn("link", r["error"])
        self.assertTrue(os.path.isfile(os.path.join(elsewhere, "keep")))

    async def test_a_bad_version_string_is_refused(self):
        for bad in ("../integration_manager", "current", "2026.1.0/../x"):
            status, _ = await self.post(bad)
            self.assertEqual(status, 400, bad)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, "integration_manager")))

    async def test_a_version_not_installed_is_refused(self):
        status, r = await self.post("2025.12.0")
        self.assertEqual(status, 409)
        self.assertIn("not installed", r["error"])


class BootWithoutThePreviousVenvTest(EntrypointMainBase):
    def test_a_new_version_that_crashes_is_kept_and_says_why(self):
        shutil.rmtree(os.path.join(self.cfg, f"venv-{A}"))
        self.write({"current": B, "desired": B, "previous": A, "proven": A, "change": {"to": B, "mode": "keep"}, "boot_failures": 3})
        self.assertEqual(self.boot(), B)
        state = self.state()
        self.assertNotIn("fallback_from", state)
        self.assertIn(f"{A} has no venv here", state["last_error"])
        self.assertIn(f'"desired": "{A}" in integration_manager/ha.json', state["last_error"])

    def test_a_rollback_installs_it_again(self):
        shutil.rmtree(os.path.join(self.cfg, f"venv-{A}"))
        self.write({"current": B, "desired": A, "previous": A, "proven": B, "change": {"to": A, "mode": "keep"}})
        with mock.patch.object(self.ep, "install", side_effect=lambda v: bool(make_venv(self.cfg, v))) as install:
            self.assertEqual(self.boot(), A)
        install.assert_called_once_with(A)

    def test_an_interrupted_removal_is_swept(self):
        aside = os.path.join(self.cfg, f".venv-removing-{A}-abcd1234")
        os.makedirs(os.path.join(aside, "bin"))
        self.write({"current": B, "desired": B, "proven": B})
        self.assertEqual(self.boot(), B)
        self.assertFalse(os.path.exists(aside))
