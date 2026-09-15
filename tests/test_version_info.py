"""The manager version and the build it came from, shown in the top bar and the status API."""

import importlib
import json
import os
import unittest
from unittest import mock


class VersionInfoTest(unittest.TestCase):
    def _ui(self, build):
        from custom_components.integration_manager import ui

        # runs after the environment below is restored: the module reads HRI_BUILD as the process has it again
        self.addCleanup(importlib.reload, ui)
        with mock.patch.dict(os.environ, {"HRI_BUILD": build} if build is not None else {}, clear=False):
            if build is None:
                os.environ.pop("HRI_BUILD", None)
            from custom_components.integration_manager import ui

            return importlib.reload(ui)

    def test_version_from_the_manifest_and_short_commit(self):
        ui = self._ui("0e6a8161234567890abcdef")
        with open(os.path.join(os.path.dirname(ui.__file__), "manifest.json"), encoding="utf-8") as fh:
            version = json.load(fh)["version"]
        self.assertEqual(ui.version_info(), {"version": version, "build": "0e6a8161234567890abcdef", "build_short": "0e6a816"})
        bar = ui.topbar("/")
        self.assertIn(f"v{version} · 0e6a816", bar)
        self.assertIn(f"/releases/tag/v{version}", bar)

    def test_local_build(self):
        ui = self._ui(None)
        self.assertEqual(ui.version_info()["build_short"], "local")
        self.assertIn("· local", ui.topbar("/"))


if __name__ == "__main__":
    unittest.main()
