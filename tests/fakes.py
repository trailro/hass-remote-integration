"""Small stand-ins for the installer, the HA updater and the MQTT publisher."""

from types import SimpleNamespace


class FakeInstaller:
    LOCAL_TAG = "local"

    def __init__(self, running=None, running_tag=None, versions=(), updates=None, spec=None, log=None):
        self.running = running
        self.running_tag = running_tag
        self.state = SimpleNamespace(installed={running: {"versions": dict.fromkeys(versions, {})}} if running else {})
        self.updates = dict(updates or {})
        self._spec = spec or {}
        self.busy = False
        self.log = log if log is not None else []

    def spec(self, domain):
        return self._spec if domain else {}

    def _patch_status(self, domain):
        return None

    async def restart(self):
        self.log.append(("restart",))


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
