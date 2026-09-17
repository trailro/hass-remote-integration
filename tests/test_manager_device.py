"""manager_device: update payloads, the manager document and the actions."""

import asyncio
import unittest
import unittest.mock
from types import SimpleNamespace

from custom_components.integration_manager import manager_device as md
from jsonio import ha_vkey, vkey
from tests.fakes import FakeInstaller, FakePublisher, FakeUpdater


def device(installer=None, updater=None, publisher=None):
    log = []
    inst = installer or FakeInstaller(log=log)
    pub = publisher or FakePublisher(log=inst.log)
    return md.ManagerDevice(SimpleNamespace(), inst, updater or FakeUpdater(), pub)


class UpdateTest(unittest.TestCase):
    def test_newer(self):
        doc = md._update("1.0.0", "1.1.0", "Demo", "https://x/1.1.0", vkey, in_progress=True)
        self.assertEqual(doc, {"installed_version": "1.0.0", "latest_version": "1.1.0", "title": "Demo", "in_progress": True,
                               "release_url": "https://x/1.1.0"})

    def test_equal_older_and_unknown_latest(self):
        for latest in ("1.1.0", "1.0.0", None):
            doc = md._update("1.1.0", latest, "Demo", "https://x", vkey)
            self.assertEqual(doc["latest_version"], "1.1.0", latest)
            self.assertNotIn("release_url", doc)

    def test_newer_without_url(self):
        self.assertNotIn("release_url", md._update("1.0.0", "2.0.0", "Demo", None, vkey))

    def test_nothing_installed(self):
        self.assertEqual(md._update(None, "1.0.0", "Demo", "https://x", vkey), {})
        self.assertEqual(md._update("", "1.0.0", "Demo", "https://x", vkey), {})

    def test_local_never_updates(self):
        doc = md._update("local", "9.9.9", "Demo", "https://x", vkey)
        self.assertEqual(doc["latest_version"], "local")
        self.assertNotIn("release_url", doc)

    def test_ha_beta_is_not_newer(self):
        self.assertEqual(md._update("2026.9.0", "2026.9.0b3", "HA", "https://x", ha_vkey)["latest_version"], "2026.9.0")


class IntegrationLatestTest(unittest.TestCase):
    def test_no_running_integration(self):
        self.assertIsNone(device(FakeInstaller(updates={"demo": "1.0.0"})).integration_latest())

    def test_store_versions(self):
        inst = FakeInstaller("demo", "1.0.0", versions=("1.0.0", "1.2.0", "local"))
        self.assertEqual(device(inst).integration_latest(), "1.2.0")

    def test_release_check_wins_when_newer(self):
        inst = FakeInstaller("demo", "1.0.0", versions=("1.0.0",), updates={"demo": "1.3.0", "other": "9.0.0"})
        self.assertEqual(device(inst).integration_latest(), "1.3.0")

    def test_store_wins_when_newer(self):
        inst = FakeInstaller("demo", "v2.0.0", versions=("v2.0.0",), updates={"demo": "1.9.0"})
        self.assertEqual(device(inst).integration_latest(), "v2.0.0")

    def test_only_local(self):
        self.assertIsNone(device(FakeInstaller("demo", "local", versions=("local",))).integration_latest())


class DocumentTest(unittest.TestCase):
    def test_integration_update(self):
        inst = FakeInstaller("demo", "1.0.0", versions=("1.0.0",), updates={"demo": "1.1.0"}, spec={"repo": "o/r", "name": "Demo"})
        dev = device(inst)
        u = dev.document()["updates"]["integration"]
        self.assertEqual((u["installed_version"], u["latest_version"], u["title"], u["in_progress"]), ("1.0.0", "1.1.0", "Demo", False))
        self.assertEqual(u["release_url"], "https://github.com/o/r/releases/tag/1.1.0")
        dev._running = "install_integration"
        self.assertTrue(dev.document()["updates"]["integration"]["in_progress"])

    def test_local_running(self):
        inst = FakeInstaller("demo", "local", versions=("local", "1.0.0"), spec={"repo": "o/r"})
        u = device(inst).document()["updates"]["integration"]
        self.assertEqual(u["latest_version"], "local")
        self.assertNotIn("release_url", u)

    def test_no_integration(self):
        doc = device(FakeInstaller(), publisher=FakePublisher(commands=False)).document()
        self.assertIsNone(doc["integration"])
        self.assertEqual(doc["updates"]["integration"], {})
        self.assertEqual(doc["patches"], "none")
        self.assertFalse(doc["commands"])

    def test_ha_latest_survives_a_failed_check(self):
        upd = FakeUpdater()
        dev = device(updater=upd)
        self.assertEqual(dev.document()["updates"]["home_assistant"]["latest_version"], md.HA_VERSION)
        upd._cache = (1.0, {"latest_stable": "9999.1.0"})
        self.assertEqual(dev.document()["updates"]["home_assistant"]["latest_version"], "9999.1.0")
        upd._cache = (2.0, {"latest_stable": None, "error": "timeout"})
        u = dev.document()["updates"]["home_assistant"]
        self.assertEqual(u["latest_version"], "9999.1.0")
        self.assertEqual(u["release_url"], "https://github.com/home-assistant/core/releases/tag/9999.1.0")

    def test_the_document_writes_no_file(self):
        """C18: document() is read by GET /api/manager and every publication; the known versions are saved by the
        version check and the resource sample instead."""
        upd = FakeUpdater()
        dev = device(updater=upd)
        dev._latest_file = "/nonexistent/integration_manager/latest_versions.json"
        written = []
        upd._cache = (1.0, {"latest_stable": "9999.1.0"})
        with unittest.mock.patch.object(md.writer, "write_nowait", lambda path, data, **_k: written.append((path, data))):
            self.assertEqual(dev.document()["updates"]["home_assistant"]["latest_version"], "9999.1.0")
            self.assertEqual(written, [])

            async def executor(func, *args):
                return func(*args)

            dev.hass = SimpleNamespace(async_add_executor_job=executor)
            dev._sample_blocking = lambda: {}

            async def record(_now):
                return None

            dev._record = record
            asyncio.run(dev.async_sample())
            self.assertEqual(written, [(dev._latest_file, {"home_assistant": "9999.1.0"})])
            asyncio.run(dev.async_sample())  # unchanged: not written again
            self.assertEqual(len(written), 1)

    def test_ha_in_progress(self):
        dev = device()
        for desired, expected in ((None, False), (md.HA_VERSION, False), ("2000.1.0", False), ("9999.1.0", True)):
            dev._ha_desired = desired
            self.assertEqual(dev.document()["updates"]["home_assistant"]["in_progress"], expected, desired)
        dev._ha_desired = None
        dev._running = "install_home_assistant"
        self.assertTrue(dev.document()["updates"]["home_assistant"]["in_progress"])

    def test_manager_release_uses_the_exact_tag(self):
        dev = device()
        dev.version = "0.1.0"
        dev.manager_tag, dev.manager_latest = "v9.9.9", "9.9.9"
        u = dev.document()["updates"]["manager"]
        self.assertEqual(u["latest_version"], "9.9.9")
        self.assertEqual(u["release_url"], f"https://github.com/{md.MANAGER_REPO}/releases/tag/v9.9.9")


class ActionTest(unittest.IsolatedAsyncioTestCase):
    def results(self, dev):
        return [e[1] for e in dev.publisher.log if e[0] == "result"]

    async def test_unknown_action(self):
        dev = device()
        rec = {}
        res = await dev.async_action("reboot", rec)
        self.assertFalse(res["ok"])
        self.assertIn("unknown action", res["error"])
        self.assertIn(("finish", rec, "failed", res["error"]), dev.publisher.log)
        self.assertEqual(self.results(dev), [res])
        self.assertEqual(dev.last_action, res)

    async def test_second_action_while_one_runs(self):
        dev = device()
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return {"ok": True, "note": "backup x"}

        dev._do_backup = slow
        first = asyncio.create_task(dev.async_action("backup"))
        await asyncio.sleep(0)
        self.assertEqual(dev._running, "backup")
        second = await dev.async_action("check_updates")  # a restart waits instead (test_e2e_cmd.py)
        self.assertTrue(second["error"].startswith("backup is still running"), second["error"])
        self.assertNotIn(("check_updates",), dev.installer.log)
        release.set()
        self.assertTrue((await first)["ok"])
        self.assertIsNone(dev._running)

    async def test_rate_limit(self):
        dev = device()

        async def ok():
            return {"ok": True}

        dev._do_backup = dev._do_check_updates = ok
        for action in ("backup", "check_updates"):
            self.assertTrue((await dev.async_action(action))["ok"])
            res = await dev.async_action(action)
            self.assertFalse(res["ok"])
            self.assertIn("ran moments ago", res["error"])
        self.assertTrue((await dev.async_action("restart"))["ok"])
        self.assertTrue((await dev.async_action("restart"))["ok"])

    async def test_restart_before_result(self):
        """The result said ok before anything stopped, and the publish it went
        out in could hang: the restart comes first now."""
        dev = device()
        res = await dev.async_action("restart")
        self.assertEqual((res["action"], res["ok"]), ("restart", True))
        self.assertNotIn("restart", {k for k in res if k != "action"})
        kinds = [e[0] for e in dev.installer.log]
        self.assertLess(kinds.index("restart"), kinds.index("result"))

    async def test_restart_waits_for_a_running_action(self):
        """m18: a bare restart waits for a running install, start or backup like the restart after an install does."""
        dev = device()
        dev.installer.busy = True
        waited = []
        real_sleep = asyncio.sleep

        async def sleep(seconds):
            waited.append(seconds)
            if len(waited) == 3:
                dev.installer.busy = False  # the install finished
            await real_sleep(0)

        with unittest.mock.patch.object(md.asyncio, "sleep", sleep):
            res = await dev.async_action("restart")
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(waited), 3)
        self.assertIn("restart", [e[0] for e in dev.installer.log])

    async def test_restart_skipped_when_the_action_outlasts_the_wait(self):
        dev = device()
        dev.installer.busy = True
        waited = []

        async def sleep(seconds):
            waited.append(seconds)

        with unittest.mock.patch.object(md.asyncio, "sleep", sleep):
            res = await dev.async_action("restart")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "restart skipped: another action is still running")
        self.assertEqual(sum(waited), 300)
        self.assertNotIn("restart", [e[0] for e in dev.installer.log])

    async def test_unexpected_error(self):
        dev = device()

        async def boom():
            raise RuntimeError("boom")

        dev._do_check_updates = boom
        with self.assertLogs(md._LOGGER, "ERROR"):
            res = await dev.async_action("check_updates")
        self.assertEqual(res["error"], "RuntimeError: boom")
        self.assertIsNone(dev._running)

    async def test_install_integration_refusals(self):
        cases = (
            (FakeInstaller(updates={"demo": "2.0.0"}), "no integration is running"),
            (FakeInstaller("demo", "local", versions=("local",), updates={"demo": "2.0.0"}), "runs a dev build"),
            (FakeInstaller("demo", "1.2.0", versions=("1.0.0", "1.2.0")), "no newer release"),
            (FakeInstaller("demo", "1.2.0", versions=("1.2.0",), updates={"demo": "1.1.0"}), "no newer release"),
        )
        for inst, error in cases:
            dev = device(inst)
            rec = {}
            res = await dev.async_action("install_integration", rec)
            self.assertFalse(res["ok"])
            self.assertIn(error, res["error"])
            self.assertIn(("finish", rec, "failed", res["error"]), dev.publisher.log)
            self.assertFalse(any(e[0] == "restart" for e in inst.log))


if __name__ == "__main__":
    unittest.main()
