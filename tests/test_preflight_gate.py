"""Preflight gate before a start or switch: what is gated, the cache, and a preflight that cannot run."""

import asyncio
import unittest
from unittest import mock

from custom_components.integration_manager import preflight


class FakeInstaller:
    LOCAL_TAG = "local"

    def __init__(self, running="v1.0.0", versions=("v1.0.0", "v2.0.0"), repo="owner/repo"):
        self.state = mock.Mock(installed={"probe": {"running_tag": running, "versions": dict.fromkeys(versions, {})}})
        self._repo = repo

    def spec(self, domain):
        return {"repo": self._repo} if self._repo else {}

    def _version_dir(self, domain, tag):
        return f"/versions/{domain}/{tag}"


def _gate(installer, tag, report=None, error=None):
    preflight._REPORTS.clear()
    run = mock.AsyncMock(side_effect=error) if error else mock.AsyncMock(return_value=report)
    with mock.patch.object(preflight, "run", run):
        return asyncio.run(preflight.gate(None, installer, "probe", tag)), run


class GateTest(unittest.TestCase):
    def test_blockers_block(self):
        res, run = _gate(FakeInstaller(), "v2.0.0", {"ok": False, "blockers": ["legacy.py:5: bad"]})
        self.assertTrue(res["blocked"])
        self.assertEqual(res["report"]["blockers"], ["legacy.py:5: bad"])
        run.assert_awaited_once()

    def test_clean_or_warnings_only_do_not(self):
        res, _ = _gate(FakeInstaller(), "v2.0.0", {"ok": True, "blockers": [], "warnings": ["imports imp"]})
        self.assertFalse(res["blocked"])

    def test_the_deployed_version_is_not_gated(self):
        for tag in ("v1.0.0", None):
            res, run = _gate(FakeInstaller(), tag, {"ok": False})
            self.assertFalse(res["blocked"])
            run.assert_not_awaited()

    def test_nothing_deployed_gates_the_newest(self):
        res, run = _gate(FakeInstaller(running=None), None, {"ok": False, "blockers": ["x"]})
        self.assertTrue(res["blocked"])
        self.assertEqual(run.await_args.args[3], "v2.0.0")

    def test_dev_build_and_no_repo_are_not_gated(self):
        res, run = _gate(FakeInstaller(versions=("v1.0.0", "local")), "local", {"ok": False})
        self.assertEqual((res["blocked"], res["skipped"]), (False, "dev build"))
        res, run = _gate(FakeInstaller(repo=None), "v2.0.0", {"ok": False})
        self.assertFalse(res["blocked"])
        run.assert_not_awaited()

    def test_a_preflight_that_cannot_run_does_not_block(self):
        res, _ = _gate(FakeInstaller(), "v2.0.0", error=ValueError("GitHub answered 503"))
        self.assertFalse(res["blocked"])
        self.assertIn("GitHub answered 503", res["skipped"])

    def test_a_recent_report_is_reused(self):
        preflight._REPORTS.clear()
        preflight.remember("probe", "stored:v2.0.0\n", {"ok": False, "blockers": ["cached"]})  # the stored copy's key (no installed_at here)
        run = mock.AsyncMock()
        with mock.patch.object(preflight, "run", run):
            res = asyncio.run(preflight.gate(None, FakeInstaller(), "probe", "v2.0.0"))
        self.assertEqual(res["report"]["blockers"], ["cached"])
        run.assert_not_awaited()

    def test_a_stale_report_is_not(self):
        preflight._REPORTS.clear()
        preflight.remember("probe", "v2.0.0", {"ok": False, "blockers": ["old"]})
        with mock.patch.object(preflight.time, "monotonic", return_value=preflight.time.monotonic() + preflight.CACHE_S + 1):
            self.assertIsNone(preflight.recent("probe", "v2.0.0"))


if __name__ == "__main__":
    unittest.main()
