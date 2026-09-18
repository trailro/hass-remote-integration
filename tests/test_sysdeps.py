"""Requirements that install perfectly but only wrap a program or a shared library the image does not
carry: the map, the lookup in this container, and the warning's way to the start gate, the Preflight
report and the environment builder's Check."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import build_views, manage_views, preflight
from custom_components.integration_manager.installer import Installer, State


def _hass(record=None):
    async def job(fn, *args):
        if record is not None:
            record.append(fn)
        return fn(*args)
    return SimpleNamespace(async_add_executor_job=job, is_running=True, loop=mock.Mock(), async_create_task=mock.Mock())


def _installer(test, requirements, running="1.0", target="2.0"):
    """An installer whose store holds ``target``: a manifest with ``requirements`` and one clean module."""
    d = tempfile.mkdtemp(prefix="hri-sysdep-")
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    inst = object.__new__(Installer)
    inst.config_dir, inst.state_dir = d, os.path.join(d, "integration_manager")
    inst.versions_dir = os.path.join(inst.state_dir, "versions")
    inst.constraints = ""
    inst._req_versions_cache = {}
    inst.state = State(domain="demo", installed={"demo": {"running_tag": running, "versions": {
        running: {}, target: {"installed_at": "2026-09-01T10:00:00", "min_ha": None}}}})
    inst.spec = lambda dom: {"repo": "owner/repo"}
    inst.settings = SimpleNamespace(github_headers=lambda: {})
    inst.installed_manifest = lambda dom=None: {"version": running}
    inst._entries_of = lambda dom: []
    inst.site_packages_for = lambda dom: d
    stored = inst._version_dir("demo", target)
    os.makedirs(stored, exist_ok=True)
    with open(os.path.join(stored, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"domain": "demo", "version": target, "config_flow": True, "requirements": requirements}, fh)
    with open(os.path.join(stored, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("X = 1\n")
    return inst


def _run(inst, install_rows=(), target="2.0", record=None):
    pip = {"ok": True, "install": list(install_rows), "stderr": ""}
    with mock.patch.object(preflight, "_pip_dry_run", return_value=pip):
        return asyncio.run(preflight.run(_hass(record), inst, "demo", target, source_dir=inst._version_dir("demo", target)))


def _sysdep_warning(report):
    return next((w for w in report["warnings"] if "is a wrapper over" in w), None)


class NoCache(unittest.TestCase):
    """The lookup is cached for the life of the process; a test must not inherit another test's image."""

    def setUp(self):
        preflight._PRESENT.clear()
        self.addCleanup(preflight._PRESENT.clear)


class MapTest(NoCache):
    def test_a_missing_program_warns_and_names_it(self):
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            warnings = preflight._system_dep_warnings(["ha-ffmpeg==3.2.2"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("ha-ffmpeg is a wrapper over the program ffmpeg", warnings[0])
        self.assertIn("does not have", warnings[0])
        self.assertTrue(warnings[0].endswith("Set HRI_APT_PACKAGES=ffmpeg (next to what it already names) "
                                             "and recreate the container"), warnings[0])

    def test_a_program_that_is_there_says_nothing(self):
        with mock.patch.object(preflight.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(preflight._system_dep_warnings(["ha-ffmpeg==3.2.2"]), [])

    def test_a_missing_library_warns(self):
        with mock.patch.object(preflight, "_library_present", return_value=False):
            warnings = preflight._system_dep_warnings(["PyTurboJPEG"])
        self.assertIn("PyTurboJPEG is a wrapper over the library libturbojpeg.so.0", warnings[0])

    def test_a_library_that_is_there_says_nothing(self):
        with mock.patch.object(preflight, "_library_present", return_value=True):
            self.assertEqual(preflight._system_dep_warnings(["PyTurboJPEG"]), [])

    def test_a_package_not_in_the_map_is_untouched(self):
        with mock.patch.object(preflight.shutil, "which", return_value=None), \
                mock.patch.object(preflight, "_library_present", return_value=False):
            self.assertEqual(preflight._system_dep_warnings(["aiohttp==3.9.0", "pyserial>=3.5"]), [])

    def test_the_name_is_matched_the_way_pypi_does(self):
        """Case, "-" and "_" do not matter, and a version, extras or a marker are not part of the name."""
        for req in ("PyTurboJPEG", "pyturbojpeg==1.7.5", "PYTURBOJPEG[extra]>=1.0",
                    "pyturbojpeg ; sys_platform == 'linux'"):
            with mock.patch.object(preflight, "_library_present", return_value=False):
                self.assertEqual(len(preflight._system_dep_warnings([req])), 1, req)
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            self.assertEqual(len(preflight._system_dep_warnings(["HA_FFmpeg==3.2.2"])), 1)

    def test_the_same_package_twice_warns_once(self):
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            self.assertEqual(len(preflight._system_dep_warnings(["ha-ffmpeg==3.2.2", "HA_FFmpeg"])), 1)

    def test_every_entry_of_the_map_is_canonical(self):
        for name in preflight._SYSTEM_DEPS:
            self.assertEqual(name, preflight._canon(name))
            self.assertTrue(any(preflight._SYSTEM_DEPS[name]), name)  # an entry that needs nothing is a typo

    def test_the_warning_names_the_debian_package_of_the_library(self):
        with mock.patch.object(preflight, "_library_present", return_value=False):
            warnings = preflight._system_dep_warnings(["PyTurboJPEG"])
        self.assertIn("Set HRI_APT_PACKAGES=libturbojpeg0", warnings[0])

    def test_a_dependency_without_a_known_package_invents_none(self):
        """No line in _DEBIAN_PACKAGE: the warning still says what is missing and nothing about apt."""
        with mock.patch.dict(preflight._DEBIAN_PACKAGE, {}, clear=True), \
                mock.patch.object(preflight.shutil, "which", return_value=None):
            warnings = preflight._system_dep_warnings(["ha-ffmpeg==3.2.2"])
        self.assertIn("ha-ffmpeg is a wrapper over the program ffmpeg", warnings[0])
        self.assertNotIn("HRI_APT_PACKAGES", warnings[0])

    def test_only_what_is_missing_is_asked_for(self):
        """An entry whose program is there and whose library is not names the library's package only."""
        with mock.patch.dict(preflight._SYSTEM_DEPS, {"demo": (("ffmpeg",), ("libGL.so.1",))}), \
                mock.patch.object(preflight.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(preflight, "_library_present", return_value=False):
            warnings = preflight._system_dep_warnings(["demo"])
        self.assertIn("Set HRI_APT_PACKAGES=libgl1 ", warnings[0])
        self.assertNotIn("ffmpeg", warnings[0])

    def test_every_program_and_library_of_the_map_has_a_package(self):
        """A new entry without a line in _DEBIAN_PACKAGE is deliberate; this test says which one it is."""
        for name, (bins, libs) in preflight._SYSTEM_DEPS.items():
            for dep in bins + libs:
                self.assertIn(dep, preflight._DEBIAN_PACKAGE, f"{name} wants {dep}")

    def test_no_package_line_is_left_over(self):
        wanted = {dep for bins, libs in preflight._SYSTEM_DEPS.values() for dep in bins + libs}
        self.assertEqual(set(preflight._DEBIAN_PACKAGE) - wanted, set())

    def test_the_package_names_are_debian_package_names(self):
        """What the warning tells the operator to set has to survive entrypoint.APT_PACKAGE_RE."""
        import re

        allowed = re.compile(r"[a-z0-9][a-z0-9+.-]+")
        for dep, pkg in preflight._DEBIAN_PACKAGE.items():
            self.assertTrue(allowed.fullmatch(pkg), f"{dep} -> {pkg}")

    def test_pydub_wants_the_ffmpeg_binary(self):
        """pydub.utils.get_encoder_name() shells out to ffmpeg/avconv: it imports, and decodes nothing."""
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            warnings = preflight._system_dep_warnings(["pydub==0.25.1"])
        self.assertIn("pydub is a wrapper over the program ffmpeg", warnings[0])


class BluetoothTest(NoCache):
    """bleak, bluetooth-adapters, habluetooth talk to BlueZ over the system D-Bus and dbus-fast opens the
    socket itself: they install and import perfectly, and there is no adapter in the container."""

    def _warn(self, reqs, socket=False):
        sockets = ("/nonexistent-hri/system_bus_socket",)
        if socket:
            d = tempfile.mkdtemp(prefix="hri-dbus-")
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
            path = os.path.join(d, "system_bus_socket")
            open(path, "w").close()
            sockets = (path,)
        with mock.patch.object(preflight, "_DBUS_SOCKETS", sockets):
            return preflight._system_dep_warnings(reqs)

    def test_a_bluetooth_requirement_warns_about_the_host_stack(self):
        warnings = self._warn(["bleak==3.0.2"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("bleak needs the host's Bluetooth stack, which a container cannot provide by itself",
                      warnings[0])
        self.assertIn("bluetoothd", warnings[0])
        self.assertIn("README", warnings[0])

    def test_it_does_not_claim_a_package_would_fix_it(self):
        warnings = self._warn(["habluetooth==7.0.0"])
        self.assertIn("No package installs that", warnings[0])
        self.assertNotIn("wrapper over", warnings[0])  # a different problem from _SYSTEM_DEPS, said differently
        self.assertNotIn("HRI_APT_PACKAGES", warnings[0])  # and no setting to try: the adapter is on the host

    def test_every_name_of_the_set_is_canonical_and_warns(self):
        for name in preflight._BLUETOOTH_DEPS:
            self.assertEqual(name, preflight._canon(name))
            self.assertEqual(len(self._warn([name])), 1, name)
            preflight._PRESENT.clear()

    def test_a_mounted_host_dbus_socket_says_nothing(self):
        """The operator who mounted it has already done the compose work; the warning would be noise."""
        self.assertEqual(self._warn(["bleak", "dbus-fast"], socket=True), [])

    def test_the_socket_is_looked_for_once(self):
        with mock.patch.object(preflight, "_DBUS_SOCKETS", ("/nonexistent-hri/system_bus_socket",)), \
                mock.patch.object(preflight.os.path, "exists", mock.Mock(return_value=False)) as exists:
            for _ in range(3):
                preflight._system_dep_warnings(["bleak", "habluetooth"])
        exists.assert_called_once_with("/nonexistent-hri/system_bus_socket")

    def test_it_reaches_the_report(self):
        inst = _installer(self, ["bleak==3.0.2"])
        with mock.patch.object(preflight, "_DBUS_SOCKETS", ("/nonexistent-hri/system_bus_socket",)):
            report = _run(inst)
        self.assertTrue(report["ok"])
        self.assertIn("bleak needs the host's Bluetooth stack", "\n".join(report["warnings"]))


class LookupCacheTest(NoCache):
    def test_the_image_is_looked_at_once_per_program(self):
        which = mock.Mock(return_value=None)
        with mock.patch.object(preflight.shutil, "which", which):
            for _ in range(3):
                preflight._system_dep_warnings(["ha-ffmpeg", "ffmpeg-python"])  # both want ffmpeg
        which.assert_called_once_with("ffmpeg")

    def test_and_once_per_library(self):
        lib = mock.Mock(return_value=False)
        with mock.patch.object(preflight, "_library_present", lib):
            for _ in range(3):
                preflight._system_dep_warnings(["pyaudio", "sounddevice"])  # both want libportaudio
        lib.assert_called_once_with("libportaudio.so.2")


class LibraryLookupTest(NoCache):
    def test_a_multiarch_directory_counts(self):
        d = tempfile.mkdtemp(prefix="hri-lib-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        os.makedirs(os.path.join(d, "x86_64-linux-gnu"))
        open(os.path.join(d, "x86_64-linux-gnu", "libturbojpeg.so.0"), "w").close()
        with mock.patch.object(preflight, "_LIB_DIRS", (d,)):
            self.assertTrue(preflight._library_present("libturbojpeg.so.0"))

    def test_nowhere_and_unknown_to_ldconfig(self):
        with mock.patch.object(preflight, "_LIB_DIRS", ("/nonexistent-hri",)), \
                mock.patch("ctypes.util.find_library", return_value=None):
            self.assertFalse(preflight._library_present("libnothing.so.9"))

    def test_ldconfig_finds_one_installed_elsewhere(self):
        with mock.patch.object(preflight, "_LIB_DIRS", ("/nonexistent-hri",)), \
                mock.patch("ctypes.util.find_library", return_value="libturbojpeg.so.0") as find:
            self.assertTrue(preflight._library_present("libturbojpeg.so.0"))
        find.assert_called_once_with("turbojpeg")  # the soname without lib and without .so.N


@unittest.skipUnless(os.path.exists("/config/venv-current"), "only inside the manager's container")
class TheRealImageTest(NoCache):
    """What the container really has: no ffmpeg, libturbojpeg since 0.16.0 (for PyTurboJPEG)."""

    def test_ffmpeg_is_not_in_the_image(self):
        self.assertIsNone(shutil.which("ffmpeg"))
        self.assertEqual(len(preflight._system_dep_warnings(["ha-ffmpeg==3.2.2"])), 1)

    def test_libturbojpeg_is(self):
        self.assertTrue(preflight._library_present("libturbojpeg.so.0"))
        self.assertEqual(preflight._system_dep_warnings(["PyTurboJPEG==1.7.5"]), [])


class ReportTest(NoCache):
    def test_a_requirement_of_the_version_warns_without_blocking(self):
        inst = _installer(self, ["ha-ffmpeg==3.2.2"])
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            report = _run(inst)
        self.assertTrue(report["ok"])
        self.assertEqual(report["blockers"], [])
        self.assertIn("ha-ffmpeg is a wrapper over the program ffmpeg", _sysdep_warning(report))

    def test_a_package_pip_pulls_in_for_it_warns_too(self):
        inst = _installer(self, ["some-camera-lib==1.0"])
        with mock.patch.object(preflight, "_library_present", return_value=False):
            report = _run(inst, [{"name": "PyTurboJPEG", "version": "1.7.5"}])
        self.assertIn("PyTurboJPEG", _sysdep_warning(report))

    def test_a_satisfied_requirement_says_nothing(self):
        inst = _installer(self, ["ha-ffmpeg==3.2.2"])
        with mock.patch.object(preflight.shutil, "which", return_value="/usr/bin/ffmpeg"):
            report = _run(inst)
        self.assertIsNone(_sysdep_warning(report))

    def test_the_container_is_not_looked_at_on_the_event_loop(self):
        inst, seen = _installer(self, ["ha-ffmpeg==3.2.2"]), []
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            _run(inst, record=seen)
        self.assertIn(preflight._system_dep_warnings, seen)


class ItReachesTheStartGateTest(NoCache):
    def _start(self, gate):
        view = object.__new__(manage_views.RunView)
        view.installer = SimpleNamespace(hass=None, start=mock.AsyncMock(return_value={"ok": True, "tag": "2.0"}))
        view.publisher = SimpleNamespace(stats={}, base_topic="t", async_after_start=mock.AsyncMock())
        request = SimpleNamespace(headers={}, query={}, content_type="application/json",
                                  json=mock.AsyncMock(return_value={"domain": "demo", "tag": "2.0"}))
        with mock.patch.object(manage_views.preflight, "gate", mock.AsyncMock(return_value=gate)):
            return json.loads(asyncio.run(view.post(request, action="start")).body)

    def test_the_gate_passes_and_carries_the_warning(self):
        inst = _installer(self, ["ha-ffmpeg==3.2.2"])
        preflight._REPORTS.clear()
        self.addCleanup(preflight._REPORTS.clear)
        with mock.patch.object(preflight, "_pip_dry_run", return_value={"ok": True, "install": [], "stderr": ""}), \
                mock.patch.object(preflight.shutil, "which", return_value=None):
            res = asyncio.run(preflight.gate(_hass(), inst, "demo", "2.0"))
        self.assertFalse(res["blocked"])
        self.assertIn("ha-ffmpeg", _sysdep_warning(res["report"]))
        started = self._start(res)
        self.assertTrue(started["ok"])
        self.assertIn("ha-ffmpeg", "\n".join(started["preflight_warnings"]))


class ItReachesTheBuilderCheckTest(NoCache):
    def test_check_answers_with_it(self):
        inst = _installer(self, ["ha-ffmpeg==3.2.2"])  # installed_domain is "demo": no "replaces" warning
        view = object.__new__(build_views.BuildCheckView)
        view.hass, view.installer, view.updater = _hass(), inst, None
        view._pf, view._checks = SimpleNamespace(_lock=asyncio.Lock()), {}
        request = SimpleNamespace(headers={}, query={}, content_type="application/json",
                                  json=mock.AsyncMock(return_value={"domain": "demo", "ref": "2.0"}))

        async def stored(hass, installer, domain, ref, *args, **kwargs):  # the Check downloads; check the stored copy
            return await real_run(hass, installer, domain, ref, source_dir=installer._version_dir(domain, "2.0"))

        real_run = preflight.run
        with mock.patch.object(build_views, "_commit_of", mock.AsyncMock(return_value="abc1234")), \
                mock.patch.object(build_views.preflight, "run", stored), \
                mock.patch.object(preflight, "_pip_dry_run", return_value={"ok": True, "install": [], "stderr": ""}), \
                mock.patch.object(preflight.shutil, "which", return_value=None):
            res = json.loads(asyncio.run(view.post(request)).body)
        self.assertTrue(res["ok"])
        self.assertTrue(res["report"]["ok"])
        self.assertIn("ha-ffmpeg is a wrapper over the program ffmpeg", _sysdep_warning(res["report"]))


if __name__ == "__main__":
    unittest.main()
