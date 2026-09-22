"""HRI_APT_PACKAGES: the Debian packages the entrypoint installs before Home Assistant starts.

Nothing here may end the boot: a name that is not a package, no network, an unknown package - each is
logged, recorded in ha.json for the System page, and the boot goes on.
"""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.fakes import entrypoint_for


class FakePopen:
    """subprocess.Popen as _run_pip uses it (context manager, wait): records how it was called."""

    calls: list = []

    def __init__(self, cmd, **kw):
        FakePopen.calls.append((cmd, kw))
        self.pid = os.getpid()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def wait(self, timeout=None):
        return 0


class AptParseTest(unittest.TestCase):
    def wanted(self, value):
        ep = entrypoint_for(self, tempfile.mkdtemp(), HRI_APT_PACKAGES=value)
        return ep.apt_packages_wanted()

    def test_names_are_split_on_spaces_and_commas(self):
        self.assertEqual(self.wanted(" ffmpeg, bluez  libpcap0.8\tjq "), (["ffmpeg", "bluez", "libpcap0.8", "jq"], []))

    def test_the_same_name_twice_is_installed_once(self):
        self.assertEqual(self.wanted("ffmpeg ffmpeg,ffmpeg"), (["ffmpeg"], []))

    def test_an_architecture_qualifier_is_a_package_name(self):
        self.assertEqual(self.wanted("libc6:arm64"), (["libc6:arm64"], []))

    def test_unset_and_empty_ask_for_nothing(self):
        self.assertEqual(self.wanted(""), ([], []))
        self.assertEqual(self.wanted("  ,  "), ([], []))

    def test_anything_that_is_not_a_package_name_is_refused(self):
        for value in ("--allow-downgrades", "-y", "https://example.invalid/x.deb", "./local.deb", "/tmp/x.deb",
                      "FFmpeg", "$(reboot)", "`reboot`", "ffmpeg&&reboot", "ffmpeg;reboot", "ffmpeg|tee", "a",
                      "ffmpeg>out", "pkg=1.2", "pkg_name"):
            with self.subTest(value=value):
                packages, refused = self.wanted(value)
                self.assertEqual(packages, [], value)
                self.assertEqual(refused, [value], value)

    def test_a_refused_name_next_to_a_good_one_keeps_the_good_one(self):
        self.assertEqual(self.wanted("ffmpeg --force-yes"), (["ffmpeg"], ["--force-yes"]))


class AptInstallTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cfg, "integration_manager"))

    def ep(self, value):
        return entrypoint_for(self, self.cfg, HRI_APT_PACKAGES=value)

    def test_valid_packages_are_installed_and_recorded(self):
        ep, state, installed = self.ep("ffmpeg jq"), {}, []
        with mock.patch.object(ep, "_dpkg_installed", return_value=False), \
                mock.patch.object(ep, "_apt_install", side_effect=installed.append):
            ep.ensure_apt_packages(state)
        self.assertEqual(installed, [["ffmpeg", "jq"]])
        self.assertEqual(state["apt"]["packages"], ["ffmpeg", "jq"])
        self.assertTrue(state["apt"]["ok"])
        self.assertIn("installed ffmpeg, jq", state["apt"]["note"])
        self.assertEqual(state["apt"]["refused"], [])

    def test_already_installed_skips_apt(self):
        """A restart must not download hundreds of MB again."""
        ep, state = self.ep("ffmpeg"), {}
        with mock.patch.object(ep, "_dpkg_installed", return_value=True), \
                mock.patch.object(ep, "_apt_install", side_effect=AssertionError("apt must not run")):
            ep.ensure_apt_packages(state)
        self.assertTrue(state["apt"]["ok"])
        self.assertEqual(state["apt"]["note"], "already installed")

    def test_only_what_is_missing_is_installed(self):
        ep, state, installed = self.ep("ffmpeg jq"), {}, []
        with mock.patch.object(ep, "_dpkg_installed", side_effect=lambda name: name == "ffmpeg"), \
                mock.patch.object(ep, "_apt_install", side_effect=installed.append):
            ep.ensure_apt_packages(state)
        self.assertEqual(installed, [["jq"]])

    def test_a_refused_name_is_recorded_and_the_boot_goes_on(self):
        ep, state, installed = self.ep("ffmpeg --allow-downgrades"), {}, []
        with mock.patch.object(ep, "_dpkg_installed", return_value=False), \
                mock.patch.object(ep, "_apt_install", side_effect=installed.append):
            ep.ensure_apt_packages(state)
        self.assertEqual(installed, [["ffmpeg"]])  # never in apt-get's argv
        self.assertEqual(state["apt"]["refused"], ["--allow-downgrades"])
        self.assertFalse(state["apt"]["ok"])
        self.assertIn("not a Debian package name", state["apt"]["error"])

    def test_only_refused_names_install_nothing(self):
        ep, state = self.ep("--allow-downgrades"), {}
        with mock.patch.object(ep, "_dpkg_installed", return_value=False), \
                mock.patch.object(ep, "_apt_install", side_effect=AssertionError("apt must not run")):
            ep.ensure_apt_packages(state)
        self.assertFalse(state["apt"]["ok"])
        self.assertEqual(state["apt"]["note"], "nothing to install")

    def test_a_failed_apt_is_recorded_and_the_boot_goes_on(self):
        ep, state = self.ep("ffmpeg"), {}
        err = subprocess.CalledProcessError(100, ["apt-get", "install"])
        with mock.patch.object(ep, "_dpkg_installed", return_value=False), \
                mock.patch.object(ep, "_apt_install", side_effect=err):
            ep.ensure_apt_packages(state)  # no exception: a failure is not fatal
        self.assertFalse(state["apt"]["ok"])
        self.assertIn("CalledProcessError", state["apt"]["error"])
        self.assertIn("not installed: ffmpeg", state["apt"]["note"])

    def test_a_hung_apt_ends_like_a_hung_pip(self):
        ep, state = self.ep("ffmpeg"), {}
        with mock.patch.object(ep, "_dpkg_installed", return_value=False), \
                mock.patch.object(ep, "_apt_install", side_effect=subprocess.TimeoutExpired(["apt-get"], ep.PIP_IDLE_TIMEOUT_S)):
            ep.ensure_apt_packages(state)
        self.assertFalse(state["apt"]["ok"])
        self.assertIn("TimeoutExpired", state["apt"]["error"])

    def test_an_unset_variable_drops_the_record_of_an_earlier_boot(self):
        ep, state = self.ep(""), {"apt": {"packages": ["ffmpeg"], "ok": True}}
        ep.ensure_apt_packages(state)
        self.assertNotIn("apt", state)

    def test_the_status_page_gets_a_phase_of_its_own(self):
        ep, state, seen = self.ep("ffmpeg"), {}, []
        with mock.patch.object(ep, "_dpkg_installed", return_value=False), \
                mock.patch.object(ep, "_apt_install", side_effect=lambda _p: seen.append(ep.install_status())):
            ep.ensure_apt_packages(state)
        self.assertIn("ffmpeg", seen[0]["phase"])
        self.assertIn("system packages", seen[0]["phase"])
        self.assertTrue(seen[0]["installing"])  # the /api/ 503 body while it runs


class AptCommandTest(unittest.TestCase):
    """What _apt_install actually runs: no shell, into its own log file, lists cleaned afterwards."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        self.lists = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.lists, "partial"))
        with open(os.path.join(self.lists, "deb.debian.org_dists_trixie_Release"), "w", encoding="utf-8") as fh:
            fh.write("x" * 100)
        self.ep = entrypoint_for(self, self.cfg, HRI_APT_PACKAGES="ffmpeg")
        patch = mock.patch.object(self.ep, "APT_LISTS_DIR", self.lists)
        patch.start()
        self.addCleanup(patch.stop)

    def run_install(self, packages=("ffmpeg",)):
        FakePopen.calls = []
        with mock.patch.object(self.ep.subprocess, "Popen", FakePopen):
            self.ep._apt_install(list(packages))
        return FakePopen.calls

    def test_apt_is_run_without_a_shell(self):
        calls = self.run_install(["ffmpeg", "libpcap0.8"])
        self.assertEqual([cmd for cmd, _kw in calls],
                         [["apt-get", "update"], ["apt-get", "-o", "APT::Cmd::Pattern-Only=true", "install", "-y", "--no-install-recommends", "ffmpeg", "libpcap0.8"]])
        for cmd, kw in calls:
            self.assertIsInstance(cmd, list)
            self.assertFalse(kw.get("shell"), kw)  # the value never reaches a shell
            self.assertEqual(kw["env"]["DEBIAN_FRONTEND"], "noninteractive")  # a question would hang the boot

    def test_the_log_file_is_written_next_to_the_home_assistant_install_log(self):
        def write(cmd, out, **_kw):
            out.write(f"$ {' '.join(cmd)}\n")
            out.flush()

        with mock.patch.object(self.ep, "_run_pip", write):
            self.ep._apt_install(["ffmpeg"])
        self.assertEqual(self.ep.APT_LOG_FILE, os.path.join(os.path.dirname(self.ep.LOG_FILE), "apt-install.log"))
        with open(self.ep.APT_LOG_FILE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# apt-get install ffmpeg", text)
        self.assertIn("$ apt-get -o APT::Cmd::Pattern-Only=true install -y --no-install-recommends ffmpeg", text)
        with mock.patch.object(self.ep, "_run_pip", write):  # one install per file, as ha-install.log
            self.ep._apt_install(["jq"])
        with open(self.ep.APT_LOG_FILE, encoding="utf-8") as fh:
            self.assertNotIn("ffmpeg", fh.read())

    def test_the_package_lists_are_removed_afterwards(self):
        self.run_install()
        self.assertTrue(os.path.isdir(self.lists))  # apt-get needs the directory itself
        self.assertEqual(os.listdir(self.lists), [])

    def test_a_missing_lists_directory_is_not_an_error(self):
        os.rmdir(os.path.join(self.lists, "partial"))
        os.remove(os.path.join(self.lists, "deb.debian.org_dists_trixie_Release"))
        os.rmdir(self.lists)
        self.run_install()

    def test_dpkg_is_asked_for_one_package_at_a_time(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"installed" if cmd[-1] == "ffmpeg" else b"")

        with mock.patch.object(self.ep.subprocess, "run", fake_run):
            self.assertTrue(self.ep._dpkg_installed("ffmpeg"))
            self.assertFalse(self.ep._dpkg_installed("jq"))
        self.assertEqual(calls, [["dpkg-query", "-W", "-f=${db:Status-Status}", "ffmpeg"],
                                 ["dpkg-query", "-W", "-f=${db:Status-Status}", "jq"]])

    def test_a_package_dpkg_does_not_know_is_missing(self):
        def fake_run(cmd, **_kw):
            return subprocess.CompletedProcess(cmd, 1, stdout=b"")

        with mock.patch.object(self.ep.subprocess, "run", fake_run):
            self.assertFalse(self.ep._dpkg_installed("jq"))

    def test_without_dpkg_apt_decides(self):
        with mock.patch.object(self.ep.subprocess, "run", side_effect=OSError("no dpkg-query")):
            self.assertFalse(self.ep._dpkg_installed("jq"))


class AptBootTest(unittest.TestCase):
    def test_the_packages_are_installed_before_home_assistant_and_recorded_in_ha_json(self):
        cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(cfg, "integration_manager"))
        ep = entrypoint_for(self, cfg, HRI_APT_PACKAGES="jq", HA_VERSION_LATEST="0")
        order = []
        with mock.patch.object(ep, "_dpkg_installed", return_value=False), \
                mock.patch.object(ep, "_apt_install", side_effect=lambda p: order.append(("apt", p))), \
                mock.patch.object(ep, "fits_this_python", return_value=True), \
                mock.patch.object(ep, "install", side_effect=lambda v: order.append(("ha", v))), \
                self.assertRaises(SystemExit):  # no venv installs in this test: the boot ends after the attempt
            ep._prepare()
        self.assertEqual(order, [("apt", ["jq"]), ("ha", ep.DEFAULT_VERSION)])
        with open(os.path.join(cfg, "integration_manager", "ha.json"), encoding="utf-8") as fh:
            record = json.load(fh)["apt"]
        self.assertEqual(record["packages"], ["jq"])
        self.assertTrue(record["ok"])


class AptDocsTest(unittest.TestCase):
    """The variable is only usable when the shipped compose file passes it in, and only found when it is documented."""

    def read(self, name):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name), encoding="utf-8") as fh:
            return fh.read()

    def test_compose_passes_the_variable_into_the_container(self):
        self.assertIn('HRI_APT_PACKAGES: "${HRI_APT_PACKAGES:-}"', self.read("docker-compose.yml"))

    def test_the_readme_documents_the_variable_and_its_log(self):
        readme = self.read("README.md")
        self.assertIn("| `HRI_APT_PACKAGES` | unset |", readme)
        self.assertIn("apt-install.log", readme)


if __name__ == "__main__":
    unittest.main()
