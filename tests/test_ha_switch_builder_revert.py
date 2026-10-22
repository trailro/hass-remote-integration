"""The environment builder's combined change: Home Assistant switched AND another integration version (B) started at
the target's first boot, from the version that ran before (A).

When the Home Assistant smoke test finds B not set up there, it switches back to the old Home Assistant AND takes the
integration back to A, with one restore (the switch's backup: .storage and custom_components, taken with A
deployed) and one story on the timeline.  B's own smoke test does not roll back on its own, so the two verdicts
never race.  Before, Home Assistant went back with B still deployed, and B often does not run there (ramses_cc
0.61.1 needs serialx>=1.11.0, which 2026.9.4 does not allow)."""

import asyncio
import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.test_ha_switch_smoke import NEW, OLD, _Base, _entry

START = {"domain": "demo", "tag": "v2", "from_tag": "v1"}


class _Builder(_Base):
    def setUp(self):
        super().setUp()
        self.installer.running_tag = "v2"
        self.installer.state = SimpleNamespace(installed={"demo": {"versions": {"v1": {}, "v2": {}}}})
        self.installer.revert_for_ha_fallback = mock.Mock()
        self.installer.rollback_full = mock.AsyncMock(return_value={"ok": True, "tag": "v1", "restore": "pre-start.zip"})

    def builder_boot(self, start=START):
        state = {"current": NEW, "desired": NEW, "previous": OLD, "proven": "", "boot_failures": 1,
                 "change": {"to": NEW, "mode": "keep", "backup": "pre-ha.zip", "applied": True, "at": "2026-10-22T10:00:00",
                            "smoke": {"domain": "demo", "from": OLD, "entries": ["e1"], "loaded": True},
                            **({"start": start} if start else {})}}
        self.write(state)
        asyncio.run(self.updater.async_start_smoke(self.installer, state))


class BuilderRevertTest(_Builder):
    def test_the_smoke_record_carries_the_start(self):
        self.builder_boot()
        self.assertEqual(self.state()["smoke"]["start"], START)

    def test_home_assistant_and_the_integration_go_back_together(self):
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        self.check()
        state = self.state()
        self.assertEqual((state["desired"], state["fallback_from"]), (OLD, NEW))
        self.assertEqual(state["recovery"]["backup"], "pre-ha.zip", "the switch's own backup, not a merged one")
        self.assertEqual(set(state["recovery"]["parts"]), {"storage", "custom_components"})
        self.assertEqual(state["smoke_fallback"]["integration"], {"from": "v2", "to": "v1"})
        self.assertEqual(backupkit.pending_for_version(self.cfg), OLD)
        self.assertEqual(set(backupkit.pending_parts(self.cfg)), {"storage", "custom_components"})
        self.installer.revert_for_ha_fallback.assert_called_once_with("demo", "v1", "v2", OLD)
        self.installer.rollback_full.assert_not_awaited()
        self.assertIn(f"switched back to Home Assistant {OLD} with demo v1", state["last_error"])
        self.installer.restart.assert_awaited_once()

    def test_a_start_that_did_not_run_is_a_plain_fallback(self):
        self.installer.running_tag = "v1"  # the deferred start was blocked: A still runs
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        self.check()
        state = self.state()
        self.assertNotIn("integration", state["smoke_fallback"])
        self.assertIn(f"fallback-config-for-{OLD}", state["recovery"]["backup"])
        self.installer.revert_for_ha_fallback.assert_not_called()

    def test_a_previous_version_gone_from_the_store_is_a_plain_fallback(self):
        self.installer.state.installed["demo"]["versions"].pop("v1")
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        self.check()
        self.installer.revert_for_ha_fallback.assert_not_called()

    def test_when_home_assistant_cannot_go_back_the_integration_still_does(self):
        shutil.rmtree(os.path.join(self.cfg, f"venv-{OLD}"))
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        self.check()
        self.installer.rollback_full.assert_awaited_once_with("demo", rejected=True)
        self.installer.restart.assert_awaited_once()
        self.assertTrue(any("not switched back" in m and "rolled back to v1" in m for m in self.emitted), self.emitted)

    def test_with_automatic_rollback_off_nothing_moves(self):
        self.settings["auto_rollback"] = False
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        self.check()
        self.installer.rollback_full.assert_not_awaited()
        self.installer.restart.assert_not_awaited()

    def test_a_builder_change_records_the_start(self):
        cfg = self.cfg

        async def async_backup(label=""):
            return backupkit.create(cfg, label)

        inst = SimpleNamespace(hass=self.hass, busy=False, running="demo", running_tag="v1", _entries_of=lambda d: [_entry("e1")],
                               settings=SimpleNamespace(backup_keep=50), async_backup=async_backup,
                               protected_backups=lambda: set(), min_ha_of=lambda *_a: None, state=SimpleNamespace(rollback_backup=None))
        with mock.patch.object(self.views, "HA_VERSION", OLD):
            asyncio.run(self.views.async_change_ha_version(inst, self.updater, NEW, "keep", "environment builder", start=START))
        self.assertEqual(self.state()["change"]["start"], START)


class InstallerSideTest(_Builder):
    def _installer(self):
        from custom_components.integration_manager.installer import Installer, State

        inst = object.__new__(Installer)
        inst.hass = self.hass
        inst.config_dir, inst.state_dir = self.cfg, os.path.join(self.cfg, backupkit.STATE_DIR)
        inst.state = State(domain="demo", installed={"demo": {"running_tag": "v2", "previous_tag": "v1", "pre_update_backup": "x.zip",
                                                               "versions": {"v1": {}, "v2": {}}}})
        inst._save_state = mock.Mock()
        inst._smoke_handle, inst._smoke_pending, inst._smoke_waiting, inst._smoke_rechecked = None, None, {}, set()
        return inst

    def test_the_deferred_start_of_a_combined_change_leaves_the_verdict_to_home_assistant(self):
        import homeassistant

        here = homeassistant.const.__version__
        inst = self._installer()
        inst.state.pending_start = {"domain": "demo", "tag": "v2", "ha": here}
        self.write({"current": here, "change": {"to": here, "mode": "keep", "start": START}})
        inst.start = mock.AsyncMock(return_value={"ok": True})
        asyncio.run(inst.async_run_pending_start())
        self.assertTrue(inst.start.call_args.kwargs["ha_judges"])

    def test_a_deferred_start_without_the_combined_record_judges_itself(self):
        import homeassistant

        here = homeassistant.const.__version__
        inst = self._installer()
        inst.state.pending_start = {"domain": "demo", "tag": "v2", "ha": here}
        self.write({"current": here, "change": {"to": here, "mode": "keep"}})
        inst.start = mock.AsyncMock(return_value={"ok": True})
        asyncio.run(inst.async_run_pending_start())
        self.assertFalse(inst.start.call_args.kwargs["ha_judges"])

    def test_the_revert_records_the_version_that_ran_before(self):
        inst = self._installer()
        inst.state.pending_smoke = {"domain": "demo", "tag": "v2", "can_rollback": False}
        inst.state.pending_start = {"domain": "demo", "tag": "v2", "ha": NEW}
        inst.revert_for_ha_fallback("demo", "v1", "v2", OLD)
        rec = inst.state.installed["demo"]
        self.assertEqual((rec["running_tag"], rec["previous_tag"], rec["pre_update_backup"]), ("v1", None, None))
        self.assertEqual(inst.state.pending_smoke, {"domain": "demo", "tag": "v1", "can_rollback": False})
        self.assertIsNone(inst.state.pending_start)
        self.assertTrue(inst.state.restart_required)
        inst._save_state.assert_called()
