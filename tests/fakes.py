"""Small stand-ins for the installer, the HA updater and the MQTT publisher, and entrypoint imported for a test."""

import importlib
import os
import sys
from types import SimpleNamespace
from unittest import mock


def entrypoint_for(test, cfg, **env):
    """entrypoint imported for ``cfg`` (HRI_CONFIG) and ``env``; the environment and the module other tests imported
    come back after the test.  entrypoint reads HRI_CONFIG and HRI_PORT at import, and a test that set them and
    popped them afterwards took the container's own values away from every test after it (auth.COOKIE, computed
    from the real HRI_PORT, no longer matched what a later test read from the environment)."""
    # The image these tests run in carries HA_VERSION_LATEST, HA_VERSION_DEFAULT and HA_VERSION_MIN, and CI
    # boots that image on several Home Assistant versions: a test that reads them from the environment asks a
    # different question in every job.  Neutral values here; a test about one of them passes its own.
    versions = {"HA_VERSION_LATEST": "1", "HA_VERSION_DEFAULT": "2026.8.3", "HA_VERSION_MIN": ""}
    patch = mock.patch.dict(os.environ, {"HRI_CONFIG": cfg, **versions, **env})
    patch.start()
    test.addCleanup(patch.stop)
    previous = sys.modules.pop("entrypoint", None)

    def restore():
        if previous is None:
            sys.modules.pop("entrypoint", None)
        else:
            sys.modules["entrypoint"] = previous

    test.addCleanup(restore)
    return importlib.import_module("entrypoint")


class FakeInstaller:
    LOCAL_TAG = "local"

    def __init__(self, running=None, running_tag=None, versions=(), updates=None, spec=None, log=None):
        self.running = running
        self.running_tag = running_tag
        self.state = SimpleNamespace(installed={running: {"versions": dict.fromkeys(versions, {})}} if running else {})
        self.updates = dict(updates or {})
        self._spec = spec or {}
        self.busy = False
        self.restart_result = {"ok": True}
        self.log = log if log is not None else []

    def spec(self, domain):
        return self._spec if domain else {}

    def _patch_status(self, domain):
        return None

    async def restart(self):
        self.log.append(("restart",))
        return self.restart_result


class FakeUpdater:
    def __init__(self, latest_stable=None):
        self._cache = (0.0, {"latest_stable": latest_stable}) if latest_stable else None


class FakePublisher:
    def __init__(self, commands=True, log=None):
        self.config = SimpleNamespace(manager_commands=commands)
        self.log = log if log is not None else []

    def publish_manager(self):
        self.log.append(("manager",))

    def publish_manager_result(self, result):
        self.log.append(("result", result))

    async def async_publish_manager_result(self, result):
        self.log.append(("result", result))

    async def async_after_start(self, res):
        self.log.append(("after_start", res))

    def _finish(self, rec, state, error=None, result=None):
        self.log.append(("finish", rec, state, error))
