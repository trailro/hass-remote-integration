"""Review round 13 (lifecycle), F2: the automatic rebuild after a clean-start downgrade and a stop or an uninstall.

async_finish_rebuild decided that the integration runs before it took the import lock, and took that lock without
the busy flag a start, a stop and an uninstall take.  apply() awaits the store copy and the registry map before it
adds the config entry, enabled because the integration was running: a stop finishing in between found no entry to
disable and recorded the integration as stopped, and the rebuild then added (and set up) an enabled entry.  The
rebuild reserves busy like a manual import now, decides under it, and waits for an action that holds busy instead
of being dropped."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState

from custom_components.integration_manager import ha_import
from custom_components.integration_manager.installer import Installer

SUMMARY = {"type": ha_import.REBUILD_TYPE, "domains": {"hub": {"storage_files": [], "entries": [
    {"entry_id": "e1", "title": "Hub", "data": {"host": "h"}, "options": {}, "version": 1, "minor_version": 1}]}}}
PLAN = {"stage": "import", "from": "2026.8.3", "to": "2026.1.0", "backup": "pre.zip", "domain": "hub", "at": "2026-09-17T10:00:00"}


class _Entries:
    """The part of hass.config_entries the import, the stop and the uninstall use."""

    def __init__(self):
        self.entries = []

    def async_entries(self, domain=None):
        return [e for e in self.entries if domain is None or e.domain == domain]

    def async_get_entry(self, entry_id):
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    async def async_add(self, entry):
        if entry.disabled_by is None:
            object.__setattr__(entry, "state", ConfigEntryState.LOADED)  # Home Assistant sets an enabled entry up here
        self.entries.append(entry)

    async def async_remove(self, entry_id):
        self.entries = [e for e in self.entries if e.entry_id != entry_id]


class RebuildVersusLifecycleTest(unittest.TestCase):

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="hri-rebuild-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        self.plan_file = os.path.join(self.cfg, ha_import.REBUILD_FILE)
        with open(self.plan_file, "w", encoding="utf-8") as fh:
            json.dump(PLAN, fh)
        self.copy_reached, self.copy_go = None, None

        async def executor(fn, *args):
            if getattr(fn, "__name__", "") == "_copy" and self.copy_reached is not None:  # apply()'s store copy
                self.copy_reached.set()
                await self.copy_go.wait()
            await asyncio.sleep(0)
            return fn(*args)

        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, components={"hub"}), config_entries=_Entries(),
                                    async_add_executor_job=executor, state=CoreState.running)
        self.installer = Installer(self.hass)
        self.installer.state.domain = "hub"
        self.installer.state.installed = {"hub": {"versions": {"v1": {}}, "running_tag": "v1"}}
        for patch in (mock.patch.object(ha_import, "load_summary", lambda cfg: SUMMARY),
                      mock.patch.object(ha_import, "clear", lambda cfg: None),
                      mock.patch.object(ha_import, "_build_map", lambda out_dir, domain, entry_id: {"entities": {}, "devices": {}}),
                      mock.patch.object(ha_import, "loader", mock.Mock(async_get_integration=mock.AsyncMock(return_value=SimpleNamespace(is_built_in=False)))),
                      mock.patch.object(ha_import, "pn"), mock.patch.object(ha_import.events, "emit"),
                      mock.patch.object(ha_import, "REBUILD_RETRY_S", 0.01, create=True),
                      mock.patch.object(Installer, "dismiss_patch_notification", lambda self, domain: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def rebuild(self):
        return ha_import.async_finish_rebuild(self.hass, mock.Mock(), self.installer)

    def entries(self):
        return self.hass.config_entries.async_entries("hub")

    def plan(self):
        try:
            with open(self.plan_file, encoding="utf-8") as fh:
                return json.load(fh)
        except OSError:
            return None

    def run_during_the_copy(self, action):
        async def scenario():
            self.copy_reached, self.copy_go = asyncio.Event(), asyncio.Event()
            task = asyncio.ensure_future(self.rebuild())
            await asyncio.wait_for(self.copy_reached.wait(), 5)
            result = await action()
            self.copy_go.set()
            await asyncio.wait_for(task, 5)
            return result

        return asyncio.run(scenario())

    def test_a_stop_during_the_rebuild_never_ends_with_an_enabled_entry(self):
        stopped = self.run_during_the_copy(self.installer.stop)
        enabled = [e for e in self.entries() if e.disabled_by is None]
        if stopped.get("ok"):
            self.assertEqual(enabled, [], "the rebuild added an enabled entry after the stop succeeded")
            self.assertIsNone(self.installer.running)
        else:
            self.assertIn("another action", stopped["error"])
            self.assertEqual(self.installer.running, "hub")
            self.assertEqual(len(enabled), 1)
        self.assertFalse(self.installer.busy)

    def test_an_uninstall_during_the_rebuild_leaves_no_entry_behind(self):
        removed = self.run_during_the_copy(lambda: self.installer.uninstall("hub"))
        if removed.get("ok"):
            self.assertEqual(self.entries(), [], "the rebuild added an entry for an integration that was uninstalled")
        else:
            self.assertIn("another action", removed["error"])
            self.assertIn("hub", self.installer.state.installed)
            self.assertEqual(len(self.entries()), 1)
        self.assertFalse(self.installer.busy)

    def test_a_rebuild_waits_for_an_action_that_holds_the_manager(self):
        async def scenario():
            self.installer.busy = True  # a start, a stop or the boot reconcile running
            task = asyncio.ensure_future(self.rebuild())
            await asyncio.sleep(0.1)
            during = (list(self.entries()), self.plan(), task.done())
            self.installer.busy = False
            await asyncio.wait_for(task, 5)
            return during

        entries, plan, done = asyncio.run(scenario())
        self.assertEqual(entries, [], "imported while another action held the manager")
        self.assertEqual(plan, PLAN, "the plan was dropped or an attempt counted while the rebuild waited")
        self.assertFalse(done)
        self.assertEqual([e.disabled_by for e in self.entries()], [None])
        self.assertIsNone(self.plan(), "done: the plan goes")
        self.assertFalse(self.installer.busy)

    def test_what_runs_is_decided_once_the_action_it_waited_for_is_over(self):
        async def scenario():
            self.installer.busy = True  # a stop that is finishing
            task = asyncio.ensure_future(self.rebuild())
            await asyncio.sleep(0.05)
            self.installer.state.domain = None
            self.installer.busy = False
            await asyncio.wait_for(task, 5)

        asyncio.run(scenario())
        self.assertEqual(self.entries(), [])
        self.assertIsNone(self.plan())
        self.assertIn("is not running now", ha_import.events.emit.call_args.args[1])

    def test_a_plan_replaced_while_the_rebuild_waited_is_left_alone(self):
        newer = {**PLAN, "stage": "reset", "at": "2026-09-17T11:00:00"}

        async def scenario():
            self.installer.busy = True
            task = asyncio.ensure_future(self.rebuild())
            await asyncio.sleep(0.05)
            with open(self.plan_file, "w", encoding="utf-8") as fh:
                json.dump(newer, fh)  # a newer clean start scheduled meanwhile
            self.installer.busy = False
            await asyncio.wait_for(task, 5)

        asyncio.run(scenario())
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.plan(), newer)


if __name__ == "__main__":
    unittest.main()
