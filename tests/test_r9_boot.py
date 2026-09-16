"""Review round 9, boot and import part: the import's unpack budget, the pre-HA status page, a restore during a
pending clean start, the preflight's pip process group."""

import asyncio
import gzip
import io
import json
import os
import shutil
import socket
import signal
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

import backupkit
from tests.test_review_backup import _entrypoint, _volume


def _tmp(test):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _header(name, size, typ=tarfile.REGTYPE):
    ti = tarfile.TarInfo(name)
    ti.size, ti.type = size, typ
    return ti.tobuf(format=tarfile.USTAR_FORMAT)


def _write_member(out, name, size, typ=tarfile.REGTYPE, fill=None):
    """A member written block by block: a test archive of tens of MB is never held in memory."""
    out.write(_header(name, size, typ))
    left = size
    while left:
        k = min(left, 1 << 20)
        out.write(fill(k) if fill else bytes(k))
        left -= k
    out.write(bytes((512 - size % 512) % 512))


def _ha_backup(cfg, members, protected=False):
    """integration_manager/import.tar as Home Assistant writes it; ``members``: (name, size, type, fill) in the
    inner homeassistant.tar.gz, after the config entries of a "demo" integration."""
    from custom_components.integration_manager import ha_import

    inner = os.path.join(cfg, "inner.tar.gz")
    entries = json.dumps({"version": 1, "data": {"entries": [{"entry_id": "abc", "domain": "demo", "title": "Demo",
                                                              "data": {}, "options": {}}]}}).encode()
    with gzip.open(inner, "wb", compresslevel=6) as g:
        _write_member(g, "data/.storage/core.config_entries", len(entries), fill=lambda k: entries[:k])
        for name, size, typ, fill in members:
            _write_member(g, name, size, typ, fill)
        _write_member(g, "data/.storage/core.entity_registry", 0)
        g.write(bytes(1024))
    path = os.path.join(cfg, ha_import.IMPORT_TAR)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tarfile.open(path, "w") as tf:
        meta = json.dumps({"name": "t", "compressed": True, "protected": protected}).encode()
        ti = tarfile.TarInfo("backup.json")
        ti.size = len(meta)
        tf.addfile(ti, io.BytesIO(meta))
        tf.add(inner, "homeassistant.tar.gz")
    os.remove(inner)


class ImportUnpackBudgetTest(unittest.TestCase):
    """F3: members the import skips were inflated without counting toward any limit."""

    def setUp(self):
        from custom_components.integration_manager import ha_import

        self.ha_import = ha_import
        self.cfg = _tmp(self)

    def test_a_skipped_member_past_the_budget_is_refused_without_being_inflated(self):
        _ha_backup(self.cfg, [("data/www/huge.bin", 64 * 1024**2, tarfile.REGTYPE, None)])  # 64 MB of zeros, ~65 KB compressed
        headers = []
        real_next = tarfile.TarFile.next

        def spy(tar):
            member = real_next(tar)
            headers.append(member and member.name)
            return member

        with mock.patch.object(self.ha_import, "MAX_EXTRACT_BYTES", 8 * 1024**2), mock.patch.object(tarfile.TarFile, "next", spy), \
                self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertIn("unpacks to more than", str(ctx.exception))
        # stopped at the huge member's header: asking for the next header is what inflates its data
        self.assertEqual(headers[-1], "data/www/huge.bin")
        self.assertNotIn("data/.storage/core.entity_registry", headers)
        self.assertFalse(os.path.exists(os.path.join(self.cfg, self.ha_import.EXTRACT_DIR)))

    def test_a_large_database_that_compresses_like_real_data_is_still_read(self):
        # the recorder database is in every backup and is not what the import extracts: 16 MB that barely compresses
        _ha_backup(self.cfg, [("data/home-assistant_v2.db", 16 * 1024**2, tarfile.REGTYPE, os.urandom)])
        with mock.patch.object(self.ha_import, "MAX_EXTRACT_BYTES", 8 * 1024**2):
            summary = self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertEqual(list(summary["domains"]), ["demo"])

    def test_an_oversized_extended_header_is_refused(self):
        # tarfile reads a pax header's data into memory whole, inside next(): 8 MB here, gigabytes in a crafted upload
        _ha_backup(self.cfg, [("PaxHeader", 8 * 1024**2, tarfile.XHDTYPE, None), ("data/x", 0, tarfile.REGTYPE, None)])
        with self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertIn("extended tar header", str(ctx.exception))
        self.assertNotIn("wrong encryption key", str(ctx.exception))

    def test_an_oversized_long_name_header_is_refused_in_an_encrypted_backup_too(self):
        _ha_backup(self.cfg, [("././@LongLink", 8 * 1024**2, tarfile.GNUTYPE_LONGNAME, None), ("data/x", 0, tarfile.REGTYPE, None)],
                   protected=True)
        # stands in for securetar's decryption: what the import gets back is a tarfile it did not open itself
        with mock.patch.object(self.ha_import.securetar, "SecureTarFile", lambda path, gzip, password: tarfile.open(path, "r|gz")), \
                self.assertRaises(ValueError) as ctx:
            self.ha_import.inspect_backup(self.cfg, "key", {"demo"})
        self.assertIn("extended tar header", str(ctx.exception))

    def test_a_plain_backup_is_still_imported(self):
        _ha_backup(self.cfg, [("data/configuration.yaml", 100, tarfile.REGTYPE, None)])
        summary = self.ha_import.inspect_backup(self.cfg, None, {"demo"})
        self.assertEqual(summary["domains"]["demo"]["entries"][0]["entry_id"], "abc")


class HeldBootStatusTest(unittest.TestCase):
    """F13, F14: while a failed restore holds the boot, the page showed the last install's log (to anyone, without
    a password) and /api/ said installing: true for as long as the retries ran."""

    def setUp(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.cfg = _tmp(self)
        env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD", "HRI_PASSWORD_FILE")}
        env.update(HRI_CONFIG=self.cfg, HRI_PORT=str(self.port))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ep = _entrypoint(self, self.cfg)
        os.makedirs(self.ep.STATE_DIR)
        with open(self.ep.LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write("# install of Home Assistant 2026.9.2\nCollecting homeassistant==2026.9.2\n")

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.read().decode()

    def during_hold(self):
        """The page and /api/status, fetched from the real hold while it waits for its retry."""
        seen = {}

        def sleep(_s):
            seen["page"] = self.get("/")
            seen["api"] = self.get("/api/status")

        with mock.patch.object(self.ep.time, "sleep", sleep), mock.patch.object(backupkit, "pending", side_effect=[True, True]):
            self.ep.hold_after_failed_rollback({"ok": False, "recovery_source": "pre-restore.zip"}, lambda: {"ok": True})
        return seen

    def test_the_hold_page_shows_no_install_log(self):
        code, body = self.during_hold()["page"]
        self.assertEqual(code, 503)
        self.assertIn("could not be put back", body)
        self.assertIn("pre-restore.zip", body)
        self.assertNotIn("Collecting homeassistant", body)

    def test_the_hold_page_shows_no_install_log_with_a_password_either(self):
        os.environ["HRI_PASSWORD"] = "pw"
        _code, body = self.during_hold()["page"]
        self.assertNotIn("install log", body)
        self.assertNotIn("Collecting homeassistant", body)

    def test_the_api_says_what_holds_the_boot(self):
        code, body = self.during_hold()["api"]
        self.assertEqual(code, 503)
        status = json.loads(body)
        self.assertFalse(status["installing"])
        self.assertTrue(status["restore_failed"])
        self.assertIn("restore", status["error"])
        self.assertNotIn("installing", status["error"])

    def test_an_install_still_shows_its_log_without_a_password_and_says_installing(self):
        self.ep._status.update(phase="pip install homeassistant==2026.9.2", version="2026.9.2")
        srv = self.ep.start_status_server()
        self.addCleanup(self.ep.stop_status_server, srv)
        self.assertIn("Collecting homeassistant", self.get("/")[1])
        status = json.loads(self.get("/api/status")[1])
        self.assertTrue(status["installing"])
        self.assertFalse(status["restore_failed"])
        os.environ["HRI_PASSWORD"] = "pw"
        self.assertNotIn("Collecting homeassistant", self.get("/")[1])


class RestoreDuringCleanStartTest(unittest.TestCase):
    """F15: a restore applied while a clean start waited for its rebuild dropped the plan and left the set-aside
    .storage (auth tokens, every integration's credentials) on the volume for good."""

    ASIDE = ".storage.pre-rebuild-20260101-000000"

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.addCleanup(os.environ.pop, "HRI_CONFIG", None)
        self.ep = _entrypoint(self, self.cfg)
        with open(os.path.join(self.cfg, "custom_components", "x", "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.pre = backupkit.create(self.cfg, "pre-change", storage_version="2026.9.2")["name"]
        self.aside = os.path.join(self.cfg, self.ASIDE)
        os.makedirs(self.aside)
        with open(os.path.join(self.aside, "auth"), "w", encoding="utf-8") as fh:
            fh.write("refresh tokens")
        self.logged = []

    def plan(self, stage):
        with open(self.ep.REBUILD_FILE, "w", encoding="utf-8") as fh:
            json.dump({"stage": stage, "to": "2026.8.3", "backup": self.pre, "boot_backup": "boot.zip", "aside": self.ASIDE}, fh)

    def boot(self, parts):
        # the fallback's restore (restore_after_failed_change) of the pre-change backup, for the version it falls back to
        backupkit.schedule_restore(self.cfg, self.pre, parts, for_version="2026.9.2", force=True)
        state = {}
        with mock.patch.object(self.ep, "log", self.logged.append):
            self.ep.apply_config_changes(state, "2026.9.2", "2026.8.3")
        self.assertTrue(state["last_restore"]["ok"], state["last_restore"])
        self.assertFalse(os.path.isfile(self.ep.REBUILD_FILE))
        return state

    def test_the_set_aside_copy_goes_when_the_restore_replaced_storage(self):
        self.plan("import")
        self.boot(["storage"])
        self.assertFalse(os.path.isdir(self.aside))
        self.assertTrue(any(self.ASIDE in line and "removed" in line for line in self.logged), self.logged)

    def test_a_copy_an_interrupted_switch_set_aside_goes_too_without_a_failed_put_back(self):
        self.plan("renaming")  # killed after the rename, before "import" was recorded
        self.boot(["storage"])
        self.assertFalse(os.path.isdir(self.aside))
        self.assertFalse([line for line in self.logged if "putting .storage back failed" in line], self.logged)

    def test_a_restore_that_left_storage_alone_keeps_the_copy_and_says_where_it_is(self):
        self.plan("import")
        self.boot(["custom_components"])
        self.assertTrue(os.path.isfile(os.path.join(self.aside, "auth")))
        self.assertTrue(any(self.ASIDE in line and "boot.zip" in line for line in self.logged), self.logged)


class PreflightPipProcessGroupTest(unittest.TestCase):
    """F18: a preflight pip run that hit its timeout was killed alone; the build backend it started ran on."""

    def setUp(self):
        from custom_components.integration_manager import preflight

        self.preflight = preflight
        d = _tmp(self)
        self.pidfile = os.path.join(d, "backend.pid")
        self.python = os.path.join(d, "python")  # stands in for the venv's python running pip: it starts a backend and waits
        with open(self.python, "w", encoding="utf-8") as fh:
            fh.write(f"#!/bin/sh\nsleep 60 &\necho $! > {self.pidfile}\nwait\n")
        os.chmod(self.python, 0o700)
        self.addCleanup(self.kill_backend)

    def backend_pid(self):
        try:
            with open(self.pidfile, encoding="utf-8") as fh:
                return int(fh.read())
        except (OSError, ValueError):
            return None

    def kill_backend(self):
        if (pid := self.backend_pid()) is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def backend_alive(self):
        pid = self.backend_pid()
        self.assertIsNotNone(pid, "the fake pip never started its backend")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                    if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                        return False  # killed, not reaped yet (nothing reaps an orphan without an init process)
            except OSError:
                return False
            time.sleep(0.05)
        return True

    def test_a_dry_run_that_times_out_takes_its_backend_with_it(self):
        with mock.patch.object(self.preflight, "PIP_TIMEOUT_S", 1):
            res = self.preflight._pip_dry_run(self.python, ["demo"], None)
        self.assertIn("did not finish", res["stderr"])
        self.assertFalse(self.backend_alive())

    def test_a_source_build_that_times_out_takes_its_backend_with_it(self):
        with mock.patch.object(self.preflight, "PIP_TIMEOUT_S", 1):
            out = self.preflight._build_from_source(self.python, [{"name": "demo", "version": "1", "source_only": True, "url": ""}], None)
        self.assertFalse(out[0]["built"])
        self.assertIn("not built within", out[0]["error"])
        self.assertFalse(self.backend_alive())

    def test_output_and_exit_code_come_back_as_from_subprocess_run(self):
        with open(self.python, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho '{\"install\": []}'\necho warn >&2\nexit 0\n")
        res = self.preflight._pip_dry_run(self.python, ["demo"], None)
        self.assertEqual(res, {"ok": True, "install": [], "stderr": ""})
        proc = self.preflight._run_pip([self.python])
        self.assertEqual((proc.returncode, proc.stderr), (0, "warn\n"))


class ImportApplyUndoesAnyValueErrorTest(unittest.TestCase):
    """apply() re-raised every ValueError untouched, taking it for its own: one from inside async_add (a listener
    of the entry change, anything setup lets through) left the copied stores in place and the originals in
    .pre-import."""

    def test_a_value_error_from_async_add_puts_the_original_stores_back(self):
        from custom_components.integration_manager import ha_import

        cfg = _tmp(self)
        src = os.path.join(cfg, ha_import.EXTRACT_DIR, ".storage")
        os.makedirs(src)
        os.makedirs(os.path.join(cfg, ".storage"))
        for name in ("hub.e1", "hub_shared"):
            with open(os.path.join(src, name), "w", encoding="utf-8") as fh:
                fh.write("from the backup")
        with open(os.path.join(cfg, ".storage", "hub.e1"), "w", encoding="utf-8") as fh:
            fh.write("this volume's own")
        summary = {"domains": {"hub": {"entries": [{"entry_id": "e1", "data": {}}], "storage_files": ["hub.e1", "hub_shared"]}}}

        async def executor(fn, *args):
            return fn(*args)

        async def async_add(_entry):
            raise ValueError("raised by a listener")

        config_entries = SimpleNamespace(async_entries=lambda _d=None: [], async_get_entry=lambda _i: None, async_add=async_add)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), config_entries=config_entries, async_add_executor_job=executor)
        aligner = mock.Mock()
        with mock.patch.object(ha_import, "load_summary", return_value=summary), self.assertRaises(ValueError):
            asyncio.run(ha_import.apply(hass, aligner, "hub", "e1", None, None, align=False, copy_storage=True, running=False, cleanup=False))
        self.assertEqual(sorted(os.listdir(os.path.join(cfg, ".storage"))), ["hub.e1"])
        with open(os.path.join(cfg, ".storage", "hub.e1"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "this volume's own")


class CachedCatalogTest(unittest.TestCase):
    """catalog.search read every key of a row without a default: a hand-edited hacs_catalog.json made the Install
    page's search a 500 whenever the cached list was used (HACS unreachable)."""

    def test_rows_search_cannot_read_are_dropped_from_the_cached_list(self):
        from custom_components.integration_manager import catalog

        cfg = _tmp(self)
        os.makedirs(os.path.join(cfg, "integration_manager"))
        good = {"domain": "demo", "repo": "o/demo", "name": "Demo", "description": "a demo", "last_version": "1.0",
                "last_updated": "2026-09-01", "topics": ["demo"]}
        with open(os.path.join(cfg, "integration_manager", "hacs_catalog.json"), "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": "x", "rows": [{"domain": "nameless", "repo": "o/n"}, "text", {**good, "topics": None},
                                                   {**good, "name": 5}, good]}, fh)
        cat = catalog.Catalog(SimpleNamespace(config=SimpleNamespace(path=lambda *p: os.path.join(cfg, *p))))
        rows = cat._load_cached()
        results, total = catalog.search(rows, "demo", {}, set())
        self.assertEqual((total, results[0]["domain"]), (1, "demo"))
        self.assertEqual(rows, [good])


class CancelRestoreByHandTest(unittest.TestCase):
    """Cancel restore in the UI leaves a version change's own restore alone, judging and cancelling one schedule
    under the lock a new schedule takes (the view read the schedule through a private helper, then cancelled)."""

    def setUp(self):
        self.cfg = _volume()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.backup = backupkit.create(self.cfg, "b", storage_version="2026.8.3")["name"]

    def ha_change(self, to):
        with open(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"current": "2026.8.3", **({"change": {"to": to, "mode": "restore"}} if to else {})}, fh)

    def test_a_version_changes_own_restore_is_refused_and_stays(self):
        self.ha_change("2026.9.2")
        backupkit.schedule_restore(self.cfg, self.backup, ["storage"], for_version="2026.9.2")
        with self.assertRaises(backupkit.BelongsToVersionChange) as ctx:
            backupkit.cancel_restore(self.cfg, by_hand=True)
        self.assertEqual(ctx.exception.for_version, "2026.9.2")
        self.assertTrue(backupkit.pending(self.cfg))

    def test_a_leftover_of_a_change_no_longer_recorded_is_cancelled(self):
        self.ha_change(None)
        backupkit.schedule_restore(self.cfg, self.backup, ["storage"], for_version="2026.9.2")
        self.assertTrue(backupkit.cancel_restore(self.cfg, by_hand=True))
        self.assertFalse(backupkit.pending(self.cfg))

    def test_a_restore_scheduled_by_hand_is_cancelled_and_the_entrypoint_call_is_unchanged(self):
        self.ha_change("2026.9.2")
        backupkit.schedule_restore(self.cfg, self.backup, ["storage"])
        self.assertTrue(backupkit.cancel_restore(self.cfg, by_hand=True))
        backupkit.schedule_restore(self.cfg, self.backup, ["storage"], for_version="2026.9.2")
        self.assertTrue(backupkit.cancel_restore(self.cfg))  # not by hand: the entrypoint drops a change's restore itself

    def test_a_schedule_cannot_slip_in_between_the_check_and_the_cancel(self):
        self.ha_change("2026.9.2")
        backupkit.schedule_restore(self.cfg, self.backup, ["storage"], for_version="2026.8.3")  # a leftover: cancellable
        first_zip = backupkit._pending_meta(self.cfg)["zip"]
        checking, go_on, scheduled = threading.Event(), threading.Event(), threading.Event()
        real_check = backupkit._scheduled_change_to

        def slow_check(cfg):
            checking.set()
            self.assertTrue(go_on.wait(5))
            return real_check(cfg)

        def schedule_the_changes_restore():
            backupkit.schedule_restore(self.cfg, self.backup, ["storage"], for_version="2026.9.2")
            scheduled.set()

        result = {}
        with mock.patch.object(backupkit, "_scheduled_change_to", slow_check):
            canceller = threading.Thread(target=lambda: result.setdefault("cancelled", backupkit.cancel_restore(self.cfg, by_hand=True)))
            canceller.start()
            self.assertTrue(checking.wait(5))
            scheduler = threading.Thread(target=schedule_the_changes_restore)
            scheduler.start()
            self.assertFalse(scheduled.wait(0.3), "a new schedule went through while the cancel was deciding")
            go_on.set()
            canceller.join(5)
            scheduler.join(5)
        self.assertTrue(result["cancelled"])  # the leftover it judged
        meta = backupkit._pending_meta(self.cfg)
        self.assertNotEqual(meta["zip"], first_zip)
        self.assertEqual(meta["for_version"], "2026.9.2")  # the change's restore scheduled afterwards is intact
        self.assertTrue(backupkit.pending(self.cfg))


if __name__ == "__main__":
    unittest.main()
