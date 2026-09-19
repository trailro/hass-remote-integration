"""What GET /api/ha offers as versions: the newest RECENT_N stable releases plus everything this box already
has (installed on the volume, running, scheduled, the one a rollback goes back to), the rest only when asked
for, and the verdicts it can hand over without resolving anything."""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import homeassistant.const

from custom_components.integration_manager import preflight, views
from custom_components.integration_manager.ha_updater import RECENT_N, HaUpdater
from jsonio import ha_vkey
from tests.test_r4_lifecycle import make_venv

CURRENT = homeassistant.const.__version__
BASELINE = "2026.8.3"
OLD = "2025.3.1"      # a venv on the volume from long before the baseline
ANCIENT = "2014.1.0"  # only ever reachable through "show all versions"
# every stable release PyPI offers, oldest first, as available() leaves it behind.  Synthetic and above the
# baseline, so nothing here depends on which Home Assistant the test container happens to run.
NEWER = [f"2026.9.{i}" for i in range(RECENT_N + 4)]
STABLE = [ANCIENT, OLD, BASELINE, *NEWER]


def _sorted(versions) -> list[str]:
    return sorted(set(versions), key=ha_vkey)


class _Request:
    def __init__(self, query=None, fetch=True):
        self.query = query or {}
        self.headers = {"X-Requested-With": "fetch"} if fetch else {}


class _Updater:
    """A HaUpdater on an empty volume, with PyPI already answered."""

    async def asyncSetUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        os.makedirs(os.path.join(self.cfg, "integration_manager"))
        loop = asyncio.get_running_loop()
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg, path=lambda *p: os.path.join(self.cfg, *p)),
                                    async_add_executor_job=lambda f, *a: loop.run_in_executor(None, f, *a))
        # both, explicitly: the image these tests run in carries its own floor, and a test that reads it
        # would answer a different question in every build
        env = mock.patch.dict(os.environ, {"HA_VERSION_DEFAULT": BASELINE, "HA_VERSION_MIN": BASELINE})
        env.start()
        self.addCleanup(env.stop)
        preflight._HA_REPORTS.clear()
        self.addCleanup(preflight._HA_REPORTS.clear)
        self.up = HaUpdater(self.hass)
        self.up._stable = list(STABLE)
        self.up._releases = dict.fromkeys(STABLE, ">=3.13")
        self.avail = {"latest_stable": NEWER[-1], "recent": STABLE[-RECENT_N:], "error": "", "checked_at": ""}
        self.up._cache = (time.monotonic(), self.avail)  # validate() reads the same list without a call

    def _ha_json(self, **state):
        with open(os.path.join(self.cfg, "integration_manager", "ha.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    async def status(self, **kw):
        with mock.patch.object(self.up, "available", mock.AsyncMock(return_value=self.avail)):
            return await self.up.status(**kw)


class VersionListTest(_Updater, unittest.IsolatedAsyncioTestCase):

    async def test_the_default_list_is_the_newest_releases_and_the_running_version(self):
        out = await self.status()
        self.assertEqual(out["versions"], _sorted(STABLE[-RECENT_N:] + [CURRENT]))
        self.assertEqual(out["recent_n"], RECENT_N)
        self.assertNotIn(ANCIENT, out["versions"])
        self.assertIn(CURRENT, out["versions"])

    async def test_nothing_this_box_has_falls_off_the_list_however_old_it_is(self):
        make_venv(self.cfg, OLD)
        self._ha_json(desired=ANCIENT, previous=BASELINE)
        out = await self.status()
        for version in (OLD, ANCIENT, BASELINE, CURRENT):
            self.assertIn(version, out["versions"], version)

    async def test_show_all_versions_adds_the_rest_and_keeps_what_the_default_list_had(self):
        make_venv(self.cfg, OLD)
        default = await self.status()
        every = await self.status(all_versions=True)
        self.assertNotIn("all_versions", default)
        self.assertEqual(every["all_versions"], _sorted(STABLE + [CURRENT]))
        for version in default["versions"]:
            self.assertIn(version, every["all_versions"], version)
        self.assertEqual(every["versions"], default["versions"])  # asking for more does not change the default list
        self.assertEqual(every["versions_total"], len(every["all_versions"]))

    async def test_the_answer_says_how_many_more_there_are(self):
        out = await self.status()
        self.assertEqual(out["versions_total"], len(_sorted(STABLE + [CURRENT])))
        self.assertGreater(out["versions_total"], len(out["versions"]))

    async def test_a_downgrade_plan_still_covers_the_offered_list(self):
        make_venv(self.cfg, OLD)
        self._ha_json(previous=OLD)
        backup = {"name": "b.zip", "created": "2026-01-01T00:00:00", "label": "", "ha_version": OLD}
        with mock.patch.object(preflight, "_ha_wheel_check", side_effect=AssertionError("pip ran")), \
             mock.patch("backupkit.list_backups", return_value=[backup]):
            out = await self.status()
        self.assertEqual(out["config_backups"].get(OLD, {}).get("name"), "b.zip")

    async def test_the_fields_the_page_and_the_builder_already_read_still_carry_what_they_did(self):
        make_venv(self.cfg, OLD)
        # a scheduled change is pending only while it differs from the version running, and which version
        # that is depends on the image these tests run in
        desired = BASELINE if BASELINE != CURRENT else NEWER[-1]
        self._ha_json(desired=desired, previous=OLD)
        out = await self.status()
        self.assertEqual(out["recent"], STABLE[-RECENT_N:])  # still only the newest stable releases
        self.assertEqual(out["installed_venvs"], [OLD])
        self.assertEqual(out["latest_stable"], NEWER[-1])
        self.assertEqual(out["current"], CURRENT)
        self.assertEqual(out["desired"], desired)
        self.assertEqual(out["previous"], OLD)
        self.assertTrue(out["pending"])

    async def test_recent_is_the_newest_n_of_what_pypi_offers(self):
        info = {"info": {"requires_python": ">=3.13"},
                "releases": {v: [{"requires_python": ">=3.13", "yanked": False, "upload_time": "2026-09-01T00:00:00"}] for v in STABLE}}
        session = SimpleNamespace(get=lambda url, timeout=None: _Response(info))
        with mock.patch("custom_components.integration_manager.ha_updater.async_get_clientsession", return_value=session):
            out = await self.up.available(force=True)
        self.assertEqual(out["recent"], STABLE[-RECENT_N:])
        self.assertEqual(len(out["recent"]), RECENT_N)
        self.assertEqual(self.up._stable, STABLE)  # the whole list is kept for "show all versions"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def read(self):
        return json.dumps(self.payload).encode()


class VerdictTest(_Updater, unittest.IsolatedAsyncioTestCase):

    async def test_a_cached_report_is_handed_over_and_nothing_is_resolved(self):
        version = NEWER[-1]
        report = {"version": version, "ok": True, "checked": True, "blockers": [], "warnings": [],
                  "notes": ["all 48 pinned requirements have a wheel"], "missing": []}
        preflight.ha_remember(version, report)
        with mock.patch.object(preflight, "_ha_wheel_check", side_effect=AssertionError("pip ran")):
            out = await self.status()
        self.assertEqual(out["verdicts"][version], report)

    async def test_a_version_nobody_checked_gets_no_verdict(self):
        with mock.patch.object(preflight, "_ha_wheel_check", side_effect=AssertionError("pip ran")):
            out = await self.status()
        self.assertEqual(out["verdicts"], {})

    async def test_a_release_this_python_is_too_new_for_is_refused_without_a_check(self):
        self.up._releases[NEWER[-2]] = "<3.13"
        with mock.patch.object(preflight, "_ha_wheel_check", side_effect=AssertionError("pip ran")):
            out = await self.status()
        self.assertFalse(out["verdicts"][NEWER[-2]]["ok"])
        self.assertIn("needs Python <3.13", out["verdicts"][NEWER[-2]]["blockers"][0])

    async def test_the_mark_and_the_refusal_are_the_same_sentence(self):
        self.up._releases[NEWER[-2]] = "<3.13"
        out = await self.status()
        with self.assertRaises(ValueError) as ctx:
            await self.up.validate(NEWER[-2])
        self.assertEqual(out["verdicts"][NEWER[-2]]["blockers"], [str(ctx.exception)])

    async def test_the_baseline_is_named_once_instead_of_on_every_old_version(self):
        out = await self.status(all_versions=True)
        self.assertEqual(out["baseline"], BASELINE)
        self.assertNotIn(ANCIENT, out["verdicts"])  # the page does that arithmetic itself
        self.assertNotIn(OLD, out["verdicts"])
        self.assertIn(BASELINE, out["all_versions"])


class StatusViewTest(_Updater, unittest.IsolatedAsyncioTestCase):

    def _view(self):
        view = views.HaStatusView(self.up)
        view.json = lambda d, *a, **k: d
        return view

    async def test_all_reaches_the_updater_and_needs_no_fetch_header(self):
        with mock.patch.object(self.up, "status", mock.AsyncMock(return_value={})) as status:
            await self._view().get(_Request({"all": "1"}, fetch=False))
        self.assertEqual(status.await_args.kwargs, {"force": False, "all_versions": True})

    async def test_refresh_still_needs_the_header_and_the_two_combine(self):
        view = self._view()
        with mock.patch.object(self.up, "status", mock.AsyncMock(return_value={})) as status:
            await view.get(_Request({"refresh": "1", "all": "1"}, fetch=False))
            self.assertEqual(status.await_args.kwargs, {"force": False, "all_versions": True})
            await view.get(_Request({"refresh": "1", "all": "1"}))
            self.assertEqual(status.await_args.kwargs, {"force": True, "all_versions": True})

    async def test_a_plain_get_is_the_default_list(self):
        with mock.patch.object(self.up, "status", mock.AsyncMock(return_value={})) as status:
            await self._view().get(_Request())
        self.assertEqual(status.await_args.kwargs, {"force": False, "all_versions": False})


if __name__ == "__main__":
    unittest.main()


class FloorAndDefaultTest(_Updater, unittest.IsolatedAsyncioTestCase):
    """HA_VERSION_MIN (the oldest version this image installs) is not HA_VERSION_DEFAULT (what a fresh
    volume installs).  They used to be one variable, so lowering the floor also changed what a new box
    started with - and an image built to reach an older release installed that older release too."""

    async def test_the_floor_is_ha_version_min_when_it_is_set(self):
        with mock.patch.dict(os.environ, {"HA_VERSION_MIN": OLD, "HA_VERSION_DEFAULT": BASELINE}):
            self.assertEqual(self.up._image_refusal(BASELINE), "")
            between = "2026.1.0"  # older than what a fresh volume installs, newer than the floor
            self.assertEqual(self.up._image_refusal(between), "")
            self.assertIn("older than this image's floor", self.up._image_refusal(ANCIENT))
            out = await self.status()
            self.assertEqual((out["baseline"], out["default_version"]), (OLD, BASELINE))

    async def test_an_image_without_the_new_variable_keeps_the_old_meaning(self):
        with mock.patch.dict(os.environ, {"HA_VERSION_DEFAULT": BASELINE}, clear=False):
            os.environ.pop("HA_VERSION_MIN", None)
            self.assertIn("older than this image's floor", self.up._image_refusal(OLD))
            self.assertEqual((await self.status())["baseline"], BASELINE)
