"""Second external review, the two findings in how the manager sets itself up.

R2-05  The Environment builder can schedule a Home Assistant version change AND a start.
       After the restart the MQTT publisher connects while the boot reconcile still runs,
       so it takes the identity the installer had THEN - and the boot path, alone among
       the start paths, never handed it the new one.  Nothing was running: the publisher
       stays disconnected with "no integration is running" while the UI shows the
       integration started.  Another domain was running: the new entities go out under
       hass_<old domain>, the main Home Assistant creates them with the wrong unique ids,
       and the identity sweep at the next restart deletes and recreates them.

R2-06  Every state.json write on the setup path was unguarded, so a full disk ended the
       setup of this component: no manager UI, and a container that crash-loops right
       after a rollback or a restore - exactly when the disk is most likely to be full.
"""

import asyncio
import errno
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant import core, loader

import custom_components.integration_manager as im
from custom_components.integration_manager.installer import Installer


def _full_disk(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


class FakePublisher:
    """Records the identity it would publish under, exactly where the real publisher
    reads it: _async_first_connect (started by async_start) and async_after_start."""

    def __init__(self, hass, key_provider=None, health_provider=None, rules_provider=None):
        self._key_provider = key_provider
        self.connected_as = "<never connected>"
        self.after_start = []
        self.manager = None
        self.config = SimpleNamespace(enabled=False)
        self.base_topic = "hass_none"
        self.stats = {}

    def build_health(self, grace=True):
        return {}

    async def async_start(self):
        self.connected_as = self._key_provider()

    async def async_after_start(self, res):
        self.after_start.append(res)
        self.connected_as = self._key_provider()

    async def async_clear_identity(self, base_topic):
        return 0


class _Boot:
    """async_setup of the real component, with the MQTT publisher and the Installer's two
    slow boot steps stood in for.  Everything else - the views, the host guard, the
    manager device, the scheduler - is the real thing."""

    def __init__(self, test, *, running=None, starts=None, adopts=None, reconcile=None, state=None, ha_json=None, save=None):
        self.hass = core.HomeAssistant(tempfile.mkdtemp())
        loader.async_setup(self.hass)
        self.hass.http = mock.Mock()
        test.addAsyncCleanup(self._stop)
        self.gate = asyncio.Event()  # opened by the test: the reconcile ends when it says so
        self.published = []
        self._running, self._starts, self._adopts, self._reconcile = running, starts, adopts, reconcile
        self._state, self._save = state or {}, save
        if ha_json is not None:
            d = os.path.join(self.hass.config.config_dir, "integration_manager")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "ha.json"), "w", encoding="utf-8") as fh:
                json.dump(ha_json, fh)

    def _installer(self, hass):
        inst = Installer(hass)
        inst.state.installed = {"demo": {"versions": {"1.0": {}}, "running_tag": "1.0"}}
        inst.state.domain = self._running
        if self._running:
            inst.state.installed[self._running] = {"versions": {"9.9": {}}, "running_tag": "9.9"}
        for key, value in self._state.items():
            setattr(inst.state, key, value)
        gate, starts, adopts, fail = self.gate, self._starts, self._adopts, self._reconcile

        async def async_reconcile():
            await gate.wait()  # the publisher connects while this runs
            if adopts is not None:
                inst.state.domain = adopts  # _adopt_enabled_entries / _apply_pending_rollback do this
            if fail is not None:
                raise fail

        async def async_run_pending_start():
            if starts is not None:
                inst.state.domain = starts

        inst.async_reconcile = async_reconcile
        inst.async_run_pending_start = async_run_pending_start
        if self._save is not None:
            inst._save_state = self._save
        return inst

    async def setup(self):
        def make_publisher(*args, **kwargs):
            self.published.append(FakePublisher(*args, **kwargs))
            return self.published[-1]

        with mock.patch.object(im, "MqttPublisher", make_publisher), mock.patch.object(im, "Installer", self._installer):
            return await im.async_setup(self.hass, {})

    @property
    def publisher(self):
        return self.published[0]

    async def finish_reconcile(self):
        self.gate.set()
        await self.hass.data["integration_manager_ready"]

    async def _stop(self):
        task = self.hass.data.get("integration_manager_ready")
        if task is not None and not task.done():
            task.cancel()
        await self.hass.async_stop(force=True)


class BootStartIdentityTest(unittest.IsolatedAsyncioTestCase):
    """R2-05: a start the boot reconcile makes gives MQTT its identity."""

    async def test_nothing_was_running_so_mqtt_never_connected(self):
        boot = _Boot(self, running=None, starts="demo")
        await boot.setup()
        # what the container looked like a moment ago: the publisher's first connect found no identity
        self.assertIsNone(boot.publisher.connected_as, "the publisher connects before the deferred start")
        await boot.finish_reconcile()
        self.assertEqual(boot.publisher.connected_as, "hass_demo",
                         "MQTT stays disconnected ('no integration is running') while the UI shows demo started")
        self.assertEqual(len(boot.publisher.after_start), 1)

    async def test_another_domain_was_running_so_the_old_identity_was_kept(self):
        boot = _Boot(self, running="old", starts="demo")
        await boot.setup()
        self.assertEqual(boot.publisher.connected_as, "hass_old")
        await boot.finish_reconcile()
        self.assertEqual(boot.publisher.connected_as, "hass_demo",
                         "demo's entities would be published under hass_old, with unique ids the next restart sweeps away")

    async def test_an_integration_adopted_by_the_reconcile_gets_the_identity_too(self):
        # _adopt_enabled_entries (a restore brought enabled entries back) and the interrupted
        # rollback both set state.domain inside the reconcile, without any pending start
        boot = _Boot(self, running=None, adopts="demo")
        await boot.setup()
        await boot.finish_reconcile()
        self.assertEqual(boot.publisher.connected_as, "hass_demo")

    async def test_a_failed_reconcile_still_hands_over_what_it_had_already_started(self):
        boot = _Boot(self, running="old", adopts="demo", reconcile=RuntimeError("pip exploded"))
        await boot.setup()
        await boot.finish_reconcile()  # async_run_pending_start is never reached
        self.assertEqual(boot.publisher.connected_as, "hass_demo")

    async def test_nothing_started_so_the_publisher_is_left_alone(self):
        boot = _Boot(self, running="demo", starts="demo")
        await boot.setup()
        await boot.finish_reconcile()
        self.assertEqual(boot.publisher.after_start, [],
                         "an unchanged identity must not cost a reconnect at every boot")

    async def test_a_reconcile_cancelled_by_a_shutdown_hands_nothing_over(self):
        boot = _Boot(self, running=None, starts="demo")
        await boot.setup()
        ready = boot.hass.data["integration_manager_ready"]
        ready.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await ready
        self.assertEqual(boot.publisher.after_start, [])

    async def test_a_broker_that_is_down_does_not_fail_the_boot(self):
        boot = _Boot(self, running=None, starts="demo")
        await boot.setup()
        boot.publisher.async_after_start = mock.AsyncMock(side_effect=OSError("broker unreachable"))
        await boot.finish_reconcile()  # raises before the fix

    async def test_the_publisher_exists_when_the_reconcile_task_is_created(self):
        """The task used to be created before the publisher: a reconcile that never awaits
        would reach the hand-over with the name unbound."""
        boot = _Boot(self, running=None, starts="demo")
        boot.gate.set()  # async_reconcile returns without awaiting anything
        await boot.setup()
        await boot.hass.data["integration_manager_ready"]
        self.assertEqual(boot.publisher.connected_as, "hass_demo")


class FullDiskSetupTest(unittest.IsolatedAsyncioTestCase):
    """R2-06: a state.json write that fails must not cost the operator the UI."""

    NOW = time.strftime("%Y-%m-%dT%H:%M:%S")
    RESTORE = {"last_restore": {"ok": True, "backup": "pre.zip", "at": NOW, "files": 3}}

    async def test_the_boot_reconcile_error_is_not_raised_a_second_time(self):
        boot = _Boot(self, reconcile=RuntimeError("pip exploded"), save=_full_disk)
        await boot.setup()
        await boot.finish_reconcile()  # run.py awaits this task; it used to die here, setting up no integration at all
        self.assertTrue(boot.hass.data["integration_manager_ready"].done())

    async def test_the_announced_smoke_verdict(self):
        smoke = {"domain": "demo", "tag": "1.0", "state": "error", "at": self.NOW, "reason": "boom", "action": "rolled back"}
        boot = _Boot(self, state={"last_smoke": smoke}, save=_full_disk)
        self.assertTrue(await boot.setup())

    async def test_the_released_rollback_backup(self):
        boot = _Boot(self, state={"rollback_backup": "pre.zip", "rollback_at": self.NOW}, ha_json=self.RESTORE, save=_full_disk)
        self.assertTrue(await boot.setup())

    async def test_the_reported_restore_outcome(self):
        boot = _Boot(self, ha_json=self.RESTORE, save=_full_disk)
        self.assertTrue(await boot.setup())

    async def test_all_of_them_at_once_and_the_later_steps_still_run(self):
        smoke = {"domain": "demo", "tag": "1.0", "state": "error", "at": self.NOW, "reason": "boom", "action": "none"}
        boot = _Boot(self, state={"last_smoke": smoke, "rollback_backup": "pre.zip", "rollback_at": self.NOW},
                     ha_json=self.RESTORE, save=_full_disk)
        with mock.patch.object(im.events, "emit") as emit:
            self.assertTrue(await boot.setup())
        # the restore outcome is the last of the three: a raise in the first used to skip it
        self.assertIn("restore", [call.args[0] for call in emit.call_args_list])

    async def test_the_write_is_only_forgiven_when_the_disk_says_so(self):
        # a bug in the state, not a full disk: it must not be swallowed here
        with self.assertRaises(TypeError):
            im.boot_step("a broken state", mock.Mock(side_effect=TypeError("not JSON serialisable")))
