"""Where pip's backtracking lands.  A requirement without a lower bound whose newest releases cannot be
installed here does not fail: pip walks back until something resolves, and it can walk back years.  The
rule for "far behind", what is deliberately never compared, the PyPI lookup behind it (which says nothing
when it cannot read the index) and the warning's way into the report."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import preflight
from custom_components.integration_manager.installer import Installer, State

# python-miio as PyPI really lists it (version: release day, requires_python), the case this came from:
# three integrations ask for >=0.5.12, whose netifaces has no wheel for CPython 3.14, and two ask for
# "python-miio" with no bound at all - pip backtracks past netifaces and settles on 0.2.0, from 2017.
MIIO = {
    "0.2.0": ("2017-10-02", ""), "0.3.0": ("2017-10-21", ""), "0.3.1": ("2017-11-02", ""),
    "0.3.2": ("2017-11-20", ""), "0.3.3": ("2017-12-18", ""), "0.3.4": ("2018-01-20", ""),
    "0.3.5": ("2018-02-04", ""), "0.3.6": ("2018-02-13", ""), "0.3.7": ("2018-02-18", ""),
    "0.3.8": ("2018-03-10", ""), "0.3.9": ("2018-03-27", ""), "0.4.0": ("2018-06-03", ""),
    "0.4.1": ("2018-08-21", ""), "0.4.2": ("2018-10-07", ""), "0.4.3": ("2018-11-01", ""),
    "0.4.4": ("2018-12-05", ""), "0.4.5": ("2019-03-19", ""), "0.4.6": ("2019-10-02", ""),
    "0.4.7": ("2019-10-27", ""), "0.4.8": ("2019-12-12", ""), "0.5.0": ("2020-03-29", ""),
    "0.5.0.1": ("2020-03-29", ""), "0.5.1": ("2020-06-04", ">=3.6.5,<4.0.0"),
    "0.5.2": ("2020-07-03", ">=3.6.5,<4.0.0"), "0.5.2.1": ("2020-07-03", ">=3.6.5,<4.0.0"),
    "0.5.3": ("2020-07-27", ">=3.6.5,<4.0.0"), "0.5.4": ("2020-11-15", ">=3.6.5,<4.0.0"),
    "0.5.5": ("2021-03-13", ">=3.6.5,<4.0.0"), "0.5.5.1": ("2021-03-20", ">=3.6.5,<4.0.0"),
    "0.5.5.2": ("2021-03-24", ">=3.6.5,<4.0.0"), "0.5.6": ("2021-05-05", ">=3.6.5,<4.0.0"),
    "0.5.7": ("2021-08-13", ">=3.6.5,<4.0.0"), "0.5.8": ("2021-09-01", ">=3.6.5,<4.0.0"),
    "0.5.9": ("2021-11-30", ">=3.6.5,<4.0.0"), "0.5.9.1": ("2021-12-02", ">=3.6.5,<4.0.0"),
    "0.5.9.2": ("2021-12-14", ">=3.6.5,<4.0.0"), "0.5.10": ("2022-02-17", ">=3.7,<4.0"),
    "0.5.11": ("2022-03-07", ">=3.7,<4.0"), "0.5.12": ("2022-07-18", ">=3.7,<4.0"),
    "0.6.0.dev0": ("2024-03-13", ">=3.8,<4.0"),
}
PY314 = "3.14.0"


def _hass(record=None):
    async def job(fn, *args):
        if record is not None:
            record.append(fn)
        return fn(*args)
    return SimpleNamespace(async_add_executor_job=job, is_running=True)


class RuleTest(unittest.TestCase):
    """The rule itself, on the resolution this was written for."""

    def test_the_python_miio_backtrack_is_reported(self):
        hit = preflight._resolution_lag("python-miio", "0.2.0", "python-miio", MIIO, PY314)
        self.assertIsNotNone(hit)
        self.assertIn("pip resolved python-miio 0.2.0, released 2017-10-02", hit)
        self.assertIn("0.5.12 (2022-07-18)", hit)
        self.assertIn("4.8 years behind", hit)
        self.assertIn("38 releases", hit)

    def test_it_says_what_forced_it_down_and_that_nothing_blocks(self):
        hit = preflight._resolution_lag("python-miio", "0.2.0", "python-miio", MIIO, PY314)
        self.assertIn("something else in the resolved set forced it down", hit)
        self.assertIn("no wheel for this Python", hit)
        self.assertIn("installs and resolves cleanly", hit)

    def test_the_newest_it_names_is_not_a_prerelease(self):
        """0.6.0.dev0 is on PyPI and newer than everything; pip does not take it, so it is not the yardstick."""
        hit = preflight._resolution_lag("python-miio", "0.2.0", "python-miio", MIIO, PY314)
        self.assertNotIn("0.6.0.dev0", hit)

    def test_a_prerelease_resolution_is_measured_against_prereleases(self):
        older = {**MIIO, "0.2.0.dev1": ("2017-09-01", "")}
        hit = preflight._resolution_lag("python-miio", "0.2.0.dev1", "python-miio>=0.2.0.dev0", older, PY314)
        self.assertIn("0.6.0.dev0", hit)

    def test_a_requirement_that_asks_for_the_newest_says_nothing(self):
        self.assertIsNone(preflight._resolution_lag("python-miio", "0.5.12", "python-miio>=0.5.12", MIIO, PY314))

    def test_a_requirement_the_integration_caps_is_measured_against_its_own_cap(self):
        """"python-miio<0.3" resolving to 0.2.0 is exactly what was asked for, not a backtrack."""
        self.assertIsNone(preflight._resolution_lag("python-miio", "0.2.0", "python-miio<0.3", MIIO, PY314))

    def test_a_pinned_requirement_says_nothing(self):
        self.assertIsNone(preflight._resolution_lag("python-miio", "0.2.0", "python-miio==0.2.0", MIIO, PY314))

    def test_a_patch_level_lag_says_nothing(self):
        """1.4.2 where 1.4.7 exists, eight years apart: the same series is not "far behind"."""
        index = {"1.4.2": ("2016-01-01", ""), "1.4.5": ("2020-01-01", ""), "1.4.7": ("2024-01-01", "")}
        self.assertIsNone(preflight._resolution_lag("demo", "1.4.2", "demo", index, PY314))

    def test_a_major_published_last_week_says_nothing(self):
        """A series gap with no age gap is not backtracking: 2.0 is simply new."""
        index = {"1.9.0": ("2026-06-01", ""), "2.0.0": ("2026-09-10", "")}
        self.assertIsNone(preflight._resolution_lag("demo", "1.9.0", "demo", index, PY314))

    def test_an_old_release_is_not_enough_on_its_own(self):
        """The resolution is eight years old, but it is still the newest series there is."""
        index = {"1.0.0": ("2018-01-01", ""), "1.0.1": ("2018-02-01", "")}
        self.assertIsNone(preflight._resolution_lag("demo", "1.0.0", "demo", index, PY314))

    def test_a_calendar_version_one_year_on_says_nothing(self):
        """A new "major" every January is a versioning scheme, not a stale resolution."""
        index = {"2025.1.0": ("2025-01-10", ""), "2026.1.0": ("2026-01-12", "")}
        self.assertIsNone(preflight._resolution_lag("demo", "2025.1.0", "demo", index, PY314))

    def test_a_calendar_version_four_years_behind_is_reported(self):
        index = {"2021.1.0": ("2021-01-10", ""), "2026.1.0": ("2026-01-12", "")}
        self.assertIn("5.0 years behind", preflight._resolution_lag("demo", "2021.1.0", "demo", index, PY314))

    def test_releases_this_python_is_excluded_from_are_not_the_yardstick(self):
        """When the newer releases dropped this Python, taking the old one is pip's right answer."""
        index = {"1.0.0": ("2017-01-01", ">=3.6"), "3.0.0": ("2026-01-01", ">=3.8,<3.13")}
        self.assertIsNone(preflight._resolution_lag("demo", "1.0.0", "demo", index, PY314))
        index["3.0.0"] = ("2026-01-01", ">=3.8")
        self.assertIsNotNone(preflight._resolution_lag("demo", "1.0.0", "demo", index, PY314))

    def test_a_version_pypi_does_not_list_says_nothing(self):
        """A requirement given as an archive URL resolves to something PyPI has no date for."""
        self.assertIsNone(preflight._resolution_lag("demo", "1.0.0+local", "demo", {"3.0.0": ("2026-01-01", "")}, PY314))

    def test_an_unreadable_version_or_requirement_says_nothing(self):
        self.assertIsNone(preflight._resolution_lag("demo", "not-a-version", "demo", MIIO, PY314))
        self.assertIsNone(preflight._resolution_lag("python-miio", "0.2.0", "python-miio >>> 1", MIIO, PY314))

    def test_a_package_pip_only_pulled_in_is_measured_against_the_whole_index(self):
        hit = preflight._resolution_lag("python-miio", "0.2.0", None, MIIO, PY314)
        self.assertIn(f"this Python ({PY314})", hit)


class IndexParsingTest(unittest.TestCase):
    def test_the_day_and_the_requires_python_come_out(self):
        raw = json.dumps({"releases": {"1.0": [{"upload_time_iso_8601": "2020-05-04T10:00:00Z",
                                                "requires_python": ">=3.6"}]}}).encode()
        self.assertEqual(preflight._pypi_releases(raw), {"1.0": ("2020-05-04", ">=3.6")})

    def test_a_fully_yanked_release_is_not_a_version_pip_can_take(self):
        raw = json.dumps({"releases": {
            "1.0": [{"upload_time_iso_8601": "2020-05-04T10:00:00Z", "yanked": True}],
            "1.1": [{"upload_time_iso_8601": "2020-06-04T10:00:00Z"}],
            "1.2": [],
        }}).encode()
        self.assertEqual(sorted(preflight._pypi_releases(raw)), ["1.1"])

    def test_the_earliest_file_of_a_release_dates_it(self):
        raw = json.dumps({"releases": {"1.0": [{"upload_time_iso_8601": "2020-05-04T12:00:00Z"},
                                               {"upload_time_iso_8601": "2020-05-04T10:00:00Z"}]}}).encode()
        self.assertEqual(preflight._pypi_releases(raw)["1.0"][0], "2020-05-04")


async def _agen(chunks):
    for c in chunks:
        yield c


class _Resp:
    def __init__(self, blob, status=200):
        self.status, self.content_length = status, len(blob)
        self.content = SimpleNamespace(iter_chunked=lambda n: _agen([blob]))


class _Session:
    def __init__(self, resp):
        self.resp, self.urls, self.timeouts = resp, [], []

    def get(self, url, **kw):
        self.urls.append(url)
        self.timeouts.append(kw.get("timeout"))
        resp = self.resp() if callable(self.resp) else self.resp

        class _Ctx:
            async def __aenter__(self):
                if isinstance(resp, Exception):
                    raise resp
                return resp

            async def __aexit__(self, *a):
                return False
        return _Ctx()


class PypiLookupTest(unittest.TestCase):
    def _index(self, resp):
        session = _Session(resp)
        with mock.patch.object(preflight, "async_get_clientsession", return_value=session):
            return asyncio.run(preflight._pypi_index(_hass(), "python-miio")), session

    def test_it_reads_the_index(self):
        raw = json.dumps({"releases": {"0.2.0": [{"upload_time_iso_8601": "2017-10-02T11:00:00Z"}]}}).encode()
        index, session = self._index(_Resp(raw))
        self.assertEqual(index, {"0.2.0": ("2017-10-02", "")})
        self.assertEqual(session.urls, ["https://pypi.org/pypi/python-miio/json"])

    def test_it_uses_a_timeout(self):
        raw = json.dumps({"releases": {}}).encode()
        _, session = self._index(_Resp(raw))
        self.assertEqual(session.timeouts[0].total, preflight.PYPI_TIMEOUT_S)

    def test_pypi_that_answers_anything_else_says_nothing(self):
        self.assertIsNone(self._index(_Resp(b"{}", status=404))[0])

    def test_pypi_that_cannot_be_reached_says_nothing(self):
        self.assertIsNone(self._index(OSError("no route to host"))[0])

    def test_an_index_that_is_not_json_says_nothing(self):
        self.assertIsNone(self._index(_Resp(b"<html>nope</html>"))[0])

    def test_an_index_too_big_to_read_says_nothing(self):
        """aiohttp's own index is 9 MB; it is not worth reading and is never guessed at."""
        blob = b"x" * (preflight.PYPI_MAX_BYTES + 1)
        self.assertIsNone(self._index(_Resp(blob))[0])

    def test_the_index_is_not_parsed_on_the_event_loop(self):
        raw = json.dumps({"releases": {}}).encode()
        seen, session = [], _Session(_Resp(raw))
        with mock.patch.object(preflight, "async_get_clientsession", return_value=session):
            asyncio.run(preflight._pypi_index(_hass(seen), "python-miio"))
        self.assertIn(preflight._pypi_releases, seen)


class WhatIsComparedTest(unittest.TestCase):
    """Only what the integration declares, and what that pulls in."""

    def setUp(self):
        self.looked = []

        async def index(hass, name):
            self.looked.append(name)
            return MIIO if name.lower().startswith("python-miio") else {}

        self.patch = mock.patch.object(preflight, "_pypi_index", index)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _warn(self, own, rows, dep_reqs=(), constraints=""):
        inst = SimpleNamespace(constraints=constraints)
        return asyncio.run(preflight._resolution_warnings(_hass(), inst, list(own), list(rows), list(dep_reqs)))

    def test_the_requirement_of_the_manifest_is_compared(self):
        out = self._warn(["python-miio"], [{"name": "python-miio", "version": "0.2.0"}])
        self.assertEqual(len(out), 1)
        self.assertIn("python-miio 0.2.0", out[0])

    def test_a_package_home_assistant_pins_is_never_compared(self):
        d = tempfile.mkdtemp(prefix="hri-constraints-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "package_constraints.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# comment\n-r other.txt\nPython_MiIO==0.2.0  # HA's own pin\n")
        self.assertEqual(self._warn(["python-miio"], [{"name": "python-miio", "version": "0.2.0"}], constraints=path), [])
        self.assertEqual(self.looked, [])

    def test_a_requirement_of_a_manifest_dependency_is_not_compared(self):
        out = self._warn(["other-lib"], [{"name": "other-lib", "version": "1.0"},
                                         {"name": "python-miio", "version": "0.2.0"}],
                         dep_reqs=["python-miio>=0.5.12"])
        self.assertEqual(out, [])
        self.assertEqual(self.looked, ["other-lib"])

    def test_a_package_pip_pulled_in_for_the_manifest_is_compared(self):
        out = self._warn(["some-vacuum==1.0"], [{"name": "some-vacuum", "version": "1.0"},
                                                {"name": "python-miio", "version": "0.2.0"}])
        self.assertEqual(len(out), 1)
        self.assertIn("this Python", out[0])  # no requirement of its own to measure against

    def test_a_version_that_declares_nothing_costs_no_round_trip(self):
        self.assertEqual(self._warn([], [{"name": "python-miio", "version": "0.2.0"}]), [])
        self.assertEqual(self.looked, [])

    def test_the_manifests_own_requirements_are_looked_up_first(self):
        rows = [{"name": f"filler-{i}", "version": "1.0"} for i in range(preflight.MAX_PYPI_LOOKUPS + 5)]
        rows.append({"name": "python-miio", "version": "0.2.0"})
        out = self._warn(["python-miio"], rows)
        self.assertEqual(len(self.looked), preflight.MAX_PYPI_LOOKUPS)
        self.assertEqual(self.looked[0], "python-miio")
        self.assertEqual(len(out), 1)

    def test_pypi_that_says_nothing_warns_about_nothing(self):
        with mock.patch.object(preflight, "_pypi_index", mock.AsyncMock(return_value=None)):
            self.assertEqual(self._warn(["python-miio"], [{"name": "python-miio", "version": "0.2.0"}]), [])


def _installer(test, requirements, running="1.0", target="2.0"):
    d = tempfile.mkdtemp(prefix="hri-resolution-")
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


def _lag_warning(report):
    return next((w for w in report["warnings"] if "behind" in w), None)


class ReportTest(unittest.TestCase):
    def _run(self, inst, rows, index=MIIO):
        pip = {"ok": True, "install": list(rows), "stderr": ""}
        with mock.patch.object(preflight, "_pip_dry_run", return_value=pip), \
                mock.patch.object(preflight, "_pypi_index", mock.AsyncMock(return_value=index)):
            return asyncio.run(preflight.run(_hass(), inst, "demo", "2.0",
                                             source_dir=inst._version_dir("demo", "2.0")))

    def test_the_report_warns_without_blocking(self):
        inst = _installer(self, ["python-miio"])
        report = self._run(inst, [{"name": "python-miio", "version": "0.2.0"}])
        self.assertTrue(report["ok"])
        self.assertEqual(report["blockers"], [])
        self.assertIn("pip resolved python-miio 0.2.0", _lag_warning(report))

    def test_a_current_resolution_says_nothing(self):
        inst = _installer(self, ["python-miio>=0.5.12"])
        report = self._run(inst, [{"name": "python-miio", "version": "0.5.12"}])
        self.assertIsNone(_lag_warning(report))

    def test_pypi_unreachable_leaves_the_preflight_whole(self):
        inst = _installer(self, ["python-miio"])
        report = self._run(inst, [{"name": "python-miio", "version": "0.2.0"}], index=None)
        self.assertTrue(report["ok"])
        self.assertIsNone(_lag_warning(report))


if __name__ == "__main__":
    unittest.main()
