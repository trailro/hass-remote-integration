"""External review of 65f2e32: the five findings that keep the container from doing its job.

F-02  one file whose mtime is outside what a zip can hold made every backup fail - and create() is what
      the pre-install, pre-replace, pre-HA-change and pre-restore copies are taken with.
F-06  the preflight decompressed hacs.json before any cap applied, so a few hundred KB of archive could
      take hundreds of MB of the container's memory - and was refused only afterwards, by _unpack.
F-08  a fresh volume whose first install fails never tried the version baked into the image: it exited,
      and the restart loop asked PyPI for the same broken version again.
F-17  an infinity in mqtt.json or settings.json (json.load reads 1e999 and Infinity) raised OverflowError
      out of int(), which nothing caught: no MQTT component, and a 500 on GET /api/settings.
C-5   a trailing "-" in HRI_APT_PACKAGES is apt's REMOVE operator, not part of a package name.
"""

import io
import json
import os
import re
import tempfile
import unittest
import zipfile
from unittest import mock

import backupkit
from custom_components.integration_manager import mqtt_publisher as mp
from custom_components.integration_manager import settings as st
from custom_components.integration_manager.installer import Installer, METADATA_MAX_BYTES, UNPACK_MAX_BYTES
from tests.fakes import entrypoint_for
from tests.test_codex_mqtt_review import _config_publisher

INFINITY = float("inf")  # what json.load makes of 1e999, Infinity and -Infinity


def _config_with(mtime):
    """A minimal /config that validate() accepts, whose one .storage file has ``mtime``."""
    cfg = tempfile.mkdtemp()
    os.makedirs(os.path.join(cfg, ".storage"))
    os.makedirs(os.path.join(cfg, backupkit.STATE_DIR))
    with open(os.path.join(cfg, "configuration.yaml"), "w", encoding="utf-8") as fh:
        fh.write("default_config:\n")
    with open(os.path.join(cfg, backupkit.MARKER), "w", encoding="utf-8") as fh:
        fh.write("{}")
    path = os.path.join(cfg, ".storage", "core.restore_state")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}")
    os.utime(path, (mtime, mtime))
    return cfg


class BackupTimestampTest(unittest.TestCase):
    """F-02: the mtime of one member is never a reason for the whole backup not to exist."""

    def assert_backup_of(self, mtime):
        cfg = _config_with(mtime)
        record = backupkit.create(cfg, label="review")  # before the fix: nothing written and a raise
        path = os.path.join(cfg, backupkit.BACKUP_DIR, record["name"])
        self.assertTrue(os.path.isfile(path), record)
        backupkit.validate(path)  # raises unless the archive is a restorable backup
        return path

    def test_a_file_from_the_epoch_still_gets_backed_up(self):
        # reproducible-build archives and dev-mode sources carry mtime 0; before the fix create() raised
        # "ZIP does not support timestamps before 1980" and left no backup at all
        path = self.assert_backup_of(0)
        with zipfile.ZipFile(path) as zf:
            stamps = {i.filename: i.date_time for i in zf.infolist() if i.filename.endswith("core.restore_state")}
        self.assertEqual(list(stamps.values()), [(1980, 1, 1, 0, 0, 0)])

    def test_a_file_dated_after_2107_still_gets_backed_up(self):
        # a clock far in the future used to raise struct.error out of the middle of the zip
        path = self.assert_backup_of(4400000000)  # 2109
        with zipfile.ZipFile(path) as zf:
            stamp = next(i.date_time for i in zf.infolist() if i.filename.endswith("core.restore_state"))
        self.assertEqual(stamp[:5], (2107, 12, 31, 23, 59))  # the seconds a zip stores are even

    def test_an_ordinary_mtime_is_kept_as_it_is(self):
        path = self.assert_backup_of(1600000000)  # 2020-09-13
        with zipfile.ZipFile(path) as zf:
            year = next(i.date_time[0] for i in zf.infolist() if i.filename.endswith("core.restore_state"))
        self.assertEqual(year, 2020)


def _archive(hacs_uncompressed, extra_pad=0):
    """A zipball whose hacs.json is ``hacs_uncompressed`` bytes of one long compressible run."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("repo-abc/hacs.json", "w") as fh:
            fh.write(b'{"homeassistant": "2024.1.0", "pad": "')
            written = 0
            while written < hacs_uncompressed:
                block = min(1 << 20, hacs_uncompressed - written)
                fh.write(b"A" * block)
                written += block
            fh.write(b'"}')
        if extra_pad:
            zf.writestr("repo-abc/pad.bin", b"B" * extra_pad)
        zf.writestr("repo-abc/custom_components/x/manifest.json", json.dumps({"domain": "x"}))
    return buf.getvalue()


class HacsJsonCapTest(unittest.TestCase):
    """F-06: what the preflight reads before _unpack runs is capped the same way _unpack is."""

    def test_an_ordinary_hacs_json_is_still_read(self):
        self.assertEqual(Installer._hacs_min_ha(_archive(0)), "2024.1.0")

    def test_a_hacs_json_bigger_than_the_metadata_cap_is_not_decompressed(self):
        blob = _archive(METADATA_MAX_BYTES + (1 << 20))
        reads = []
        real_read = zipfile.ZipFile.read

        def spy(self, name, *a, **kw):
            reads.append(name)
            return real_read(self, name, *a, **kw)

        self.assertLess(len(blob), 1 << 20, "the point of the test is a small archive")
        with mock.patch.object(zipfile.ZipFile, "read", spy), self.assertLogs(Installer.__module__, "WARNING"):
            self.assertIsNone(Installer._hacs_min_ha(blob))
        self.assertEqual(reads, [], "the oversized member must never reach zf.read")

    def test_an_archive_unpack_would_refuse_is_refused_here_too(self):
        # 398 KB in, 830 MB of peak memory out - and only then an _unpack that says no
        blob = _archive(UNPACK_MAX_BYTES + (1 << 20))
        self.assertLess(len(blob), 1 << 20)
        with self.assertLogs(Installer.__module__, "WARNING") as logs:
            self.assertIsNone(Installer._hacs_min_ha(blob))
        self.assertIn("300 MB", "\n".join(logs.output))


class FreshVolumeInstallFallbackTest(unittest.TestCase):
    """F-08: the version the image was built with is tried before the boot gives up."""

    def _prepare(self, newest, installs, **env):
        cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(cfg, "integration_manager"))
        ep = entrypoint_for(self, cfg, **env)
        tried = []

        def install(version):
            tried.append(version)
            return installs.get(version, False)

        done = {v for v, ok in installs.items() if ok}
        with mock.patch.object(ep, "latest_stable", return_value=newest), \
                mock.patch.object(ep, "fits_this_python", return_value=True), \
                mock.patch.object(ep, "ensure_apt_packages", lambda state: None), \
                mock.patch.object(ep, "ensure_extra_requirements", lambda version: None), \
                mock.patch.object(ep, "installed_versions", side_effect=lambda: sorted(done & set(tried))), \
                mock.patch.object(ep, "venv_ok", side_effect=lambda v: v in done and v in tried), \
                mock.patch.object(ep, "install", side_effect=install):
            try:
                ep._prepare()
                exit_code = None
            except SystemExit as err:
                exit_code = err.code
        state = ep.load_state()
        return ep, tried, exit_code, state

    def test_a_failed_install_of_the_newest_falls_back_to_the_image_default(self):
        ep, tried, exit_code, state = self._prepare("2026.10.0", {"2026.8.3": True},
                                                    HA_VERSION_DEFAULT="2026.8.3")
        self.assertEqual(tried, ["2026.10.0", "2026.8.3"])
        self.assertIsNone(exit_code, "the boot goes on with the version the image was built with")
        self.assertEqual(state.get("desired"), "2026.8.3", "so the next boot does not try the broken one again")
        self.assertIn("2026.10.0", state.get("last_error", ""))

    def test_the_fallback_is_never_below_the_images_floor(self):
        ep, tried, exit_code, state = self._prepare("2026.10.0", {"2026.9.1": True},
                                                    HA_VERSION_DEFAULT="2026.8.3", HA_VERSION_MIN="2026.9.1")
        self.assertEqual(tried, ["2026.10.0", "2026.9.1"])
        self.assertEqual(state.get("desired"), "2026.9.1")

    def test_when_the_default_fails_as_well_the_boot_still_ends(self):
        ep, tried, exit_code, state = self._prepare("2026.10.0", {}, HA_VERSION_DEFAULT="2026.8.3")
        self.assertEqual(tried, ["2026.10.0", "2026.8.3"], "tried once, not in a loop")
        self.assertEqual(exit_code, 1)

    def test_the_default_is_not_installed_twice_when_it_is_what_failed(self):
        ep, tried, exit_code, state = self._prepare(None, {}, HA_VERSION_DEFAULT="2026.8.3", HA_VERSION_LATEST="0")
        self.assertEqual(tried, ["2026.8.3"])
        self.assertEqual(exit_code, 1)


class InfiniteMqttSettingTest(unittest.TestCase):
    """F-17: json.load reads 1e999 as an infinity; int() refuses it with OverflowError, not ValueError."""

    def test_an_infinite_interval_on_disk_loads_as_the_default(self):
        pub = _config_publisher()
        with open(pub.path, "w", encoding="utf-8") as fh:
            fh.write('{"republish_interval_s": 1e999, "port": 1884}')
        with self.assertLogs(mp._LOGGER, "WARNING") as logs:
            config = pub._load()  # before the fix: OverflowError out of __init__, so no component at all
        self.assertEqual(config.republish_interval_s,
                         mp.MqttConfig.__dataclass_fields__["republish_interval_s"].default)
        self.assertEqual(config.port, 1884, "the other settings still come through")
        self.assertIn("republish_interval_s", "\n".join(logs.output))

    def test_the_json_word_infinity_is_the_same_case(self):
        pub = _config_publisher()
        with open(pub.path, "w", encoding="utf-8") as fh:
            fh.write('{"full_republish_interval_min": Infinity}')
        with self.assertLogs(mp._LOGGER, "WARNING"):
            config = pub._load()
        self.assertEqual(config.full_republish_interval_min,
                         mp.MqttConfig.__dataclass_fields__["full_republish_interval_min"].default)

    def test_the_form_answers_an_infinity_with_a_plain_error(self):
        pub = _config_publisher()
        with self.assertRaises(ValueError) as caught:  # a 400, not an unhandled OverflowError
            pub._validated({"republish_interval_s": INFINITY})
        self.assertIn("must be an integer", str(caught.exception))

    def test_bounded_still_clamps_an_ordinary_number(self):
        self.assertEqual(mp._bounded("republish_interval_s", 5), 30)
        self.assertEqual(mp._bounded("republish_interval_s", 10**12), 86400)


class InfiniteSettingTest(unittest.TestCase):
    """F-17, second half: the same value hand-edited into settings.json."""

    def _settings(self, raw):
        state_dir = tempfile.mkdtemp()
        with open(os.path.join(state_dir, "settings.json"), "w", encoding="utf-8") as fh:
            fh.write(raw)
        return st.Settings(state_dir)

    def test_an_infinite_number_reads_back_as_its_default(self):
        settings = self._settings('{"health_stale_s": 1e999}')
        self.assertEqual(settings.int_("health_stale_s", 60, 86400), st.DEFAULTS["health_stale_s"])

    def test_the_settings_api_still_answers(self):
        # public() is what GET /api/settings returns: an OverflowError here was a 500 with no way back
        settings = self._settings('{"health_stale_s": 1e999, "backup_keep": Infinity, "watchdog_after_min": -Infinity}')
        public = settings.public()
        self.assertEqual(public["health_stale_s"], st.DEFAULTS["health_stale_s"])
        self.assertEqual(public["backup_keep"], 5)
        self.assertEqual(public["watchdog_after_min"], st.DEFAULTS["watchdog_after_min"])

    def test_an_infinite_per_integration_override_is_ignored(self):
        settings = self._settings('{"health": {"demo": {"stale_s": 1e999}}}')
        self.assertEqual(settings.health_for("demo")["stale_s"], st.DEFAULTS["health_stale_s"])


class AptRemoveOperatorTest(unittest.TestCase):
    """C-5: HRI_APT_PACKAGES names packages to install; it can never name one to remove."""

    def wanted(self, value):
        return entrypoint_for(self, tempfile.mkdtemp(), HRI_APT_PACKAGES=value).apt_packages_wanted()

    def test_a_trailing_dash_is_refused(self):
        for value in ("libturbojpeg0-", "ffmpeg-", "libc6:arm64-"):
            with self.subTest(value=value):
                self.assertEqual(self.wanted(value), ([], [value]))

    def test_ordinary_names_still_pass(self):
        names = ["ffmpeg", "bluez", "jq", "libpcap0.8", "libturbojpeg0", "python3-dev", "zlib1g-dev",
                 "libatlas-base-dev", "g++", "libstdc++6", "libc6:arm64"]
        self.assertEqual(self.wanted(" ".join(names)), (names, []))


if __name__ == "__main__":
    unittest.main()
