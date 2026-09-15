"""Preflight checks against the image's Python: source-only packages, code that does not compile, removed modules."""

import os
import tempfile
import unittest
from unittest import mock

from custom_components.integration_manager import preflight


def _component(files):
    d = tempfile.mkdtemp()
    for rel, text in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return d


class CodeChecksTest(unittest.TestCase):
    def test_clean_code(self):
        d = _component({"__init__.py": "import asyncio\nfrom .const import X\n", "const.py": "X = 1\n"})
        self.assertEqual(preflight._code_checks(d), ([], []))

    def test_syntax_error_is_reported_with_its_line(self):
        d = _component({"__init__.py": "x = 1\nprint 'old'\n"})
        errors, _ = preflight._code_checks(d)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("__init__.py:2:"))

    def test_removed_module_and_guarded_fallback(self):
        d = _component({
            "__init__.py": "import imp\nfrom asyncore import dispatcher\n",
            "compat.py": "try:\n    import telnetlib\nexcept ImportError:\n    telnetlib = None\n",
        })
        _, removed = preflight._code_checks(d)
        self.assertEqual(removed, ["__init__.py:1 imports imp", "__init__.py:2 imports asyncore"])

    def test_module_still_importable_here_is_not_reported(self):
        d = _component({"__init__.py": "import imp\n"})
        with mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertEqual(preflight._code_checks(d), ([], []))


class SourceOnlyTest(unittest.TestCase):
    def test_wheel_rows_are_not_built(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=lambda *a, **k: calls.append(a)):
            out = preflight._build_from_source("python", [{"name": "requests", "version": "2", "source_only": False}], None)
        self.assertEqual((out, calls), ([], []))

    def test_failed_build_is_reported(self):
        proc = mock.Mock(returncode=1, stderr="Building wheel\nerror: command 'gcc' failed: No such file or directory\n")
        with mock.patch("subprocess.run", return_value=proc):
            out = preflight._build_from_source("python", [{"name": "pycrypto", "version": "2.6.1", "source_only": True}], None)
        self.assertEqual(out[0]["built"], False)
        self.assertIn("gcc", out[0]["error"])


if __name__ == "__main__":
    unittest.main()
