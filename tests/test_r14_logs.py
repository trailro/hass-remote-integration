"""The Log files format after the fourteenth review.

N4: a pattern with two or more global flags after the start
(``(?P<x>a)(?p)b(?b)``) compiles, but the weighing parsed it again only once:
the second flag's exception reached aiohttp, a 500 on saving the format and
on every Log files tail with it stored.  The package itself parses again until
no new global flag turns up.

Every test fails on the tree before the fix unless its docstring says it pins
behaviour that already held.
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from aiohttp.test_utils import make_mocked_request

from custom_components.integration_manager import logfiles_page, manage_views
from tests.fakes import FakeInstaller


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


# ----- N4 -----------------------------------------------------------------------------------------

GLOBAL_FLAGS_LATER = (
    "(?P<x>a)(?p)b(?b)",  # the reviewer's
    "(?P<x>a)(?p)b(?b)c(?e)",
    "(?P<x>a)(?p)b(?b)c(?e)d(?r)",
    "(?P<x>a)(?p)b(?b)c(?e)d(?r)e(?V1)",
    "(?P<x>\\w+)(?b)(?p)(?e)(?x) (?P<y> .* )",
)


@unittest.skipIf(logfiles_page._regex is None, "the regex package is not installed here")
class GlobalFlagsLaterTest(unittest.TestCase):

    def setUp(self):
        logfiles_page._compiled.cache_clear()
        self.addCleanup(logfiles_page._compiled.cache_clear)

    def test_a_pattern_the_package_compiles_is_taken(self):
        for pattern in GLOBAL_FLAGS_LATER:
            with self.subTest(pattern=pattern):
                logfiles_page._regex.compile(pattern)  # the premise: the package takes it
                fmt, error = logfiles_page.clean_log_format({"pattern": pattern})
                self.assertIsNone(error)
                self.assertEqual(fmt, {"pattern": pattern})

    def test_the_weight_still_counts_after_every_flag(self):
        for pattern in ("(?P<x>a)(?p)b(?b)c(?e)(?P<y>a{200}){200}",
                        "(?P<x>a)(?p)b(?b)c(?e)d(?r)(?P<y>(?:a{120}){120})",
                        "(?P<x>a)(?b)(?p)(?e)(?x)(?P<y>a{1 0 0 0 1})"):  # verbose set last: the count as the compile reads it
            with self.subTest(pattern=pattern):
                fmt, error = logfiles_page.clean_log_format({"pattern": pattern})
                self.assertEqual(fmt, {})
                self.assertIn("repeats too much", error or "")

    def test_saving_answers_instead_of_500(self):
        for pattern in GLOBAL_FLAGS_LATER[:3]:
            with self.subTest(pattern=pattern):
                st = SimpleNamespace(data={}, async_save=mock.AsyncMock(), public=lambda: {}, github_headers=lambda: {})
                view = manage_views.SettingsView(SimpleNamespace(settings=st, _releases_cache={}, scheduler=None))
                request = SimpleNamespace(headers={}, query={}, content_type="application/json",
                                          json=mock.AsyncMock(return_value={"log_format": {"pattern": pattern}}))
                res = json.loads(asyncio.run(view.post(request)).body)
                self.assertTrue(res["ok"], res)
                self.assertEqual(st.data["log_format"], {"pattern": pattern})

    def test_a_tail_with_the_format_stored_answers_instead_of_500(self):
        cfg = _tmp(self)
        with open(os.path.join(cfg, "probe.log"), "w", encoding="utf-8") as fh:
            fh.write("hello world\n")
        installer = FakeInstaller()
        installer.settings = SimpleNamespace(data={"log_format": {"pattern": GLOBAL_FLAGS_LATER[-1]}})
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=_job)
        request = make_mocked_request("GET", "/api/log_files/tail?" + urlencode({"lines": 5}),
                                      headers={"Host": "10.0.0.2:8222", "X-Requested-With": "fetch"})
        resp = asyncio.run(logfiles_page.LogFileTailView(hass, installer).get(request))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertIsNone(body["format_error"])
        self.assertEqual(body["lines"][0]["cells"], ["hello", "world"])

    def test_a_parser_that_misbehaves_refuses_the_pattern(self):
        """Fail closed: whatever the weighing raises, the answer is a refusal and the pattern is never compiled."""
        core = logfiles_page._regex._regex_core

        def endless_flags(source, info):
            info.global_flags |= 1 << (endless_flags.calls % 60)
            endless_flags.calls += 1
            raise core._UnscopedFlagSet(info.global_flags)
        endless_flags.calls = 0

        for name, parse in (("global flags without end", endless_flags),
                            ("an unforeseen exception", mock.Mock(side_effect=KeyError("probe"))),
                            ("a changed node", mock.Mock(side_effect=IndexError("probe")))):
            with self.subTest(name=name):
                logfiles_page._compiled.cache_clear()
                with mock.patch.object(core, "_parse_pattern", parse), \
                        mock.patch.object(logfiles_page._regex, "compile") as compile_:
                    fmt, error = logfiles_page.clean_log_format({"pattern": "(?P<x>a)(?p)b(?b)"})
                self.assertEqual(fmt, {})
                self.assertIn("cannot be checked", error or "")
                compile_.assert_not_called()
        self.assertLessEqual(endless_flags.calls, 64)  # bounded

    def test_an_invalid_pattern_says_it_does_not_compile(self):
        """The first pins behaviour that already held; the second, invalid after two global flags, raised."""
        for pattern in ("(?P<x>a", "(?P<x>a)(?p)b(?b)c("):
            with self.subTest(pattern=pattern):
                fmt, error = logfiles_page.clean_log_format({"pattern": pattern})
                self.assertEqual(fmt, {})
                self.assertIn("does not compile", error or "")


if __name__ == "__main__":
    unittest.main()
