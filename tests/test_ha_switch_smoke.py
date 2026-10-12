"""The smoke test after a Home Assistant version change.

A new version that fails to boot three times was left by the entrypoint; one that boots while the integration that
ran before no longer sets up on it stayed, with the integration down.  Now the switch records the integration's
loaded config entries, the first boot of the new version checks them `smoke_test_s` later and, when they do not
load, goes back to the version before the switch once (with the configuration from the switch's backup); a
degraded but loaded integration is kept and reported."""

import asyncio
import json
import os
import shutil
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.test_r4_lifecycle import A, B, EntrypointMainBase
from tests.test_review_backup import _volume, _zip

OLD, NEW = "2026.9.4", "2026.10.0"


def _entry(entry_id, state="loaded", title=None, reason=None, disabled_by=None):
    return SimpleNamespace(entry_id=entry_id, title=title or entry_id, state=SimpleNamespace(value=state), reason=reason,
                           disabled_by=disabled_by)


class _Base(unittest.TestCase):
    def setUp(self):
        from custom_components.integration_manager import ha_updater, views

        self.ha_updater, self.views = ha_updater, views
        self.cfg = cfg = _volume()
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        _zip(cfg, "pre-ha.zip", {"ha_version": OLD})
        for v in (OLD, NEW):  # what the entrypoint keeps: the running venv and the previous one
            site = os.path.join(cfg, f"venv-{v}", "lib", f"python{ha_updater.sys.version_info[0]}.{ha_updater.sys.version_info[1]}",
                                "site-packages", "homeassistant")
            os.makedirs(site)
            for f in (os.path.join(site, "__init__.py"), os.path.join(cfg, f"venv-{v}", ".ok")):
                open(f, "w").close()
            os.makedirs(os.path.join(cfg, f"venv-{v}", "bin"))
            open(os.path.join(cfg, f"venv-{v}", "bin", "python"), "w").close()
        self.emitted = []
        self.notified = []
        self.later = []
        for p in (mock.patch.object(ha_updater, "HA_VERSION", NEW), mock.patch.object(views, "HA_VERSION", NEW),
                  mock.patch.object(ha_updater.events, "emit", side_effect=lambda kind, msg, **kw: self.emitted.append(msg)),  # views' too
                  mock.patch("homeassistant.components.persistent_notification.async_create",
                             side_effect=lambda hass, msg, **kw: self.notified.append(msg))):
            p.start()
            self.addCleanup(p.stop)

        async def executor(fn, *args):
            return fn(*args)

        self.entries = {}
        self.hass = SimpleNamespace(
            config=SimpleNamespace(config_dir=cfg, path=lambda *p: os.path.join(cfg, *p), components={"demo"}),
            async_add_executor_job=executor, is_running=True,
            config_entries=SimpleNamespace(async_get_entry=lambda i: self.entries.get(i)),
            loop=SimpleNamespace(call_later=lambda delay, cb: self.later.append(delay) or mock.Mock()),
            async_create_task=lambda coro: coro.close())
        self.updater = ha_updater.HaUpdater(self.hass)
        self.settings = {"smoke_test_s": 300, "auto_rollback": True}
        self.installer = SimpleNamespace(
            hass=self.hass, busy=False, running="demo", SETUP_WAIT_S=900, health_source=None,
            health=lambda: {"state": "ok"},
            settings=SimpleNamespace(int_=lambda key, *_a: self.settings[key], bool_=lambda key: self.settings[key]),
            restart=mock.AsyncMock(return_value={"ok": True}))

    def write(self, state):
        with open(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def state(self):
        with open(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def first_boot(self, entries=("e1",), loaded=True, mode="keep"):
        """ha.json as the first boot of NEW finds it after a keep switch from OLD."""
        state = {"current": NEW, "desired": NEW, "previous": OLD, "proven": "", "boot_failures": 1,
                 "change": {"to": NEW, "mode": mode, "backup": "pre-ha.zip", "at": "2026-10-12T10:00:00", "applied": True,
                            "smoke": {"domain": "demo", "from": OLD, "entries": list(entries), "loaded": loaded}}}
        self.write(state)
        asyncio.run(self.updater.async_start_smoke(self.installer, state))

    def check(self):
        asyncio.run(self.updater.async_smoke_check())


class SwitchRecordsTest(_Base):
    def change(self, mode="keep"):
        cfg = self.cfg

        async def async_backup(label=""):
            return backupkit.create(cfg, label)

        inst = SimpleNamespace(hass=self.hass, busy=False, running="demo", running_tag="v2",
                               _entries_of=lambda d: [_entry("e1"), _entry("e2", state="setup_error"), _entry("e3", disabled_by="user")],
                               settings=SimpleNamespace(backup_keep=50), async_backup=async_backup,
                               protected_backups=lambda: set(), min_ha_of=lambda *_a: None, state=SimpleNamespace(rollback_backup=None))
        with mock.patch.object(self.views, "HA_VERSION", OLD):
            result = asyncio.run(self.views.async_change_ha_version(inst, self.updater, NEW, mode, "test"))
        return result, self.state()

    def test_a_keep_switch_records_the_entries_loaded_now(self):
        _result, state = self.change()
        self.assertEqual(state["change"]["smoke"], {"domain": "demo", "from": OLD, "entries": ["e1"], "loaded": True})

    def test_a_switch_back_to_a_version_that_was_left_warns(self):
        self.write({"current": OLD, "smoke_fallback": {"from": NEW, "to": OLD, "domain": "demo", "reason": "x", "at": "t"}})
        result, _state = self.change()
        self.assertTrue(any(f"Home Assistant {NEW} was left automatically" in w for w in result["warnings"]), result["warnings"])


class BootTest(_Base):
    def test_the_first_boot_of_the_change_schedules_the_smoke_test_and_records_it(self):
        self.first_boot()
        self.assertEqual(self.later, [300])
        smoke = self.state()["smoke"]
        self.assertEqual((smoke["version"], smoke["from"], smoke["entries"], smoke["backup"]), (NEW, OLD, ["e1"], "pre-ha.zip"))

    def test_a_restart_before_the_verdict_resumes_it(self):
        self.first_boot()
        state = self.state()
        state.pop("change")  # run.py settled the change at the first boot
        self.later.clear()
        updater = self.ha_updater.HaUpdater(self.hass)
        asyncio.run(updater.async_start_smoke(self.installer, state))
        self.assertEqual(self.later, [300])

    def test_restore_and_rebuild_are_not_smoke_tested(self):
        for mode in ("restore", "rebuild"):
            self.later.clear()
            self.first_boot(mode=mode)
            self.assertEqual(self.later, [], mode)
            self.assertNotIn("smoke", self.state())

    def test_the_smoke_test_off_means_none_here_either(self):
        self.settings["smoke_test_s"] = 0
        self.first_boot()
        self.assertEqual(self.later, [])
        self.assertNotIn("smoke", self.state())

    def test_a_boot_of_another_version_drops_the_record(self):
        self.write({"current": OLD, "smoke": {"version": "2026.11.0", "domain": "demo"}})
        asyncio.run(self.updater.async_start_smoke(self.installer, self.state()))
        self.assertEqual(self.later, [])
        self.assertNotIn("smoke", self.state())


class VerdictTest(_Base):
    def test_an_entry_that_does_not_set_up_switches_back_once(self):
        self.entries["e1"] = _entry("e1", state="setup_error", title="Evohome", reason="AttributeError: vol")
        self.first_boot()
        self.check()
        state = self.state()
        self.assertEqual((state["desired"], state["fallback_from"]), (OLD, NEW))
        self.assertEqual(state["recovery"]["for"], OLD)
        self.assertEqual(state["recovery"]["backup"], "pre-ha.zip")
        self.assertEqual((state["smoke_fallback"]["from"], state["smoke_fallback"]["to"]), (NEW, OLD))
        self.assertIn("setup_error", state["smoke_fallback"]["reason"])
        self.assertIn(f"switched back to Home Assistant {OLD}", state["last_error"])
        self.assertNotIn("smoke", state)
        self.assertNotIn("change", state)
        self.assertEqual(backupkit.pending_for_version(self.cfg), OLD, "the configuration from before the switch is not scheduled")
        self.installer.restart.assert_awaited_once()
        self.assertTrue(any("switched back" in m for m in self.emitted))

    def test_an_entry_that_loads_keeps_the_version(self):
        self.entries["e1"] = _entry("e1")
        self.first_boot()
        self.check()
        state = self.state()
        self.assertEqual(state["desired"], NEW)
        self.assertNotIn("smoke", state)
        self.assertNotIn("fallback_from", state)
        self.assertFalse(backupkit.pending(self.cfg))
        self.installer.restart.assert_not_awaited()
        self.assertEqual(self.notified, [])

    def test_a_degraded_integration_is_kept_and_reported(self):
        self.entries["e1"] = _entry("e1")
        self.installer.health = lambda: {"state": "degraded", "reason": "3 of 10 entities unavailable"}
        self.first_boot()
        self.check()
        self.assertEqual(self.state()["desired"], NEW)
        self.installer.restart.assert_not_awaited()
        self.assertTrue(self.notified and "degraded" in self.notified[0], self.notified)

    def test_an_entry_still_setting_up_is_waited_for(self):
        self.entries["e1"] = _entry("e1", state="setup_in_progress")
        self.first_boot()
        self.later.clear()
        self.check()
        self.assertEqual(self.later, [self.ha_updater.SMOKE_RECHECK_S])
        self.assertEqual(self.state()["desired"], NEW)
        self.installer.restart.assert_not_awaited()

    def test_an_entry_retrying_gets_one_more_interval(self):
        self.entries["e1"] = _entry("e1", state="setup_retry")
        self.first_boot()
        self.check()
        self.assertEqual(self.state()["desired"], NEW)
        self.check()
        self.assertEqual(self.state()["desired"], OLD)
        self.installer.restart.assert_awaited_once()

    def test_without_auto_rollback_it_is_reported_and_kept(self):
        self.settings["auto_rollback"] = False
        self.entries["e1"] = _entry("e1", state="setup_error")
        self.first_boot()
        self.check()
        self.assertEqual(self.state()["desired"], NEW)
        self.installer.restart.assert_not_awaited()
        self.assertTrue(self.notified and "automatic rollback is off" in self.notified[0])

    def test_without_the_previous_venv_it_is_reported_and_kept(self):
        shutil.rmtree(os.path.join(self.cfg, f"venv-{OLD}"))
        self.entries["e1"] = _entry("e1", state="setup_error")
        self.first_boot()
        self.check()
        self.assertEqual(self.state()["desired"], NEW)
        self.installer.restart.assert_not_awaited()
        self.assertTrue(self.notified and "no longer installed" in self.notified[0])

    def test_a_switch_scheduled_meanwhile_wins(self):
        self.entries["e1"] = _entry("e1", state="setup_error")
        self.first_boot()
        state = self.state()
        state["desired"] = "2026.10.1"
        self.write(state)
        self.check()
        self.assertEqual(self.state()["desired"], "2026.10.1")
        self.assertNotIn("smoke", self.state())
        self.installer.restart.assert_not_awaited()

    def test_a_removed_or_disabled_entry_is_not_the_version_s_doing(self):
        self.entries["e1"] = _entry("e1", state="not_loaded", disabled_by="user")
        self.first_boot(entries=("e1", "gone"))
        self.check()
        self.assertEqual(self.state()["desired"], NEW)
        self.installer.restart.assert_not_awaited()

    def test_after_the_fallback_the_version_gone_back_to_is_not_smoke_tested(self):
        self.entries["e1"] = _entry("e1", state="setup_error")
        self.first_boot()
        self.check()
        state = self.state()  # the boot of OLD: no change and no smoke record, so nothing to test (no loop)
        self.later.clear()
        with mock.patch.object(self.ha_updater, "HA_VERSION", OLD):
            asyncio.run(self.ha_updater.HaUpdater(self.hass).async_start_smoke(self.installer, state))
        self.assertEqual(self.later, [])

    def test_a_yaml_only_integration_that_no_longer_loads_switches_back(self):
        self.hass.config.components = set()
        self.first_boot(entries=(), loaded=True)
        self.check()
        self.assertEqual(self.state()["desired"], OLD)


class EntrypointAfterTheFallbackTest(EntrypointMainBase):
    """The boot after the manager switched back from B to A (here B is the version that was left)."""

    def fallen_back(self, **extra):
        state = {"current": B, "desired": A, "previous": A, "proven": B, "fallback_from": B, "boot_failures": 0,
                 "recovery": {"backup": "pre.zip", "from": B, "for": A, "parts": ["storage"], "at": "2026-10-12T10:00:00"},
                 "last_restore": {"ok": True, "for_version": A, "backup": "pre.zip", "at": "2026-10-12T10:00:01", "parts": ["storage"]},
                 "smoke_fallback": {"from": B, "to": A}}
        state.update(extra)
        self.write(state)

    def test_it_boots_the_version_before_the_switch_and_offers_no_rollback_to_the_one_left(self):
        self.fallen_back()
        self.assertEqual(self.boot(), A)
        state = self.state()
        self.assertEqual((state["current"], state["fallback_from"]), (A, B))
        self.assertNotIn("previous", state)
        self.assertNotIn("recovery", state)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, f"venv-{B}")), "the venv left was pruned before A booted")

    def test_a_restore_that_did_not_happen_stays_on_the_version_left(self):
        self.fallen_back(last_restore={})
        self.assertEqual(self.boot(), B)
        self.assertEqual(self.state()["desired"], B)

    def test_a_version_that_fails_to_boot_still_takes_the_crash_path(self):
        self.write({"current": B, "desired": B, "previous": A, "proven": A, "boot_failures": 3,
                    "change": {"to": B, "mode": "keep", "smoke": {"domain": "demo", "from": A, "entries": ["e1"], "loaded": True}}})
        self.assertEqual(self.boot(), A)
        state = self.state()
        self.assertEqual(state["fallback_from"], B)
        self.assertIn("crashed at boot", state["last_error"])


if __name__ == "__main__":
    unittest.main()
