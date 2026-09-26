"""The CI check that the newest released HRI Manager accepts and stamps app/config.yaml (.github/manager_compat_check.py),
against a stand-in for the manager's hrimgr.stamp: the real one is checked out in CI, not in the container."""

import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "manager_compat_check.py"

STAMP = '''
import yaml

ACCEPTED = {accepted!r}
FAIL_STAMP = {fail_stamp!r}


class TemplateError(Exception):
    pass


def vet_template(data):
    for key in data:
        if key not in ACCEPTED:
            raise TemplateError(f"HRI's app definition has {{key!r}}, which this manager version does not accept")


def parse_template(raw):
    data = yaml.safe_load(raw)
    vet_template(data)
    return data


def stamp(template, name, version, channel, bluetooth=False):
    if FAIL_STAMP and bluetooth:
        raise TemplateError("a backup_exclude entry names HRI's slug elsewhere")
    return dict(template, slug="hri_" + name, version=version, **({{"host_dbus": True}} if bluetooth else {{}}))


def dump(config, source):
    return yaml.safe_dump(config).encode()
'''


@unittest.skipUnless(SCRIPT.is_file() and (ROOT / "app" / "config.yaml").is_file(), ".github or app/ not copied next to the tests")
class ManagerCompatTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("manager_compat_check", SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.keys = list(yaml.safe_load((ROOT / "app" / "config.yaml").read_text(encoding="utf-8")))
        path = mock.patch.object(sys, "path", list(sys.path))
        path.start()
        self.addCleanup(path.stop)
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)

    def _manager(self, accepted, fail_stamp=False):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = pathlib.Path(tmp, "hri_manager", "hrimgr")
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")
        (pkg / "stamp.py").write_text(textwrap.dedent(STAMP.format(accepted=set(accepted), fail_stamp=fail_stamp)), encoding="utf-8")
        for name in [m for m in sys.modules if m == "hrimgr" or m.startswith("hrimgr.")]:
            del sys.modules[name]
        return pathlib.Path(tmp)

    def test_a_manager_that_knows_every_key_passes(self):
        self.assertEqual(self.mod.check(self._manager(self.keys), ROOT), [])

    def test_a_key_the_manager_refuses_is_named(self):
        problems = self.mod.check(self._manager([k for k in self.keys if k not in ("backup_pre", "uart")]), ROOT)
        self.assertEqual([p.split(" (", 1)[0] for p in problems],
                         [f"release a manager that accepts {k} first" for k in self.keys if k in ("backup_pre", "uart")])

    def test_a_stamp_that_fails_fails(self):
        problems = self.mod.check(self._manager(self.keys, fail_stamp=True), ROOT)
        self.assertEqual(len(problems), 2)  # both channels, with Bluetooth
        self.assertTrue(all(p.startswith("release a manager that stamps this app/config.yaml first") for p in problems))

    def test_the_exit_status(self):
        for accepted, rc in ((self.keys, 0), (self.keys[1:], 1)):
            with self.subTest(rc=rc):
                manager = self._manager(accepted)
                err, out = io.StringIO(), io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                    self.assertEqual(self.mod.main(["x", str(manager), str(ROOT)]), rc)
                self.assertIn("HRI Manager 9.9.9", (err if rc else out).getvalue())
                if rc:
                    self.assertIn(f"release a manager that accepts {self.keys[0]} first", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.mod.main(["x"]), 2)


if __name__ == "__main__":
    unittest.main()
