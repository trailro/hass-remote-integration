"""Review of b4cd1a1, installer / watchdog / patches / loop IO:

S2-1  a YAML-only integration whose stored YAML did not load reported "no config entry, no YAML setup", the
      reason the watchdog leaves alone: it was never acted on.
S2-2  two watchdog refusals never expired: a config entry setting up forever, and a deferred start kept
      blocked for a Home Assistant version that did not boot.
S2-3  an automatic restart on a ledger that could not be written: the boot reads the file without that
      restart, the daily cap and the backoff start again, and it loops.
S2-4  applying a .patch re-encoded the whole target (errors="replace", LF endings).
S2-5  a crafted hacs.json minimum Home Assistant version (thousands of digits) raised out of ha_vkey.
S2-6  the builder's Prepare with replace + a Home Assistant change could remove the integration there was and
      then have the change refused.
S2-7  a failed .patch apply could leave .tmp files next to the code.
S3-3  the health document and last_error carried a URL token unmasked.
S5-1  file reads on the event loop: protected_backups(), restore-pending.json, the rebuild plan, the import
      tar, manifest.json and the registries.
S5-2  one pre-restore backup named with an impossible date broke every prune.
"""

import asyncio
import errno
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
from custom_components.integration_manager import backup_views, build_views, import_views, patches, views
from custom_components.integration_manager import installer as installer_mod
from custom_components.integration_manager import scheduler as scheduler_mod
from custom_components.integration_manager.installer import Installer, State
from tests.test_patches import DIFF, PatchTestCase
from tests.test_watchdog import DOMAIN, _Base as WatchdogBase


def _full_disk(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def _job(func, *args):
    """A real executor: the job runs in a worker thread, where no event loop runs."""
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


class _Where:
    """Records, per call, whether it ran on the event loop."""

    def __init__(self, result=None):
        self.result, self.on_loop = result, []

    def __call__(self, *_args, **_kwargs):
        self.on_loop.append(_on_loop())
        return self.result() if callable(self.result) else self.result


def _request(body=None, headers=None):
    return SimpleNamespace(headers=headers or {}, query={}, content_type="application/json",
                           json=mock.AsyncMock(return_value=body if body is not None else {}))


def _body(response):
    return json.loads(response.body)


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-review4-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _start_installer(test):
    d = _tmp(test)
    inst = object.__new__(Installer)
    inst.hass = SimpleNamespace(async_add_executor_job=_job, config=SimpleNamespace(components=set()), data={})
    inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
    inst.versions_dir = os.path.join(inst.state_dir, "versions")
    inst.state = State(installed={"demo": {"versions": {"2.0": {"requirements": []}}, "running_tag": None}})
    inst.busy = False
    inst._rollback_undo = None
    inst.settings = SimpleNamespace(backup_keep=5)
    inst.async_backup = mock.AsyncMock(return_value={"name": "pre.zip"})
    inst._save_state = lambda: None
    inst.protected_backups = _Where(set)
    os.makedirs(inst._version_dir("demo", "2.0"))
    return inst


# ----- S2-1 -----------------------------------------------------------------------------------------

class YamlOnlyHealthTest(WatchdogBase):
    def _yaml_only(self, with_yaml):
        inst = self.installer()
        inst._entries_of = lambda dom: []
        inst._patch_cache = {}
        if with_yaml:
            os.makedirs(os.path.dirname(inst.yaml_path(DOMAIN)), exist_ok=True)
            with open(inst.yaml_path(DOMAIN), "w", encoding="utf-8") as fh:
                fh.write("demo:\n  host: 192.0.2.1\n")
        inst.health_source = lambda grace=True: inst.health()
        return inst

    def test_stored_yaml_that_did_not_load_is_acted_on(self):
        inst = self._yaml_only(with_yaml=True)
        h = inst.health()
        self.assertEqual(h["state"], "error")
        self.assertNotIn("no YAML setup", h["reason"])  # before the fix: the reason the watchdog leaves alone
        self.assertIn("YAML setup did not load it", h["reason"])
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_awaited_once()  # nothing to reload: the first step is the restart
        self.assertTrue(self.lines("no config entry to reload"), self.emitted)

    def test_without_yaml_it_is_still_left_alone(self):
        inst = self._yaml_only(with_yaml=False)
        self.assertIn("no config entry, no YAML setup", inst.health()["reason"])
        sch = self.scheduler(inst)
        for _ in range(120):
            self.tick(sch)
        inst.restart.assert_not_awaited()


# ----- S2-2 -----------------------------------------------------------------------------------------

class RefusalsExpireTest(WatchdogBase):
    def test_a_setup_that_never_ends_stops_holding_the_watchdog_off(self):
        inst = self.installer()
        inst._entries_of = lambda dom: [SimpleNamespace(state=SimpleNamespace(value="setup_in_progress"), disabled_by=None, title="Hub")]
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("still setting up"), self.emitted)
        for _ in range(Installer.SETUP_WAIT_S // 60 - 1):
            self.tick(sch)
        inst.restart.assert_not_awaited()  # the smoke test's hang limit has not passed yet
        for _ in range(3):
            self.tick(sch)
        inst.restart.assert_awaited_once()  # before the fix: refused for the life of the process

    def test_a_setup_that_ends_is_not_counted_against_the_next_one(self):
        inst = self.installer()
        setting_up = [SimpleNamespace(state=SimpleNamespace(value="setup_in_progress"), disabled_by=None, title="Hub")]
        inst._entries_of = lambda dom: setting_up
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        self.assertIsNotNone(sch._setup_since)
        self.verdict = {"state": "ok", "reason": ""}
        self.tick(sch)
        self.assertIsNone(sch._setup_since)

    def test_a_blocked_deferred_start_does_not_hold_it_off(self):
        inst = self.installer()
        inst.state.pending_start = {"domain": DOMAIN, "tag": "1.0", "ha": "2020.1.0",
                                    "blocked": "Home Assistant is 2026.9.0, not 2020.1.0 (the update failed or was rolled back)"}
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_awaited_once()  # before the fix: "a start is deferred" forever

    def test_a_deferred_start_that_still_applies_still_refuses(self):
        inst = self.installer()
        inst.state.pending_start = {"domain": DOMAIN, "tag": "1.0", "ha": None}
        sch = self.scheduler(inst)
        for _ in range(16):
            self.tick(sch)
        inst.restart.assert_not_awaited()
        self.assertTrue(self.lines("a start is deferred"))


if __name__ == "__main__":
    unittest.main()
