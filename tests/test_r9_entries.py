"""External review at ceda6de (checked again at f76ba30): an entry Home Assistant refuses to unload is still
recorded as suspended, ha.json is written off the loop, Cancel restore leaves a version change's own restore
alone, the rollback backup's protection ends only with its own restore, and anchored names end at \\Z."""

import ast
import asyncio
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import backupkit
from homeassistant import config_entries as ce, core, loader
from homeassistant.helpers import device_registry as dr, entity_registry as er

import custom_components.integration_manager as im
from custom_components.integration_manager import backup_views, views
from custom_components.integration_manager.ha_updater import HaUpdater
from custom_components.integration_manager.installer import Installer, State

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "custom_components", "integration_manager")


class _RealEntries:
    """Home Assistant's own ConfigEntries (with the registries it touches) and one entry in a given state:
    async_set_disabled_by runs exactly as in 2026.9, setting disabled_by before the unload that raises."""

    async def __aenter__(self):
        self.hass = core.HomeAssistant(tempfile.mkdtemp())
        loader.async_setup(self.hass)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.hass.config_entries = ce.ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        return self

    def add(self, state, domain="demo", disabled_by=None):
        entry = ce.ConfigEntry(domain=domain, title="Demo", data={}, source="user", version=1, minor_version=1, options={},
                               unique_id=None, discovery_keys={}, subentries_data=None, state=state, disabled_by=disabled_by)
        self.hass.config_entries._entries[entry.entry_id] = entry  # noqa: SLF001
        self.hass.config.components.add(domain)  # loaded: the reload goes through the unload
        return entry

    async def __aexit__(self, *exc):
        await self.hass.async_stop(force=True)

    def installer(self, running=None):
        inst = object.__new__(Installer)
        inst.hass = self.hass
        inst.state = State(installed={"demo": {"versions": {"1.0": {}}, "running_tag": "1.0"}}, domain=running, suspended_entries=[])
        inst._entries_of = lambda d: self.hass.config_entries.async_entries(d)
        inst.saves = []
        inst._save_state = lambda: inst.saves.append(list(inst.state.suspended_entries or []))
        return inst


class SuspendedWhenUnloadRefusedTest(unittest.IsolatedAsyncioTestCase):
    """F1"""

    async def test_stop_records_an_entry_home_assistant_disabled_but_could_not_unload(self):
        async with _RealEntries() as ha:
            entry = ha.add(ce.ConfigEntryState.MIGRATION_ERROR)
            inst = ha.installer(running="demo")
            with self.assertRaises(RuntimeError) as ctx:
                await inst._disable_entries("demo")
            self.assertIs(entry.disabled_by, ce.ConfigEntryDisabler.USER)  # what HA saved
            self.assertEqual(inst.state.suspended_entries, [entry.entry_id])
            self.assertIn([entry.entry_id], inst.saves)
            self.assertIn("is disabled but did not unload", str(ctx.exception))
            self.assertTrue(inst.state.restart_required)

    async def test_entry_created_for_another_integration_is_recorded_when_its_unload_raises(self):
        async with _RealEntries() as ha:
            entry = ha.add(ce.ConfigEntryState.SETUP_IN_PROGRESS, domain="other")
            inst = ha.installer(running="demo")
            note = await im.async_disable_foreign_entry(inst, {"handler": "other", "result": entry})
            self.assertIs(entry.disabled_by, ce.ConfigEntryDisabler.USER)
            self.assertEqual(inst.state.suspended_entries, [entry.entry_id])
            self.assertIn("did not unload", note)

    async def test_resumed_at_the_next_start(self):
        async with _RealEntries() as ha:
            entry = ha.add(ce.ConfigEntryState.MIGRATION_ERROR)
            inst = ha.installer(running="demo")
            with self.assertRaises(RuntimeError):
                await inst._disable_entries("demo")
            entry._async_set_state(ha.hass, ce.ConfigEntryState.NOT_LOADED, None)  # what the process restart leaves
            ha.hass.config.components.remove("demo")
            with mock.patch.object(ha.hass.config_entries, "async_reload", mock.AsyncMock(return_value=True)):
                self.assertEqual(await inst._enable_entries("demo"), [entry.entry_id])
            self.assertIsNone(entry.disabled_by)

    async def test_an_entry_the_user_disabled_is_not_recorded(self):
        async with _RealEntries() as ha:
            entry = ha.add(ce.ConfigEntryState.NOT_LOADED, disabled_by=ce.ConfigEntryDisabler.USER)
            inst = ha.installer()
            self.assertTrue(await inst.async_suspend_entry(entry))
            self.assertEqual(inst.state.suspended_entries, [])

    async def test_an_entry_that_is_not_there_is_not_recorded(self):
        async with _RealEntries() as ha:
            inst = ha.installer()
            note = await im.async_disable_foreign_entry(inst, {"handler": "other", "result": SimpleNamespace(entry_id="gone", disabled_by=None)})
            self.assertIn("could not be disabled", note)
            self.assertEqual(inst.state.suspended_entries, [])


_BLOCKING_UPDATER = {"set_desired", "previous_version", "_read", "_read_for_update", "cancel_config_change", "_installed_venvs"}


def _loop_calls(path):
    """Calls of the blocking HaUpdater methods made directly in a coroutine (not in a lambda or a def run elsewhere)."""
    out = []

    def walk(node, on_loop):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                walk(child, False)
            else:
                if on_loop and isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in _BLOCKING_UPDATER:
                    out.append(f"{os.path.basename(path)}:{child.lineno} {child.func.attr}()")
                walk(child, on_loop)

    with open(path, encoding="utf-8") as fh:
        walk(ast.parse(fh.read()), False)
    return out


class HaJsonOffTheLoopTest(unittest.IsolatedAsyncioTestCase):
    """F4"""

    def test_no_view_reads_or_writes_ha_json_on_the_loop(self):
        found = [c for name in ("views.py", "backup_views.py", "build_views.py", "__init__.py") for c in _loop_calls(os.path.join(PKG, name))]
        self.assertEqual(found, [])

    def _updater(self, cfg, state):
        loop = asyncio.get_running_loop()
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg, path=lambda *p: os.path.join(cfg, *p)),
                               async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a))
        os.makedirs(os.path.join(cfg, "integration_manager"), exist_ok=True)
        with open(os.path.join(cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        up = HaUpdater(hass)
        on_loop = []
        for name in ("set_desired", "previous_version"):
            real = getattr(up, name)

            def spy(*a, _real=real, _name=name, **k):
                on_loop.append((_name, threading.current_thread() is threading.main_thread()))
                return _real(*a, **k)

            setattr(up, name, spy)
        return hass, up, on_loop

    @staticmethod
    def _request(body):
        async def body_json():
            return body

        return SimpleNamespace(content_type="application/json", json=body_json)

    async def test_cancel_by_choosing_the_running_version(self):
        cfg = tempfile.mkdtemp()
        hass, up, on_loop = self._updater(cfg, {"desired": "2099.1.0", "current": views.HA_VERSION})
        view = views.HaActionView(up, SimpleNamespace(hass=hass, busy=False))
        view.json = lambda d: d
        locked = []
        real = up.set_desired
        up.set_desired = lambda *a: (locked.append(views._HA_CHANGE_LOCK.locked()), real(*a))[1]
        r = await view.post(self._request({"version": views.HA_VERSION}), "update")
        self.assertTrue(r["ok"], r)
        self.assertEqual(on_loop, [("set_desired", False)])
        self.assertEqual(locked, [True])  # a change scheduled while the executor writes cannot slip in between

    async def test_rollback_reads_and_writes_in_the_executor(self):
        cfg = tempfile.mkdtemp()
        hass, up, on_loop = self._updater(cfg, {"current": views.HA_VERSION, "previous": "2026.1.0"})
        inst = SimpleNamespace(hass=hass, busy=False, running=None, async_backup=mock.AsyncMock(return_value={"name": "b.zip"}),
                               settings=SimpleNamespace(backup_keep=5), protected_backups=set)
        view = views.HaActionView(up, inst)
        view.json = lambda d: d
        with mock.patch.object(backupkit, "prune", return_value=[]):
            r = await view.post(self._request({}), "rollback")
        self.assertTrue(r["ok"], r)
        self.assertEqual(on_loop, [("previous_version", False), ("set_desired", False)])


def _schedule(cfg, for_version):
    os.makedirs(os.path.join(cfg, backupkit.STATE_DIR), exist_ok=True)
    zip_name = "restore-pending-x.zip"
    with open(os.path.join(cfg, backupkit.STATE_DIR, zip_name), "wb") as fh:
        fh.write(b"PK")
    with open(os.path.join(cfg, backupkit.PENDING_META), "w", encoding="utf-8") as fh:
        json.dump({"name": "b.zip", "parts": ["storage"], "zip": zip_name, "for_version": for_version}, fh)


def _ha_json(cfg, data):
    with open(os.path.join(cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class CancelRestoreTest(unittest.TestCase):
    """F16"""

    def _cancel(self, cfg):
        async def executor(fn, *args):
            return fn(*args)

        view = backup_views.RestoreCancelView(SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=executor))
        view.json = lambda d: d
        return asyncio.run(backup_views.RestoreCancelView.post.__wrapped__(view, None, {}))

    def test_a_version_change_keeps_its_own_restore(self):
        cfg = tempfile.mkdtemp()
        _schedule(cfg, "2026.1.0")
        _ha_json(cfg, {"desired": "2026.1.0", "change": {"to": "2026.1.0", "mode": "restore", "backup": "pre.zip"}})
        r = self._cancel(cfg)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["for_version"], "2026.1.0")
        self.assertTrue(backupkit.pending(cfg))

    def test_a_restore_scheduled_by_hand_is_cancelled(self):
        cfg = tempfile.mkdtemp()
        _schedule(cfg, None)
        _ha_json(cfg, {"desired": "2026.1.0", "change": {"to": "2026.1.0", "mode": "keep", "backup": "pre.zip"}})
        self.assertEqual(self._cancel(cfg), {"ok": True, "cancelled": True})
        self.assertFalse(backupkit.pending(cfg))

    def test_a_leftover_of_a_switch_no_longer_scheduled_is_cancelled(self):
        cfg = tempfile.mkdtemp()
        _schedule(cfg, "2026.1.0")
        _ha_json(cfg, {"desired": "2026.9.2"})
        self.assertEqual(self._cancel(cfg), {"ok": True, "cancelled": True})
        self.assertFalse(backupkit.pending(cfg))


class RollbackBackupProtectionTest(unittest.TestCase):
    """F17"""

    def _installer(self, backup="pre.zip", at="2026-09-16T10:00:00"):
        inst = object.__new__(Installer)
        inst.state = State(rollback_backup=backup, rollback_at=at)
        inst._save_state = lambda: None
        return inst

    def test_an_older_restore_does_not_release_it(self):
        inst = self._installer()
        for last in ({"ok": True, "backup": "other.zip", "at": "2026-09-01T08:00:00"},  # a restore of another day
                     {"ok": True, "backup": "other.zip", "at": "2026-09-16T11:00:00"},  # another backup, after
                     {"ok": True, "backup": "pre.zip", "at": "2026-09-10T08:00:00"},  # this backup, before the rollback
                     {"ok": False, "backup": "pre.zip", "at": "2026-09-16T11:00:00"}):  # its restore failed
            with self.subTest(last=last):
                self.assertFalse(inst.release_rollback_backup(last))
                self.assertEqual(inst.state.rollback_backup, "pre.zip")
                self.assertIn("pre.zip", Installer.protected_backups(SimpleNamespace(state=inst.state, state_dir=tempfile.mkdtemp())))

    def test_its_own_restore_releases_it(self):
        inst = self._installer()
        self.assertTrue(inst.release_rollback_backup({"ok": True, "backup": "pre.zip", "at": "2026-09-16T10:00:05"}))
        self.assertEqual((inst.state.rollback_backup, inst.state.rollback_at), (None, None))

    def test_a_volume_from_before_the_time_goes_by_the_name(self):
        inst = self._installer(at=None)
        self.assertFalse(inst.release_rollback_backup({"ok": True, "backup": "other.zip", "at": "2026-09-16T11:00:00"}))
        self.assertTrue(inst.release_rollback_backup({"ok": True, "backup": "pre.zip", "at": "2026-09-01T08:00:00"}))

    def test_boot_uses_it(self):
        with open(os.path.join(PKG, "__init__.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertTrue("installer.release_rollback_backup(last_restore)" in src and "installer.state.rollback_backup = None" not in src,
                        "async_setup releases the rollback backup on its own terms")

    def test_state_file_round_trip(self):
        d = tempfile.mkdtemp()
        inst = object.__new__(Installer)
        inst.state_file = os.path.join(d, "state.json")
        with open(inst.state_file, "w", encoding="utf-8") as fh:
            json.dump({"installed": {}, "rollback_backup": "pre.zip", "rollback_at": ["x"]}, fh)
        state = Installer._load_state(inst)
        self.assertEqual((state.rollback_backup, state.rollback_at), ("pre.zip", None))


class AnchoredNamesTest(unittest.TestCase):
    """F22: "$" also matches before a trailing newline"""

    def test_trailing_newline_refused(self):
        from custom_components.integration_manager import ha_updater

        for rx, value in ((views._REPO_RE, "o/r"), (views._TAG_RE, "1.0"), (backup_views._NAME_RE, "b.zip"), (ha_updater._STABLE, "2026.9.2")):
            with self.subTest(rx=rx.pattern):
                self.assertIsNotNone(rx.match(value))
                self.assertIsNone(rx.match(value + "\n"))
        self.assertFalse(backup_views._name_ok("b.zip\n"))


class TagCheckedByTheInstallerTest(unittest.TestCase):
    """cleanup: install, start and remove_version check the tag themselves"""

    def test_bad_tags_refused_before_anything_happens(self):
        inst = object.__new__(Installer)
        inst.config_dir = tempfile.mkdtemp()
        inst.state = State(installed={"demo": {"versions": {"1.0": {}}}})
        inst.spec = lambda d: {}
        for call in (lambda: inst.install("../x", domain="demo"), lambda: inst.install("1.0", domain="demo", archive_ref="a\nb"),
                     lambda: inst.start("demo", "1.0\n"), lambda: inst.remove_version("demo", "..")):
            r = asyncio.run(call())
            self.assertFalse(r["ok"])
            self.assertIn("invalid tag", r["error"])


if __name__ == "__main__":
    unittest.main()
