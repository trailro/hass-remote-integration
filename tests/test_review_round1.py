"""Fixes from the first review round: MQTT commands, backups, sessions, ha.json, protected backups."""

import json
import os
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace

import backupkit
from custom_components.integration_manager.auth import SESSION_S, Auth
from custom_components.integration_manager.ha_updater import HaUpdater
from custom_components.integration_manager.installer import Installer

from tests.test_mqtt_publisher import BASE, publisher


def message(topic, payload=b"", retain=False):
    return SimpleNamespace(topic=topic, payload=payload, retain=retain)


class EmptyPayloadTest(unittest.TestCase):
    def test_empty_command_and_call_are_ignored(self):
        pub = publisher()
        pub._topics = {"switch.boiler": f"{BASE}/demo/switch/boiler"}
        for topic in (f"{BASE}/cmd/switch/boiler/state", f"{BASE}/call/script/turn_on", f"{BASE}/manager/cmd/restart"):
            pub._handle_message(message(topic))
        self.assertEqual(pub.history, [])
        self.assertEqual(pub.hass.loop.calls, [])
        self.assertEqual(pub.hass.tasks, [])


class CallDedupTest(unittest.TestCase):
    def test_same_id_for_another_service_is_not_a_duplicate(self):
        pub = publisher()
        rec = {"received": time.time(), "state": "ok"}
        pub._calls = {"light.turn_on:1": rec}
        self.assertIs(pub._seen_call("light.turn_on:1"), rec)
        self.assertIsNone(pub._seen_call("script.turn_on:1"))
        self.assertIsNone(pub._seen_call(None))


def make_backup(config_dir, name, info):
    path = os.path.join(config_dir, backupkit.BACKUP_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(backupkit.MARKER, "{}")
        zf.writestr(".storage/core.config_entries", "{}")
        zf.writestr("backup-info.json", json.dumps(info))
    return path


class BackupVersionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cfg, backupkit.STATE_DIR), exist_ok=True)

    def test_non_string_ha_version_is_ignored(self):
        path = make_backup(self.cfg, "odd.zip", {"ha_version": 2026})
        self.assertNotIn("ha_version", backupkit.validate(path))
        self.assertIsNone(backupkit.describe(self.cfg, "odd.zip")["ha_version"])

    def test_newer_backup_without_storage_can_be_scheduled(self):
        make_backup(self.cfg, "new.zip", {"ha_version": "2026.9.2"})
        with open(os.path.join(self.cfg, backupkit.STATE_DIR, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"current": "2026.8.3"}, fh)
        with self.assertRaises(ValueError):
            backupkit.schedule_restore(self.cfg, "new.zip", None)
        backupkit.schedule_restore(self.cfg, "new.zip", ["yaml"])
        self.assertEqual(backupkit.pending_parts(self.cfg), ["yaml"])

    def test_pending_restore_records_the_backup_version(self):
        make_backup(self.cfg, "b.zip", {"ha_version": "2026.8.3"})
        backupkit.schedule_restore(self.cfg, "b.zip", ["storage"])
        self.assertEqual(backupkit.pending_ha_version(self.cfg), "2026.8.3")
        self.assertEqual(backupkit.pending_parts(self.cfg), ["storage"])
        backupkit.cancel_restore(self.cfg)
        self.assertIsNone(backupkit.pending_ha_version(self.cfg))


class LogoutTest(unittest.TestCase):
    def test_logout_ends_sessions_issued_before_it(self):
        path = os.path.join(tempfile.mkdtemp(), "auth_revoked")
        auth = Auth("pw", b"k" * 32, path)
        old = auth.new_session()
        self.assertTrue(auth.valid_session(old))
        auth.revoke_all()
        self.assertFalse(auth.valid_session(old))
        self.assertTrue(auth.valid_session(auth.new_session()))
        again = Auth("pw", b"k" * 32, path)
        again.load_revoked()
        self.assertFalse(again.valid_session(old))

    def test_tampered_or_expired_cookie(self):
        auth = Auth("pw", b"k" * 32)
        expires = int(time.time()) - 1
        self.assertFalse(auth.valid_session(auth._sign(expires)))
        self.assertFalse(auth.valid_session(f"{int(time.time()) + SESSION_S}.deadbeef"))


class HaJsonTest(unittest.TestCase):
    def updater(self, content):
        path = os.path.join(tempfile.mkdtemp(), "ha.json")
        if content is not None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        up = object.__new__(HaUpdater)
        up.file = path
        return up

    def test_unreadable_file_is_not_overwritten(self):
        up = self.updater('{"current": "2026.9')
        with self.assertRaises(ValueError):
            up.set_desired("2026.9.2")
        with open(up.file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"current": "2026.9')

    def test_missing_file_starts_empty(self):
        up = self.updater(None)
        self.assertEqual(up.set_desired("2026.9.2")["desired"], "2026.9.2")


class ProtectedBackupsTest(unittest.TestCase):
    def test_recovery_backup_is_protected(self):
        inst = object.__new__(Installer)
        inst.state_dir = tempfile.mkdtemp()
        inst.state = SimpleNamespace(installed={}, rollback_backup=None)
        with open(os.path.join(inst.state_dir, "ha.json"), "w", encoding="utf-8") as fh:
            json.dump({"change": {"backup": "pre-ha.zip"}, "recovery": {"backup": "recovery.zip", "for": "2026.8.3"}}, fh)
        self.assertTrue({"pre-ha.zip", "recovery.zip"} <= inst.protected_backups())
