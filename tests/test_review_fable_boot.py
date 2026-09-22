"""External review at 70c3e8c, BOOT-1: a name in HRI_APT_PACKAGES that is no package was read by apt as a regex.

Debian package names may hold "." and "+", so APT_PACKAGE_RE lets them through, and apt-get install falls back to
reading an argument that is not an exact package name, but holds one of those characters, as a regular expression
over every package name.  Checked on this image's apt (3.0.3) with ``apt-get install -s``: ``python3.1.`` selected
182 packages, ``libc6.dev`` 165 (cross-compilers included), ``li.b`` a set that could not even be resolved.
APT::Cmd::Pattern-Only turns that fallback off; exact names (``g++``, ``python3.13``, ``zlib1g-dev:arm64``) install
as before.

The first test fails on the tree before the fix; the second runs the argv the entrypoint builds through this
container's real apt-get, in simulation (-s: nothing is installed) and without package lists (an installed package
is enough), and is skipped where there is no apt.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.fakes import entrypoint_for


class AptNameIsNeverARegexTest(unittest.TestCase):

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        self.ep = entrypoint_for(self, self.cfg, HRI_APT_PACKAGES="ffmpeg")
        lists = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, lists, True)
        patch = mock.patch.object(self.ep, "APT_LISTS_DIR", lists)
        patch.start()
        self.addCleanup(patch.stop)

    def install_argv(self, packages):
        runs = []
        with mock.patch.object(self.ep, "_run_pip", lambda cmd, _out, **kw: runs.append((cmd, kw))):
            self.ep._apt_install(list(packages))
        return [(cmd, kw) for cmd, kw in runs if "install" in cmd]

    def test_the_install_turns_the_regex_fallback_off(self):
        (cmd, _kw), = self.install_argv(["python3.13", "g++"])
        self.assertEqual(cmd[:4], ["apt-get", "-o", "APT::Cmd::Pattern-Only=true", "install"])
        self.assertEqual(cmd[-2:], ["python3.13", "g++"])

    @unittest.skipUnless(shutil.which("apt-get") and shutil.which("dpkg"), "no apt here")
    def test_this_image_s_apt_takes_exact_names_only(self):
        arch = subprocess.run(["dpkg", "--print-architecture"], stdout=subprocess.PIPE, text=True, check=True).stdout.strip()

        def simulate(name):
            (cmd, kw), = self.install_argv([name])
            cmd = [*cmd[:cmd.index("install") + 1], "-s", *cmd[cmd.index("install") + 1:]]  # simulate: installs nothing
            proc = subprocess.run(cmd, env=kw.get("env"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
            return proc.returncode, proc.stdout

        for name in ("libc6", f"libc6:{arch}"):  # installed in every Debian image: resolved from dpkg's status alone
            with self.subTest(name=name):
                rc, out = simulate(name)
                self.assertEqual(rc, 0, out)
        # "li.c6" is the regex that matches libc6: without Pattern-Only apt reports libc6 and exits 0
        rc, out = simulate("li.c6")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("Unable to locate package li.c6", out)
        self.assertNotIn("regex", out)


if __name__ == "__main__":
    unittest.main()
