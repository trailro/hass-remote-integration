"""External review, backup part: interrupted restores, clean start after a kill, pruning, HA import limits, durability."""

import asyncio
import errno
import importlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import backupkit
import jsonio


def _entrypoint(cfg):
    os.environ["HRI_CONFIG"] = cfg
    sys.modules.pop("entrypoint", None)
    return importlib.import_module("entrypoint")


def _hass(cfg):
    async def executor(fn, *args):
        return fn(*args)

    return SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=executor)


def _volume():
    cfg = tempfile.mkdtemp()
    for d in (".storage", backupkit.STATE_DIR, "custom_components/x"):
        os.makedirs(os.path.join(cfg, d))
    for i in range(10):
        with open(os.path.join(cfg, ".storage", f"s{i}"), "w", encoding="utf-8") as fh:
            fh.write(f"orig{i}")
    with open(os.path.join(cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
        fh.write("{}")
    with open(os.path.join(cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
        json.dump({"current": "2026.8.3"}, fh)
    return cfg


def _zip(cfg, name, info=None, created=None):
    bdir = os.path.join(cfg, backupkit.BACKUP_DIR)
    os.makedirs(bdir, exist_ok=True)
    with zipfile.ZipFile(os.path.join(bdir, name), "w") as zf:
        zf.writestr(backupkit.MARKER, "{}")
        zf.writestr(".storage/core.config_entries", json.dumps({"from": name}))
        if info is not None:
            zf.writestr("backup-info.json", info if isinstance(info, str) else json.dumps(info))
    return os.path.join(bdir, name)


def _schedule_changed_restore(cfg):
    rec = backupkit.create(cfg, "src")
    for i in range(10):
        with open(os.path.join(cfg, ".storage", f"s{i}"), "w", encoding="utf-8") as fh:
            fh.write(f"new{i}")
    backupkit.schedule_restore(cfg, rec["name"])
    return rec


def _storage(cfg):
    out = {}
    for n in sorted(os.listdir(os.path.join(cfg, ".storage"))):
        with open(os.path.join(cfg, ".storage", n), encoding="utf-8") as fh:
            out[n] = fh.read()
    return out


class InterruptedRestoreTest(unittest.TestCase):
    """20: a KeyboardInterrupt / SystemExit during the moves."""

    def test_interrupt_rolls_back_keeps_the_schedule_and_propagates(self):
        cfg = _volume()
        _schedule_changed_restore(cfg)
        before = _storage(cfg)
        real, calls, recorded = os.replace, [], []

        def replace(a, b):
            if "staging-restore" in a:
                calls.append(a)
                if len(calls) == 3:
                    raise KeyboardInterrupt
            return real(a, b)

        with mock.patch.object(backupkit.os, "replace", replace), self.assertRaises(KeyboardInterrupt):
            backupkit.apply_pending(cfg, log=lambda *_: None, record=recorded.append)
        self.assertEqual(recorded, [])  # no final ok=False with an empty error
        self.assertTrue(backupkit.pending(cfg))
        self.assertTrue(backupkit._pending_meta(cfg).get("pre_restore"))
        self.assertEqual(_storage(cfg), before)  # put back
        result = backupkit.apply_pending(cfg, log=lambda *_: None, record=recorded.append)  # the next boot retries
        self.assertTrue(result["ok"], result)
        self.assertEqual(_storage(cfg)["s0"], "orig0")


class FailedRollbackTest(unittest.TestCase):
    """21: the rollback itself fails (a full disk)."""

    def _fail(self, cfg):
        real_extract, real_replace = backupkit._extract_to, os.replace
        n = {"x": 0, "r": 0}

        def extract(zf, names, root):
            n["x"] += 1
            if n["x"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_extract(zf, names, root)

        def replace(a, b):
            if "staging-restore" in a:
                n["r"] += 1
                if n["r"] == 3:
                    raise OSError(errno.ENOSPC, "No space left on device")
            return real_replace(a, b)

        recorded = []
        with mock.patch.object(backupkit, "_extract_to", extract), mock.patch.object(backupkit.os, "replace", replace):
            result = backupkit.apply_pending(cfg, log=lambda *_: None, record=recorded.append)
        return result, recorded

    def test_schedule_stays_and_the_pre_restore_copy_is_named_and_protected(self):
        cfg = _volume()
        _schedule_changed_restore(cfg)
        result, recorded = self._fail(cfg)
        pre = backupkit._pending_meta(cfg)["pre_restore"]
        self.assertFalse(result["ok"])
        self.assertEqual(result["recovery_source"], pre)
        self.assertIn("rollback", result["error"])
        self.assertEqual(recorded, [result])  # the failure is recorded, the schedule is not removed
        self.assertTrue(backupkit.pending(cfg))
        self.assertEqual([n for n in os.listdir(os.path.join(cfg, backupkit.STATE_DIR)) if n.startswith("staging-restore")], [])
        for i in range(3):
            _zip(cfg, f"2027010{i}-000000-later.zip", {"created": f"2027010{i}-000000", "ha_version": "2026.8.3"})
        self.assertNotIn(pre, backupkit.prune(cfg, 1))

    def test_recorded_recovery_source_is_protected_without_the_schedule(self):
        cfg = _volume()
        _zip(cfg, "a-pre.zip", {"created": "20200101-000000"})
        _zip(cfg, "b.zip", {"created": "20210101-000000"})
        with open(os.path.join(cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"last_restore": {"ok": False, "recovery_source": "a-pre.zip"}}, fh)
        self.assertEqual(backupkit.prune(cfg, 1), [])


class CleanStartKillTest(unittest.TestCase):
    """22: a kill right after .storage was renamed."""

    def setUp(self):
        self.cfg = _volume()
        self.ep = _entrypoint(self.cfg)
        os.makedirs(os.path.join(self.ep.STATE_DIR, "import-extracted"))
        with open(os.path.join(self.ep.STATE_DIR, "import-extracted", "summary.json"), "w", encoding="utf-8") as fh:
            json.dump({"type": "ha-downgrade-rebuild"}, fh)
        with open(self.ep.REBUILD_FILE, "w", encoding="utf-8") as fh:
            json.dump({"stage": "reset", "to": "2026.8.3", "backup": "b.zip"}, fh)
        self.old = os.path.join(self.cfg, ".storage.pre-rebuild-20200101-000000")
        os.makedirs(self.old)

    def tearDown(self):
        os.environ.pop("HRI_CONFIG", None)
        sys.modules.pop("entrypoint", None)

    def plan(self):
        with open(self.ep.REBUILD_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def kill_after_rename(self):
        real = os.rename

        def rename(a, b):
            real(a, b)
            raise KeyboardInterrupt  # docker stop: no finally, no except

        with mock.patch.object(backupkit, "validate", return_value={}), mock.patch.object(self.ep.os, "rename", rename), \
                self.assertRaises(KeyboardInterrupt):
            self.ep.reset_storage_for_rebuild("2026.8.3", False)

    def test_next_boot_finishes_the_switch_without_a_new_backup(self):
        self.kill_after_rename()
        plan = self.plan()
        self.assertEqual(plan["stage"], "renaming")
        aside = os.path.join(self.cfg, plan["aside"])
        self.assertTrue(os.path.isdir(aside))
        self.assertTrue(os.path.isdir(self.old))  # older copies go only once "import" is recorded
        backups = sorted(os.listdir(os.path.join(self.cfg, backupkit.BACKUP_DIR)))
        with mock.patch.object(backupkit, "validate", return_value={}):
            self.assertTrue(self.ep.reset_storage_for_rebuild("2026.8.3", False))
        plan2 = self.plan()
        self.assertEqual(plan2["stage"], "import")
        self.assertEqual(plan2["boot_backup"], plan["boot_backup"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.cfg, backupkit.BACKUP_DIR))), backups)  # no backup of the empty .storage
        self.assertTrue(os.path.isfile(os.path.join(aside, "s0")))
        self.assertEqual(os.listdir(os.path.join(self.cfg, ".storage")), [])
        self.assertFalse(os.path.isdir(self.old))

    def test_dropped_plan_puts_the_set_aside_storage_back(self):
        self.kill_after_rename()
        with mock.patch.object(backupkit, "validate", return_value={}):
            self.assertFalse(self.ep.reset_storage_for_rebuild("2026.9.2", False))  # another version boots
        self.assertEqual(_storage(self.cfg)["s0"], "orig0")
        self.assertFalse(os.path.isfile(self.ep.REBUILD_FILE))

    def test_existing_boot_backup_is_never_replaced(self):
        with open(self.ep.REBUILD_FILE, "w", encoding="utf-8") as fh:
            json.dump({"stage": "reset", "to": "2026.8.3", "backup": "b.zip", "boot_backup": "earlier.zip"}, fh)
        with mock.patch.object(backupkit, "validate", return_value={}), mock.patch.object(backupkit, "create") as create:
            self.assertTrue(self.ep.reset_storage_for_rebuild("2026.8.3", False))
        create.assert_not_called()
        self.assertEqual(self.plan()["boot_backup"], "earlier.zip")


class ForeignBackupInfoTest(unittest.TestCase):
    """23: backup-info.json that is not an object."""

    def test_list_prune_and_pending_version_survive_it(self):
        cfg = _volume()
        _zip(cfg, "20200101-000000-bad.zip", "[1, 2]")
        _zip(cfg, "20210101-000000-ok.zip", {"created": "20210101-000000", "ha_version": "2026.8.3"})
        self.assertEqual(len(backupkit.list_backups(cfg)), 2)
        self.assertIsNone(backupkit.describe(cfg, "20200101-000000-bad.zip")["created"])
        self.assertEqual(len(backupkit.prune(cfg, 1)), 1)
        path = _zip(cfg, "c.zip", "[1]")
        backupkit.schedule_restore(cfg, "c.zip", ["manager"])
        meta = backupkit._pending_meta(cfg)
        meta.pop("ha_version")
        jsonio.write_json(os.path.join(cfg, backupkit.PENDING_META), meta)
        self.assertTrue(os.path.isfile(path))
        self.assertIsNone(backupkit.pending_ha_version(cfg))


class PruneOrderTest(unittest.TestCase):
    """24"""

    def test_future_dated_upload_does_not_push_out_real_backups(self):
        cfg = _volume()
        for i, name in enumerate(("a.zip", "b.zip", "c.zip")):
            p = _zip(cfg, name, {"created": time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 3600 * (3 - i)))})
            os.utime(p, (time.time() - 3600 * (3 - i),) * 2)
        fut = _zip(cfg, "future.zip", {"created": "29991231-000000"})
        os.utime(fut, (time.time() - 10 * 86400,) * 2)
        self.assertEqual(sorted(backupkit.prune(cfg, 2)), ["a.zip", "future.zip"])

    def test_recent_uploads_are_not_pruned_and_old_ones_are(self):
        cfg = _volume()
        _zip(cfg, "upload-new.zip", {"created": "20200101-000000"})
        old = _zip(cfg, "upload-old.zip", {"created": "20200102-000000"})
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        _zip(cfg, "own.zip", {"created": time.strftime("%Y%m%d-%H%M%S")})
        self.assertEqual(backupkit.prune(cfg, 1), ["upload-old.zip"])

    def test_create_view_and_daily_backup_protect_the_backup_they_made(self):
        from custom_components.integration_manager import backup_views, scheduler

        cfg = _volume()
        seen = []
        installer = SimpleNamespace(settings=SimpleNamespace(backup_keep=1, bool_=lambda k: k == "backup_daily"), protected_backups=lambda: {"x.zip"},
                                    busy=False, backup_running=False, config_dir=cfg)

        async def backup(label):
            return {"name": "new.zip", "bytes": 1}

        installer.async_backup_exclusive = backup
        with mock.patch.object(backupkit, "prune", lambda c, keep, protect: seen.append(protect) or []):
            view = backup_views.BackupCreateView(_hass(cfg), installer)
            view.json = lambda d: d
            asyncio.run(backup_views.BackupCreateView.post.__wrapped__(view, None, {}))
            sch = scheduler.Scheduler(_hass(cfg), installer)
            sch._release_check_due = lambda: False
            asyncio.run(sch._daily(None))
        self.assertEqual(seen, [{"x.zip", "new.zip"}] * 2)


def _ha_tar(path, meta, inner=b"x" * 100, mode="w"):
    with tarfile.open(path, mode) as tf:
        for name, data in (("backup.json", meta if isinstance(meta, bytes) else json.dumps(meta).encode()), ("homeassistant.tar.gz", inner)):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


class HaImportLimitsTest(unittest.TestCase):
    """25"""

    def inspect(self, *args, **kwargs):
        from custom_components.integration_manager import ha_import

        cfg = tempfile.mkdtemp()
        path = os.path.join(cfg, "import.tar")
        _ha_tar(path, *args, **kwargs)
        out = os.path.join(cfg, "out")
        os.makedirs(out)
        with self.assertRaises(ValueError) as ctx:
            ha_import._inspect(cfg, path, out, None, set())
        return str(ctx.exception)

    def test_compressed_outer_tar_is_refused(self):
        self.assertIn("uncompressed", self.inspect({"compressed": True}, mode="w:xz"))

    def test_non_object_backup_json(self):
        self.assertIn("not a JSON object", self.inspect([1, 2]))

    def test_oversized_backup_json(self):
        from custom_components.integration_manager import ha_import

        with mock.patch.object(ha_import, "MAX_META_BYTES", 5):
            self.assertIn("implausibly large", self.inspect({"compressed": True}))

    def test_oversized_inner_archive_is_refused_before_copying(self):
        from custom_components.integration_manager import ha_import

        with mock.patch.object(ha_import, "MAX_INNER_BYTES", 10), mock.patch.object(ha_import.shutil, "copyfileobj") as copy:
            self.assertIn("larger than", self.inspect({"compressed": True}))
        copy.assert_not_called()


class UnknownVersionRestoreTest(unittest.TestCase):
    """26"""

    def setUp(self):
        self.cfg = _volume()
        _zip(self.cfg, "nov.zip", {"created": "20200101-000000"})

    def tearDown(self):
        os.environ.pop("HRI_CONFIG", None)
        sys.modules.pop("entrypoint", None)

    def test_storage_restore_needs_force(self):
        with self.assertRaises(backupkit.UnknownVersion):
            backupkit.schedule_restore(self.cfg, "nov.zip")
        backupkit.schedule_restore(self.cfg, "nov.zip", ["manager"])  # no .storage: the version does not matter
        backupkit.schedule_restore(self.cfg, "nov.zip", ["storage"], force=True)
        self.assertTrue(backupkit.pending_forced(self.cfg))

    def test_view_asks_for_force(self):
        from custom_components.integration_manager import backup_views

        installer = SimpleNamespace(busy=False, protected_backups=set)
        view = backup_views.BackupActionView(_hass(self.cfg), installer)
        view.json = lambda d: d
        r = asyncio.run(backup_views.BackupActionView.post.__wrapped__(view, None, {}, "nov.zip", "restore"))
        self.assertTrue(r.get("needs_force"), r)
        self.assertFalse(backupkit.pending(self.cfg))
        with mock.patch.object(backup_views.ha_import, "drop_rebuild", lambda cfg: None), mock.patch.object(backup_views.events, "emit"):
            r = asyncio.run(backup_views.BackupActionView.post.__wrapped__(view, None, {"force": True}, "nov.zip", "restore"))
        self.assertTrue(r["ok"], r)
        self.assertTrue(backupkit.pending(self.cfg))

    def test_boot_drops_an_unforced_schedule(self):
        backupkit.schedule_restore(self.cfg, "nov.zip", force=True)
        meta = backupkit._pending_meta(self.cfg)
        jsonio.write_json(os.path.join(self.cfg, backupkit.PENDING_META), {**meta, "force": False})
        ep = _entrypoint(self.cfg)
        state = {}
        ep.apply_config_changes(state, "2026.8.3", "2026.8.3")
        self.assertFalse(backupkit.pending(self.cfg))
        self.assertNotIn("last_restore", state)
        self.assertIn("not forced", state["last_error"])


class UmaskTest(unittest.TestCase):
    """27"""

    def test_main_restricts_the_umask_first(self):
        ep = _entrypoint(tempfile.mkdtemp())
        order = []

        class Stop(Exception):
            pass

        with mock.patch.object(ep, "restrict_umask", lambda: order.append("umask")), \
                mock.patch.object(ep.os, "makedirs", side_effect=lambda *a, **k: (order.append("makedirs"), (_ for _ in ()).throw(Stop()))):
            with self.assertRaises(Stop):
                ep.main()
        self.assertEqual(order, ["umask", "makedirs"])
        os.environ.pop("HRI_CONFIG", None)
        sys.modules.pop("entrypoint", None)

    def test_restrict_umask(self):
        ep = _entrypoint(tempfile.mkdtemp())
        previous = ep.restrict_umask()
        try:
            self.assertEqual(os.umask(0o077), 0o077)
            d = tempfile.mkdtemp()
            p = os.path.join(d, "f")
            open(p, "w").close()
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        finally:
            os.umask(previous)
            os.environ.pop("HRI_CONFIG", None)
            sys.modules.pop("entrypoint", None)


class DurabilityTest(unittest.TestCase):
    """28"""

    def test_write_json_syncs_the_file_and_its_directory(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(jsonio.os, "fsync", wraps=os.fsync) as fsync:
            jsonio.write_json(os.path.join(d, "a.json"), {"x": 1})
        self.assertGreaterEqual(fsync.call_count, 2)
        self.assertEqual(jsonio.read_json(os.path.join(d, "a.json")), {"x": 1})

    def test_backup_is_synced_before_it_gets_its_name(self):
        cfg = _volume()
        with mock.patch.object(backupkit.os, "fsync", wraps=os.fsync) as fsync:
            rec = backupkit.create(cfg, "x")
        self.assertGreaterEqual(fsync.call_count, 1)
        backupkit.validate(os.path.join(cfg, backupkit.BACKUP_DIR, rec["name"]))

    def test_restore_syncs_before_recording(self):
        cfg = _volume()
        _schedule_changed_restore(cfg)
        order = []
        with mock.patch.object(backupkit, "_sync", lambda: order.append("sync")):
            backupkit.apply_pending(cfg, log=lambda *_: None, record=lambda r: order.append("record"))
        self.assertEqual(order, ["sync", "record"])


class FullRollbackOrderTest(unittest.TestCase):
    """D1: the restore is scheduled before the old code is started."""

    def installer(self, start_result):
        from custom_components.integration_manager.installer import Installer

        cfg = _volume()
        _zip(cfg, "pre.zip", {"ha_version": "2026.8.3"})
        inst = Installer.__new__(Installer)
        inst.hass = _hass(cfg)
        inst.config_dir = cfg
        inst.busy = False
        inst.state = SimpleNamespace(domain="demo", installed={"demo": {"previous_tag": "v1", "pre_update_backup": "pre.zip", "versions": {"v1": {}}}},
                                     pending_change=None, pending_smoke=None, restart_required=False, last_action="", rollback_backup=None)
        inst._cancel_smoke = lambda: None
        inst._save_state = lambda: None
        seen = {}

        async def start(domain, tag, own_restore=None):
            archive = backupkit.pending_archive(cfg)
            seen["scheduled"] = archive is not None
            seen["own_restore_is_the_scheduled_one"] = archive is not None and os.path.basename(archive) == own_restore
            return start_result

        inst.start = start
        return inst, seen, cfg

    def test_scheduled_before_start(self):
        from custom_components.integration_manager import installer as mod

        inst, seen, cfg = self.installer({"ok": True})
        with mock.patch.object(mod.events, "emit"):
            res = asyncio.run(inst.rollback_full())
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen, {"scheduled": True, "own_restore_is_the_scheduled_one": True})
        self.assertTrue(backupkit.pending(cfg))
        self.assertEqual(backupkit.pending_parts(cfg), ["storage", "custom_components"])

    def test_failed_start_cancels_the_restore(self):
        inst, seen, cfg = self.installer({"ok": False, "error": "boom"})
        res = asyncio.run(inst.rollback_full())
        self.assertFalse(res["ok"])
        self.assertTrue(seen["scheduled"])
        self.assertFalse(backupkit.pending(cfg))


class ImportInvalidEntryIdTest(unittest.TestCase):
    """D2"""

    def test_invalid_entry_ids_are_skipped_with_a_warning(self):
        from custom_components.integration_manager import ha_import

        hass = _hass("/nonexistent")
        hass.config_entries = SimpleNamespace(async_entries=lambda domain=None: [], async_get_entry=lambda _id: None)
        applied = []

        async def fake_apply(_hass, _aligner, domain, entry_id, *args, **kwargs):
            applied.append(entry_id)
            return {"entry_id": entry_id, "state": "loaded"}

        summary = {"domains": {"hub": {"entries": [{"entry_id": "."}, {"entry_id": ""}, {"entry_id": "e1"}]}}}
        with mock.patch.object(ha_import, "load_summary", return_value=summary), mock.patch.object(ha_import, "apply", fake_apply), \
                mock.patch.object(ha_import, "clear", lambda cfg: None):
            res = asyncio.run(ha_import.apply_all(hass, None, None, True, True, "hub", {"hub"}))
        self.assertEqual(applied, ["e1"])
        self.assertEqual(len(res["warnings"]), 2)
        self.assertEqual([r["skipped"] for r in res["skipped"]], ["invalid entry id"] * 2)


class UploadNameTest(unittest.TestCase):
    """C9: an upload never replaces an existing backup."""

    def test_existing_name_gets_a_suffix(self):
        from custom_components.integration_manager import backup_views

        cfg = _volume()
        existing = _zip(cfg, "upload-b.zip", {"ha_version": "2026.8.3"})
        with open(existing, "rb") as fh:
            original = fh.read()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as zf:
            zf.writestr(backupkit.MARKER, "{}")
            zf.writestr(".storage/core.config_entries", "{}")
        chunks = [payload.getvalue(), b""]

        async def read_chunk(_n):
            return chunks.pop(0)

        field = SimpleNamespace(name="file", filename="b.zip", read_chunk=read_chunk)

        async def multipart():
            async def nxt():
                return field
            return SimpleNamespace(next=nxt)

        request = SimpleNamespace(headers={"X-Requested-With": "fetch"}, multipart=multipart)
        view = backup_views.BackupUploadView(_hass(cfg))
        view.json = lambda d: d
        r = asyncio.run(view.post(request))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["name"], "upload-b-2.zip")
        with open(existing, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual(backupkit.reserve_name(os.path.join(cfg, backupkit.BACKUP_DIR), "upload-b"), "upload-b-3.zip")


if __name__ == "__main__":
    unittest.main()
