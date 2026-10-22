"""The environment builder's combined change: Home Assistant switched AND another integration version (B) started at
the target's first boot, from the version that ran before (A).

When the Home Assistant smoke test finds B not set up there (setup_retry included, after the same extra interval
B's own smoke test gives), it switches back to the old Home Assistant AND takes the integration back to A.  B's own
smoke test waits while that smoke test is pending and judges as always once it is done without acting (the version
kept, its record dropped, or none planned), so no case is left without an automatic rollback.

The switch back is written in steps with one commit point: (1) the restore, for the old version only; (2) ha.json -
the switch back and the integration's way back (integration_revert) in one write; (3) state.json.  Killed after (1)
the new version boots again, its entrypoint drops the restore and the smoke test runs again; killed after (2) the
old version's boot completes (3) from the intent."""

import asyncio
import json
import os
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.test_ha_switch_smoke import NEW, OLD, _Base, _entry
from tests.test_r4_lifecycle import A as EP_A, B as EP_B, EntrypointMainBase

START = {"domain": "demo", "tag": "v2", "from_tag": "v1"}


def _store(version, data):
    return json.dumps({"version": version, "minor_version": 1, "key": "x", "data": data})


class _Builder(_Base):
    def setUp(self):
        super().setUp()
        bdir = os.path.join(self.cfg, backupkit.BACKUP_DIR)
        # the switch's backup, taken at Prepare with v1 deployed on OLD
        with zipfile.ZipFile(os.path.join(bdir, "pre-ha.zip"), "w") as zf:
            zf.writestr(backupkit.MARKER, "{}")
            zf.writestr("backup-info.json", json.dumps({"ha_version": OLD}))
            zf.writestr(".storage/core.config_entries", _store(1, {"entries": [
                {"entry_id": "e1", "domain": "demo", "version": 2, "data": {"as": "v1"}},
                {"entry_id": "m1", "domain": "mqtt", "data": {"broker": "before"}}]}))
            zf.writestr(".storage/core.entity_registry", _store(1, {"entities": [
                {"entity_id": "sensor.demo", "platform": "demo", "config_entry_id": "e1", "name": "v1 name"},
                {"entity_id": "sensor.mqtt", "platform": "mqtt", "config_entry_id": "m1", "name": "before"}], "deleted_entities": []}))
            zf.writestr(".storage/demo.data", _store(1, "v1 store"))
            zf.writestr(".storage/input_boolean", _store(1, "helper before"))
        # what runs now on NEW, with v2
        live_entries = {"entries": [{"entry_id": "e1", "domain": "demo", "version": 3, "data": {"as": "v2"}},
                                    {"entry_id": "e9", "domain": "demo", "data": {"made": "by v2"}},
                                    {"entry_id": "m1", "domain": "mqtt", "data": {"broker": "changed on NEW"}},
                                    {"entry_id": "i1", "domain": "integration_manager", "data": {}}]}
        live_entities = {"entities": [{"entity_id": "sensor.demo", "platform": "demo", "config_entry_id": "e1", "name": "v2 name"},
                                      {"entity_id": "sensor.demo_new", "platform": "demo", "config_entry_id": "e9"},
                                      {"entity_id": "sensor.mqtt", "platform": "mqtt", "config_entry_id": "m1", "name": "renamed on NEW"}],
                         "deleted_entities": []}
        for name, text in {"core.config_entries": _store(1, live_entries), "core.entity_registry": _store(1, live_entities),
                           "demo.data": _store(1, "v2 store"), "input_boolean": _store(1, "helper changed on NEW"),
                           "counter": _store(1, "made on NEW")}.items():
            with open(os.path.join(self.cfg, ".storage", name), "w", encoding="utf-8") as fh:
                fh.write(text)
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

    def pending_storage(self):
        out = {}
        with zipfile.ZipFile(backupkit.pending_archive(self.cfg)) as zf:
            for n in zf.namelist():
                if n.startswith(".storage/"):
                    try:
                        out[n[len(".storage/"):]] = json.loads(zf.read(n))["data"]
                    except ValueError:
                        pass  # the volume's plain test files
        return out


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
        self.assertIn(f"fallback-config-for-{OLD}", state["recovery"]["backup"], "merged, not the switch's backup whole")
        self.assertEqual(state["recovery"]["parts"], ["storage"], "custom_components is deployed by the boot, not restored wholesale")
        self.assertEqual(state["smoke_fallback"]["integration"], {"from": "v2", "to": "v1"})
        self.assertEqual(state["integration_revert"], {"domain": "demo", "from": "v2", "to": "v1", "back": OLD,
                                                       "at": state["smoke_fallback"]["at"]})
        self.assertEqual(backupkit.pending_for_version(self.cfg), OLD)
        stores = self.pending_storage()
        # the integration's items as v1 left them (and nothing v2 created); every other integration's as they are now
        self.assertEqual({e["entry_id"]: e["data"] for e in stores["core.config_entries"]["entries"]},
                         {"e1": {"as": "v1"}, "m1": {"broker": "changed on NEW"}, "i1": {}})
        self.assertEqual({e["entity_id"]: e.get("name") for e in stores["core.entity_registry"]["entities"]},
                         {"sensor.demo": "v1 name", "sensor.mqtt": "renamed on NEW"})
        self.assertEqual(stores["demo.data"], "v1 store")
        self.assertEqual(stores["input_boolean"], "helper changed on NEW")
        self.assertEqual(stores["counter"], "made on NEW")
        self.installer.revert_for_ha_fallback.assert_called_once_with("demo", "v1", "v2", OLD)
        self.installer.rollback_full.assert_not_awaited()
        self.assertIn(f"switched back to Home Assistant {OLD} with demo v1", state["last_error"])
        self.installer.restart.assert_awaited_once()

    def test_setup_retry_after_the_extra_interval_takes_both_back(self):
        self.entries["e1"] = _entry("e1", state="setup_retry", reason="serial port not available")
        self.builder_boot()
        self.check()  # one more interval first, as B's own smoke test gives
        self.assertNotIn("fallback_from", self.state())
        self.check()
        self.assertEqual(self.state()["smoke_fallback"]["integration"], {"from": "v2", "to": "v1"})
        self.installer.restart.assert_awaited_once()

    def test_setup_retry_without_a_combined_change_is_kept_as_before(self):
        self.entries["e1"] = _entry("e1", state="setup_retry")
        self.builder_boot(start=None)
        self.check()
        self.check()
        self.assertNotIn("fallback_from", self.state())
        self.assertNotIn("smoke", self.state(), "the record goes: the integration's own smoke test judges from here")

    def test_a_start_that_did_not_run_is_a_plain_fallback(self):
        self.installer.running_tag = "v1"
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        self.check()
        state = self.state()
        self.assertNotIn("integration", state["smoke_fallback"])
        self.assertNotIn("integration_revert", state)
        entries = {e["entry_id"]: e["data"] for e in self.pending_storage()["core.config_entries"]["entries"]}
        self.assertEqual(entries["e1"], {"as": "v2"}, "a plain fallback keeps what the integration has now")
        self.installer.revert_for_ha_fallback.assert_not_called()

    def test_a_previous_version_gone_from_the_store_is_a_plain_fallback(self):
        self.installer.state.installed["demo"]["versions"].pop("v1")
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        self.check()
        self.installer.revert_for_ha_fallback.assert_not_called()

    def test_when_home_assistant_cannot_go_back_the_integration_still_does(self):
        import shutil

        shutil.rmtree(os.path.join(self.cfg, f"venv-{OLD}"))
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        self.check()
        self.installer.rollback_full.assert_awaited_once_with("demo", rejected=True)
        self.installer.restart.assert_awaited_once()

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


class KillPointTest(_Builder):
    """Each step of the switch back cut off: what is on the volume tells the next boot what to do."""

    def setUp(self):
        super().setUp()
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()

    def test_killed_after_the_restore_before_ha_json(self):
        with mock.patch.object(self.updater, "_drop_smoke", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.check()
        state = self.state()
        # nothing switched: the new version boots again, its entrypoint drops a restore for another version, the
        # smoke record is still there and judges again
        self.assertEqual(state["desired"], NEW)
        self.assertIn("smoke", state)
        self.assertNotIn("integration_revert", state)
        self.assertEqual(backupkit.pending_for_version(self.cfg), OLD)
        self.installer.revert_for_ha_fallback.assert_not_called()

    def test_ha_json_that_cannot_be_written_cancels_the_restore(self):
        with mock.patch.object(self.updater, "_drop_smoke", side_effect=OSError("disk full")):
            self.check()
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertEqual(self.state()["desired"], NEW)
        self.installer.revert_for_ha_fallback.assert_not_called()
        self.installer.restart.assert_not_awaited()

    def test_killed_after_ha_json_before_state_json(self):
        self.installer.revert_for_ha_fallback.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.check()
        state = self.state()
        self.assertEqual(state["desired"], OLD)
        self.assertEqual(state["integration_revert"]["to"], "v1")
        self.assertEqual(backupkit.pending_for_version(self.cfg), OLD)  # the boot of OLD restores, then the manager reverts

    def test_state_json_that_cannot_be_written_still_restarts_into_the_switch_back(self):
        self.installer.revert_for_ha_fallback.side_effect = OSError("read-only")
        self.check()
        self.assertEqual(self.state()["integration_revert"]["back"], OLD)
        self.installer.restart.assert_awaited_once()


class _RealInstaller(_Builder):
    def make(self, running="v2"):
        from custom_components.integration_manager import installer as inst_mod

        inst = object.__new__(inst_mod.Installer)
        inst.hass = self.hass
        inst.config_dir, inst.state_dir = self.cfg, os.path.join(self.cfg, backupkit.STATE_DIR)
        inst.state_file = os.path.join(inst.state_dir, "state.json")
        inst.state = inst_mod.State(domain="demo", installed={"demo": {"running_tag": running, "previous_tag": "v1",
                                                                       "pre_update_backup": "x.zip", "versions": {"v1": {}, "v2": {}}}})
        inst._save_state = mock.Mock()
        inst._smoke_handle, inst._smoke_pending, inst._smoke_waiting, inst._smoke_rechecked = None, None, {}, set()
        return inst


class BootIntentTest(_RealInstaller):
    def intent(self, back=OLD, desired=OLD):
        self.write({"current": back, "desired": desired, "integration_revert": {"domain": "demo", "from": "v2", "to": "v1",
                                                                                "back": back, "at": "t"}})

    def test_the_boot_of_the_old_version_completes_the_revert(self):
        import homeassistant

        self.intent()
        inst = self.make()
        with mock.patch.object(homeassistant.const, "__version__", OLD):
            inst._apply_ha_integration_revert()
        rec = inst.state.installed["demo"]
        self.assertEqual((rec["running_tag"], rec["previous_tag"]), ("v1", None))
        self.assertEqual(inst.state.pending_smoke, {"domain": "demo", "tag": "v1", "can_rollback": False})
        self.assertFalse(inst.state.restart_required, "this boot is the restart")
        self.assertNotIn("integration_revert", self.state())

    def test_a_revert_already_recorded_is_only_cleared(self):
        import homeassistant

        self.intent()
        inst = self.make(running="v1")
        with mock.patch.object(homeassistant.const, "__version__", OLD):
            inst._apply_ha_integration_revert()
        inst._save_state.assert_not_called()
        self.assertNotIn("integration_revert", self.state())

    def test_a_start_that_never_ran_is_dropped(self):
        import homeassistant

        self.intent()
        inst = self.make(running="v1")
        inst.state.pending_start = {"domain": "demo", "tag": "v2", "ha": NEW}
        with mock.patch.object(homeassistant.const, "__version__", OLD):
            inst._apply_ha_integration_revert()
        self.assertIsNone(inst.state.pending_start)

    def test_on_its_way_back_it_waits(self):
        import homeassistant

        self.intent(desired=OLD)
        inst = self.make()
        with mock.patch.object(homeassistant.const, "__version__", NEW):
            inst._apply_ha_integration_revert()
        self.assertEqual(inst.state.installed["demo"]["running_tag"], "v2")
        self.assertIn("integration_revert", self.state())

    def test_another_version_booting_drops_it(self):
        import homeassistant

        self.intent(desired=NEW)
        inst = self.make()
        with mock.patch.object(homeassistant.const, "__version__", NEW):
            inst._apply_ha_integration_revert()
        self.assertEqual(inst.state.installed["demo"]["running_tag"], "v2")
        self.assertNotIn("integration_revert", self.state())

    def test_a_failed_write_leaves_memory_as_on_disk(self):
        inst = self.make()
        inst._save_state.side_effect = OSError("read-only")
        from custom_components.integration_manager.installer import State

        on_disk = State(domain="demo", installed={"demo": {"running_tag": "v2", "versions": {"v1": {}, "v2": {}}}})
        inst._load_state = mock.Mock(return_value=on_disk)
        with self.assertRaises(OSError):
            inst.revert_for_ha_fallback("demo", "v1", "v2", OLD)
        self.assertIs(inst.state, on_disk)

    def test_the_revert_in_process_asks_for_the_restart(self):
        inst = self.make()
        inst.state.pending_start = {"domain": "demo", "tag": "v2", "ha": NEW}
        inst.revert_for_ha_fallback("demo", "v1", "v2", OLD)
        self.assertEqual(inst.state.installed["demo"]["running_tag"], "v1")
        self.assertIsNone(inst.state.pending_start)
        self.assertTrue(inst.state.restart_required)


class DeferralTest(_RealInstaller):
    """B's own smoke test waits for the Home Assistant smoke test of its change, and judges once that one is done."""

    def test_the_updater_decides_while_its_verdict_is_pending(self):
        self.entries["e1"] = _entry("e1", state="setup_retry")
        self.builder_boot()
        self.assertTrue(self.updater.decides("demo", "v2"))
        self.assertFalse(self.updater.decides("demo", "v3"))
        self.check()  # the extra interval: still pending
        self.assertTrue(self.updater.decides("demo", "v2"))
        self.entries["e1"] = _entry("e1")
        self.check()  # loaded: kept, the verdict is given
        self.assertFalse(self.updater.decides("demo", "v2"), "done without acting: B judges itself from here")

    def test_no_smoke_planned_means_nothing_to_wait_for(self):
        state = {"current": NEW, "desired": NEW, "change": {"to": NEW, "mode": "keep", "backup": "pre-ha.zip", "start": START}}
        self.write(state)
        asyncio.run(self.updater.async_start_smoke(self.installer, state))  # A had no loaded entry at Prepare
        self.assertFalse(self.updater.decides("demo", "v2"))

    def test_a_dropped_record_releases_it(self):
        self.builder_boot()
        self.installer.running = "other"  # another integration runs now: the record is dropped at the check
        self.check()
        self.assertFalse(self.updater.decides("demo", "v2"))

    def test_the_smoke_check_waits_without_judging(self):
        inst = self.make()
        inst.settings = SimpleNamespace(int_=lambda *a: 60)
        inst.ha_smoke_decides = lambda domain, tag: (domain, tag) == ("demo", "v2")
        inst.health_source = mock.Mock(side_effect=AssertionError("judged while the Home Assistant verdict is pending"))
        inst.rollback_full = mock.AsyncMock()
        asyncio.run(inst._smoke_check("demo", "v2", True))
        self.assertEqual(self.later, [60])
        inst.rollback_full.assert_not_awaited()


class EntrypointCrashFallbackTest(EntrypointMainBase):
    def test_a_crash_loop_after_a_combined_change_takes_the_integration_back_too(self):
        self.write({"current": EP_B, "desired": EP_B, "previous": EP_A, "proven": EP_A, "boot_failures": 3,
                    "change": {"to": EP_B, "mode": "keep", "start": {"domain": "demo", "tag": "v2", "from_tag": "v1"}}})
        self.assertEqual(self.boot(), EP_A)
        self.assertEqual(self.state()["integration_revert"]["to"], "v1")
        self.assertEqual(self.state()["integration_revert"]["back"], EP_A)

    def test_a_plain_crash_loop_records_none(self):
        self.write({"current": EP_B, "desired": EP_B, "previous": EP_A, "proven": EP_A, "boot_failures": 3,
                    "change": {"to": EP_B, "mode": "keep"}})
        self.assertEqual(self.boot(), EP_A)
        self.assertNotIn("integration_revert", self.state())


class RaceAndBoundTest(_RealInstaller):
    """The verdict in progress keeps the start's own smoke test waiting until it acted or let go; a verdict that dies,
    or never comes, does not keep it waiting for good."""

    def test_decides_stays_true_while_the_verdict_acts(self):
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        seen = []
        real = self.installer.async_backup

        async def backup(label=""):
            seen.append(self.updater.decides("demo", "v2"))  # awaits before the lock, where B's timer could fire
            return await real(label)

        self.installer.async_backup = backup
        self.check()
        self.assertEqual(seen, [True])
        self.assertTrue(self.updater.decides("demo", "v2"), "it acted: B waits for the restart")

    def test_a_verdict_that_dies_lets_go_and_says_so(self):
        self.entries["e1"] = _entry("e1", state="not_loaded")
        self.builder_boot()
        with mock.patch.object(self.updater, "_smoke_fall_back", mock.AsyncMock(side_effect=RuntimeError("boom"))):
            self.check()
        self.assertFalse(self.updater.decides("demo", "v2"))
        self.assertTrue(any("could not judge" in m for m in self.emitted), self.emitted)

    def test_the_wait_is_bounded(self):
        from custom_components.integration_manager import installer as inst_mod

        inst = self.make()
        inst.ha_smoke_decides = lambda domain, tag: True
        inst.settings = SimpleNamespace(int_=lambda *a: 60)
        limit = 2 * max(inst.SETUP_WAIT_S, 60) + 60 + inst_mod.HA_SMOKE_WAIT_MARGIN_S
        with mock.patch.object(inst_mod.time, "monotonic", return_value=1000.0):
            self.assertTrue(inst._waits_for_ha_smoke("demo", "v2"))
        with mock.patch.object(inst_mod.time, "monotonic", return_value=1000.0 + limit + 1):
            self.assertFalse(inst._waits_for_ha_smoke("demo", "v2"))
            self.assertFalse(inst._waits_for_ha_smoke("demo", "v2"))
        self.assertEqual(sum("gave no verdict" in m for m in self.emitted), 1)

    def test_it_waits_again_right_before_its_own_rollback(self):
        inst = self.make()
        answers = iter([False, True])  # free at the top of the check, the Home Assistant verdict starts before the rollback
        inst.ha_smoke_decides = lambda domain, tag: next(answers)
        inst.settings = SimpleNamespace(int_=lambda *a: 60, bool_=lambda *a: True)
        inst.busy = False
        inst._entries_of = lambda domain: [_entry("e1", state="not_loaded")]
        inst.health_source = lambda grace=False: {"state": "error", "reason": "not loaded"}
        inst.yaml_path = lambda domain: "/nonexistent"
        inst.rollback_full = mock.AsyncMock()
        with mock.patch.object(self.views, "_ha_change_lock_taken", return_value=False):
            asyncio.run(inst._smoke_check("demo", "v2", True))
        inst.rollback_full.assert_not_awaited()
        self.assertEqual(self.later, [60])


class RevertWriteFailureTest(_RealInstaller):
    def test_the_start_s_own_smoke_test_is_scheduled_again(self):
        from custom_components.integration_manager.installer import State

        inst = self.make()
        inst._save_state.side_effect = OSError("read-only")
        inst._load_state = mock.Mock(return_value=State(domain="demo", installed={"demo": {"running_tag": "v2", "versions": {"v2": {}}}},
                                                        pending_smoke={"domain": "demo", "tag": "v2", "can_rollback": True}))
        inst._schedule_smoke = mock.Mock()
        with self.assertRaises(OSError):
            inst.revert_for_ha_fallback("demo", "v1", "v2", OLD)
        inst._schedule_smoke.assert_called_once_with("demo", "v2", True)

    def test_the_intent_is_cleared_with_the_ha_json_reader(self):
        import homeassistant
        from custom_components.integration_manager import ha_updater, installer as inst_mod

        self.write({"current": OLD, "desired": OLD, "integration_revert": {"domain": "demo", "from": "v2", "to": "v1", "back": OLD}})
        inst = self.make()
        with mock.patch.object(homeassistant.const, "__version__", OLD), \
                mock.patch.object(inst_mod.jsonio, "update_json", side_effect=ValueError("ha.json is unreadable")) as upd:
            inst._apply_ha_integration_revert()  # unreadable at the write: kept for the next boot, no crash
        self.assertIs(upd.call_args.kwargs["read"], ha_updater.read_ha_json_for_update)
