"""Edge campaign: a registry.json of the wrong type must not crash-loop the
container, a damaged state.json must be visible and reconciled, saves on a full
volume answer JSON, and a start says what it verified."""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import run
from custom_components.integration_manager import installer as inst_mod, manage_views, views
from custom_components.integration_manager.installer import Installer, State

CORRUPT_STATE_KEEP = getattr(inst_mod, "CORRUPT_STATE_KEEP", 3)  # so the file also loads against a build without it


def _tmp(test):
    d = tempfile.mkdtemp(prefix="hri-camp-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _request(body=None, headers=None):
    return SimpleNamespace(headers=headers or {}, query={}, content_type="application/json",
                           json=mock.AsyncMock(return_value=body if body is not None else {}))


def _body(resp):
    return json.loads(resp.body)


BAD_REGISTRIES = {
    "a list of integrations": '{"integrations": [1, 2, 3]}',
    "a number of integrations": '{"integrations": 5}',
    "a list": "[]",
    "a non-empty list": '["demo"]',
    "a string": '"x"',
    "a number": "7",
}


# ----- M2: a wrong type in registry.json ----------------------------------------------------------

class RegistryOfTheWrongTypeTest(unittest.TestCase):
    """registry.json is documented as user-editable; every shape has to come up empty, not raise."""

    def installer(self, text):
        d = _tmp(self)
        _write(os.path.join(d, "integration_manager", "registry.json"), text)
        inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=d)))
        return inst

    def test_the_registry_reads_empty_and_says_so(self):
        for what, text in BAD_REGISTRIES.items():
            with self.subTest(what):
                with self.assertLogs("custom_components.integration_manager.installer", "ERROR") as log:
                    inst = self.installer(text)
                    self.assertEqual(inst.registry(), {})
                self.assertIn("registry.json is ignored", "".join(log.output))

    def test_a_good_registry_still_reads(self):
        inst = self.installer('{"integrations": {"demo": {"repo": "o/demo"}}}')
        self.assertEqual(inst.registry()["demo"]["repo"], "o/demo")

    def test_an_entry_can_still_be_added_over_a_damaged_file(self):
        inst = self.installer('{"integrations": [1, 2, 3]}')
        spec = inst.add_to_registry("demo", "owner/demo")
        self.assertEqual(spec["repo"], "owner/demo")
        self.assertEqual(json.loads(_read(inst.user_registry_file))["integrations"]["demo"]["repo"], "owner/demo")

    def test_a_damaged_file_is_kept_beside_the_new_one(self):
        for text in ('{"integrations": {"mine": {"repo": "o/mine"},}}', '{"integrations": [1, 2, 3]}'):
            with self.subTest(text=text):
                inst = self.installer(text)
                with self.assertLogs("custom_components.integration_manager.installer", "WARNING"):
                    inst.add_to_registry("demo", "owner/demo")
                kept = [n for n in os.listdir(inst.state_dir) if n.startswith("registry.json.corrupt-")]
                self.assertEqual(len(kept), 1)
                self.assertEqual(_read(os.path.join(inst.state_dir, kept[0])), text)

    def test_the_boot_reads_quiet_loggers_without_crashing(self):
        """run.py:_quiet_loggers ran before the event loop: its AttributeError killed the process with an
        empty log, and the entrypoint kept retrying the boot for ever."""
        for what, text in BAD_REGISTRIES.items():
            with self.subTest(what):
                d = _tmp(self)
                _write(os.path.join(d, "integration_manager", "registry.json"), text)
                _write(os.path.join(d, "integration_manager", "state.json"),
                       json.dumps({"domain": "demo", "installed": {"demo": {"versions": {}}}}))
                with mock.patch.object(run, "CONFIG_DIR", d), self.assertLogs("hass_remote_integration", "ERROR"):
                    self.assertEqual(run._quiet_loggers(), ["custom_components.demo"])

    def test_the_configured_quiet_loggers_still_win(self):
        d = _tmp(self)
        _write(os.path.join(d, "integration_manager", "registry.json"),
               json.dumps({"integrations": {"demo": {"repo": "o/d", "quiet_loggers": ["demo.chatty"]}}}))
        _write(os.path.join(d, "integration_manager", "state.json"), json.dumps({"domain": "demo", "installed": {}}))
        with mock.patch.object(run, "CONFIG_DIR", d):
            self.assertEqual(run._quiet_loggers(), ["demo.chatty"])


class BootLogsWhatKilledItTest(unittest.TestCase):
    """Whatever ends the boot has to reach the log: _exit is what flushes the queue, so an exception that
    escapes main() used to leave docker logs, process.log and ha-install.log with nothing to go on."""

    def test_a_crash_before_the_loop_is_logged_and_flushed(self):
        with mock.patch.object(run, "_quiet_loggers", side_effect=AttributeError("'list' object has no attribute 'get'")), \
                mock.patch.object(run, "logbuffer", mock.Mock()), mock.patch.object(run, "_install_excepthooks"), \
                mock.patch.object(run, "_install_import_tracer"), mock.patch.object(run, "faulthandler"), \
                mock.patch.object(run, "_exit") as exit_, \
                self.assertLogs("hass_remote_integration", "CRITICAL") as log:
            run.main()
        self.assertEqual(exit_.call_args.args, (1,))  # a crash is a failed boot, and _exit flushes the log queue
        self.assertIn("'list' object has no attribute 'get'", "".join(log.output))


# ----- M5 / m1 / m10: a damaged state.json --------------------------------------------------------

class _Entry:
    def __init__(self, domain, entry_id="e1", disabled_by=None):
        self.domain, self.entry_id, self.disabled_by = domain, entry_id, disabled_by
        self.title = f"{domain} entry"


class _Entries:
    def __init__(self, entries):
        self._entries = entries

    def async_entries(self, domain=None):
        return [e for e in self._entries if domain in (None, e.domain)]


class DamagedStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = _tmp(self)
        self.state_dir = os.path.join(self.dir, "integration_manager")
        os.makedirs(self.state_dir)

    def installer(self, raw, entries=()):
        _write(os.path.join(self.state_dir, "state.json"), raw)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.dir, components=set()),
                               config_entries=_Entries(list(entries)))
        with self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            return Installer(hass)

    def copies(self):
        return sorted(n for n in os.listdir(self.state_dir) if n.startswith("state.json.corrupt-"))

    def test_a_valid_json_of_the_wrong_shape_is_kept_too(self):
        inst = self.installer(json.dumps({"domain": "demo"}))  # no "installed": the manager cannot use it
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(json.loads(_read(os.path.join(self.state_dir, self.copies()[0]))), {"domain": "demo"})
        self.assertIn("unknown layout", inst.state_load_error)

    def test_unparsable_json_is_kept_as_before(self):
        inst = self.installer("{broken")
        self.assertEqual(_read(os.path.join(self.state_dir, self.copies()[0])), "{broken")
        self.assertIn("not valid JSON", inst.state_load_error)

    def test_old_copies_are_pruned(self):
        for i in range(CORRUPT_STATE_KEEP + 3):
            _write(os.path.join(self.state_dir, f"state.json.corrupt-2026010{i}-000000"), str(i))
        self.installer("{broken")
        self.assertEqual(len(self.copies()), CORRUPT_STATE_KEEP)
        self.assertNotIn("state.json.corrupt-20260100-000000", self.copies())  # the oldest went first

    def test_a_running_integration_is_adopted_from_the_config_entries(self):
        inst = self.installer("{broken", entries=[_Entry("demo"), _Entry("integration_manager", "im")])
        _write(os.path.join(inst._version_dir("demo", "1.0"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._component_dir("demo"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._component_dir("demo"), ".hri-tag"), "1.0\n2026-09-16T09:00:00")
        inst._save_state = lambda: None
        self.assertEqual(inst._adopt_from_disk(), "demo")
        self.assertEqual(inst.state.domain, "demo")
        self.assertEqual(inst.running_tag, "1.0")
        self.assertEqual(list(inst.state.installed["demo"]["versions"]), ["1.0"])
        self.assertFalse(inst._ensure_deployed("demo", "1.0"))  # what it adopted matches what is deployed: no redeploy

    def test_a_stopped_integration_is_adopted_but_not_started(self):
        inst = self.installer("{broken", entries=[_Entry("demo", disabled_by="user")])
        _write(os.path.join(inst._version_dir("demo", "1.0"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._component_dir("demo"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._component_dir("demo"), ".hri-tag"), "1.0\n2026-09-16T09:00:00")
        inst._save_state = lambda: None
        self.assertEqual(inst._adopt_from_disk(), "demo")
        self.assertIn("demo", inst.state.installed)
        self.assertIsNone(inst.state.domain)  # its entries are disabled: it is installed, not running

    def test_two_domains_are_not_adopted(self):
        inst = self.installer("{broken", entries=[_Entry("demo"), _Entry("other", "e2")])
        for domain in ("demo", "other"):
            _write(os.path.join(inst._component_dir(domain), "manifest.json"), json.dumps({"domain": domain, "version": "1"}))
        inst._save_state = lambda: None
        with self.assertLogs("custom_components.integration_manager.installer", "ERROR"):
            self.assertIsNone(inst._adopt_from_disk())
        self.assertEqual(inst.state.installed, {})

    def test_the_loss_reaches_the_timeline_the_ui_and_a_notification(self):
        inst = self.installer("{broken", entries=[_Entry("demo")])
        _write(os.path.join(inst._version_dir("demo", "1.0"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._component_dir("demo"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._component_dir("demo"), ".hri-tag"), "1.0\n2026-09-16T09:00:00")
        inst._save_state = lambda: None
        with mock.patch.object(inst_mod.events, "emit") as emit, \
                mock.patch("homeassistant.components.persistent_notification.async_create") as note:
            inst._report_state_loss()
        self.assertEqual(emit.call_args.args[0], "error")
        self.assertIn("adopted demo 1.0", emit.call_args.args[1])
        self.assertIn("adopted demo 1.0", inst.state.last_error)  # the Overview's own error line
        self.assertEqual(note.call_args.args[1], inst.state.last_error)
        self.assertIsNone(inst.state_load_error)  # reported once, not at every reconcile

    def test_a_good_state_reports_nothing(self):
        _write(os.path.join(self.state_dir, "state.json"), json.dumps({"domain": None, "installed": {}}))
        inst = Installer(SimpleNamespace(config=SimpleNamespace(config_dir=self.dir, components=set()), config_entries=_Entries([])))
        self.assertIsNone(inst.state_load_error)
        self.assertEqual(self.copies(), [])


# ----- M4 / m2: a save on a full volume -----------------------------------------------------------

class FullVolumeAnswersJsonTest(unittest.TestCase):
    def test_settings_keep_memory_and_disk_together(self):
        st = SimpleNamespace(data={"backup_keep": 5}, async_save=mock.AsyncMock(side_effect=OSError(28, "No space left on device")),
                             public=lambda: {"backup_keep": 5}, github_headers=lambda: {})
        view = manage_views.SettingsView(SimpleNamespace(settings=st, _releases_cache={}, scheduler=None))
        res = _body(asyncio.run(view.post(_request({"backup_keep": 9}))))
        self.assertFalse(res["ok"])
        self.assertIn("No space left on device", res["error"])
        self.assertEqual(st.data["backup_keep"], 5)  # what the UI shows is what the file holds

    def test_a_saved_setting_still_answers_ok(self):
        st = SimpleNamespace(data={"backup_keep": 5}, async_save=mock.AsyncMock(), public=lambda: {"backup_keep": 9}, github_headers=lambda: {})
        view = manage_views.SettingsView(SimpleNamespace(settings=st, _releases_cache={}, scheduler=None))
        res = _body(asyncio.run(view.post(_request({"backup_keep": 9}))))
        self.assertTrue(res["ok"])
        self.assertEqual(st.data["backup_keep"], 9)

    def test_log_format_is_checked_off_the_event_loop(self):
        st = SimpleNamespace(data={}, async_save=mock.AsyncMock(), public=lambda: {}, github_headers=lambda: {})
        view = manage_views.SettingsView(SimpleNamespace(settings=st, _releases_cache={}, scheduler=None))
        threads = []
        real = manage_views.clean_log_format

        def spy(value):
            threads.append(threading.current_thread() is threading.main_thread())
            return real(value)

        with mock.patch.object(manage_views, "clean_log_format", spy):
            res = _body(asyncio.run(view.post(_request({"log_format": {"pattern": "^(?P<time>\\S+) (?P<message>.*)$"}}))))
        self.assertTrue(res["ok"], res)
        self.assertEqual(threads, [False])
        self.assertIn("log_format", st.data)

    def test_the_registry_post_says_why(self):
        async def job(func, *args):
            return await asyncio.get_running_loop().run_in_executor(None, func, *args)

        inst = SimpleNamespace(add_to_registry=mock.Mock(side_effect=OSError(28, "No space left on device")),
                               hass=SimpleNamespace(async_add_executor_job=job))
        res = _body(asyncio.run(views.RegistryView(inst).post(_request({"domain": "demo", "repo": "owner/demo"}))))
        self.assertFalse(res["ok"])
        self.assertIn("No space left on device", res["error"])


# ----- overlapping patch saves --------------------------------------------------------------------

class PatchSaveRaceTest(unittest.TestCase):
    """Two saves of the same patch shared one <name>.tmp: the second wrote through its open handle into the
    file the first had already renamed into place."""

    def setUp(self):
        self.dir = _tmp(self)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.dir),
                               async_add_executor_job=lambda fn, *a: self.loop.run_in_executor(None, fn, *a))
        self.view = manage_views.PatchEditView(hass, SimpleNamespace(running=None, running_tag=None))

    PATCH = "VALUE = {}\ndef apply(ctx):\n    pass\ndef status(ctx):\n    pass\n"

    def save(self, value):
        return self.view.post(_request({"name": "p.py", "text": self.PATCH.format(value)}, {"X-Requested-With": "fetch"}), domain="demo", op="save")

    def test_each_save_writes_its_own_temporary_file(self):
        seen = []
        real = os.replace

        def replace(src, dst, *a, **kw):
            seen.append(src)
            return real(src, dst, *a, **kw)

        with mock.patch.object(manage_views.os, "replace", replace):
            for value in (1, 2):
                self.assertTrue(_body(self.loop.run_until_complete(self.save(value)))["ok"])
        self.assertEqual(len(set(seen)), 2, "every save must write through a temporary file of its own")

    def test_overlapping_saves_do_not_interleave(self):
        order, started, real = [], threading.Event(), os.replace

        def replace(src, dst, *a, **kw):
            order.append("enter")
            if not started.is_set():  # hold the first save where the second used to walk into its temporary file
                started.set()
                time.sleep(0.3)
            out = real(src, dst, *a, **kw)
            order.append("exit")
            return out

        async def both():
            return [_body(r) for r in await asyncio.gather(self.save(1), self.save(2))]

        with mock.patch.object(manage_views.os, "replace", replace):
            results = self.loop.run_until_complete(both())
        self.assertEqual(order, ["enter", "exit", "enter", "exit"], "a second save ran inside the first one")
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertIn(_read(os.path.join(self.dir, "integration_manager", "patches", "demo", "p.py")),
                      [self.PATCH.format(v) for v in (1, 2)])


# ----- m11: what a start guarantees ---------------------------------------------------------------

def _yielding_hass(**kw):
    async def job(fn, *args):
        await asyncio.sleep(0)
        return fn(*args)
    return SimpleNamespace(async_add_executor_job=job, is_running=True, loop=mock.Mock(), async_create_task=mock.Mock(), **kw)


class StartVerdictTest(unittest.TestCase):
    """A start of the version that already runs deploys new code (a dev build, a repair) and used to schedule
    nothing: the entry could fail to set up afterwards and the Overview would still say it runs."""

    def installer(self, smoke_s=300):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        inst.state = State(domain="demo", installed={"demo": {"running_tag": "local", "previous_tag": None, "pre_update_backup": None,
                                                              "versions": {"local": {"installed_at": "one", "version": "1.0"}}}})
        _write(os.path.join(inst._version_dir("demo", "local"), "manifest.json"), json.dumps({"domain": "demo", "version": "1.0"}))
        _write(os.path.join(inst._version_dir("demo", "local"), "__init__.py"), "x = 1\n")
        inst._ensure_deployed("demo", "local")
        inst.busy = False
        inst.hass = _yielding_hass(config=SimpleNamespace(components=set()))
        inst.settings = SimpleNamespace(backup_keep=5, int_=lambda key, lo=0, hi=0: smoke_s, bool_=lambda key: False)
        inst._save_state = lambda: None
        inst._smoke_handle = inst._smoke_pending = None
        inst._smoke_waiting, inst._smoke_rechecked = {}, set()
        inst._loaded_tags, inst._code_hash = {}, {}
        inst._abandoned_switch, inst._restart_before_uninstall = {}, {}
        inst._requirements_for = mock.AsyncMock(return_value=[])
        inst._install_requirements = lambda reqs, force=False: []
        inst._apply_patches = lambda domain: "n/a"
        inst._loadable = mock.AsyncMock(return_value=True)
        inst._enable_entries = mock.AsyncMock(return_value=[])
        return inst

    def start(self, inst):
        with mock.patch.object(inst_mod.events, "emit"):
            return asyncio.run(inst.start("demo", "local"))

    def redeploy(self, inst):  # what a dev install does: new code under the same tag
        _write(os.path.join(inst._version_dir("demo", "local"), "__init__.py"), "x = 2\n")
        inst.state.installed["demo"]["versions"]["local"]["installed_at"] = "two"

    def test_a_redeploy_of_the_running_version_is_verified(self):
        inst = self.installer()
        self.redeploy(inst)
        res = self.start(inst)
        self.assertTrue(res["ok"])
        self.assertEqual(res["deployed"], True)
        self.assertEqual((res["smoke_test"] or {}).get("tag"), "local")
        self.assertEqual(inst.state.pending_smoke, {"domain": "demo", "tag": "local", "can_rollback": False})

    def test_a_start_that_changed_nothing_says_so(self):
        res = self.start(self.installer())
        self.assertTrue(res["ok"])
        self.assertFalse(res["deployed"])
        self.assertIsNone(res["smoke_test"])
        self.assertIn("no health verdict", res["note"])

    def test_a_dev_install_that_already_deployed_is_verified_too(self):
        """install_local deploys the running copy itself and asks for a restart: the start that follows sees
        nothing to deploy, and the new code would run after the restart with nobody looking at it."""
        inst = self.installer()
        inst.state.restart_required = True
        res = self.start(inst)
        self.assertFalse(res["deployed"])
        self.assertEqual(inst.state.pending_smoke, {"domain": "demo", "tag": "local", "can_rollback": False})

    def test_the_smoke_test_being_off_is_not_silence(self):
        inst = self.installer(smoke_s=0)
        self.redeploy(inst)
        res = self.start(inst)
        self.assertIsNone(res["smoke_test"])
        self.assertIn("only deployed", res["note"])


# ----- an interrupted full rollback and the restore it means -------------------------------------

class PendingRollbackCorrelationTest(unittest.TestCase):
    """The intent named the archive; a restore of that same archive from before the intent was written (a
    manual restore of the pre-update backup) passed for the rollback's own."""

    def installer(self, last_restore, intent_at="2026-09-16T12:00:00"):
        d = _tmp(self)
        inst = object.__new__(Installer)
        inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
        inst.versions_dir = os.path.join(inst.state_dir, "versions")
        _write(os.path.join(inst.state_dir, "ha.json"), json.dumps({"last_restore": last_restore}))
        inst.state = State(domain="demo", installed={"demo": {"running_tag": "2.0", "versions": {"1.0": {}, "2.0": {}}}},
                           pending_rollback={"domain": "demo", "tag": "1.0", "backup": "pre.zip", "at": intent_at})
        inst._save_state = lambda: None
        return inst

    def apply(self, inst):
        with mock.patch.object(inst_mod.events, "emit") as emit:
            inst._apply_pending_rollback()
        return emit

    def test_an_older_restore_of_the_same_archive_is_not_this_rollback(self):
        inst = self.installer({"ok": True, "backup": "pre.zip", "at": "2026-09-16T08:00:00"})
        with self.assertLogs("custom_components.integration_manager.installer", "WARNING"):
            emit = self.apply(inst)
        self.assertEqual(inst.state.installed["demo"]["running_tag"], "2.0")  # nothing was restored: no rollback
        self.assertEqual(emit.call_args.args[0], "error")
        self.assertIsNone(inst.state.pending_rollback)

    def test_the_restore_of_this_rollback_finishes_it(self):
        inst = self.installer({"ok": True, "backup": "pre.zip", "at": "2026-09-16T12:00:05"})
        emit = self.apply(inst)
        self.assertEqual(inst.state.installed["demo"]["running_tag"], "1.0")
        self.assertEqual(emit.call_args.args[0], "rollback")

    def test_an_intent_from_an_older_version_is_still_trusted(self):
        inst = self.installer({"ok": True, "backup": "pre.zip", "at": "2026-09-16T08:00:00"}, intent_at=None)
        inst.state.pending_rollback.pop("at")
        self.apply(inst)
        self.assertEqual(inst.state.installed["demo"]["running_tag"], "1.0")


if __name__ == "__main__":
    unittest.main()
