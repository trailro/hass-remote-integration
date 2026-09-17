"""The masking after the thirteenth review.

F3: a quoted value ended at the first quote character, escaped or not
(``"[^"]*"``), so ``{"password":"prefix\\"SUFFIX"}`` came out as
``{"password":"***"SUFFIX"}``, and a secret starting with a quote came out
almost whole.  A value whose quote never closed (a line the logger cut) was not
masked at all, and a name and value inside a JSON string (``\\"password\\":
\\"x\\"``) were not recognised.  The same text reached the diagnostics zip,
the Logs page and the Log files page, and both searches decided on it.

Every answer a search gives is compared whole between a right and a wrong
guess.  Every test fails on the tree before the fix unless its docstring says
it pins behaviour that already held.

C2: the module docstring and the zip's README.txt said settings.json was never
included, while the zip carries its sanitized public view.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from aiohttp.test_utils import make_mocked_request

# the modules, not the names: the same file then loads against a tree without the fix
from custom_components.integration_manager import diagnostics, logfiles_page
from tests.fakes import FakeInstaller
from tests.test_log_follow import _Log

SUFFIX = "SNTL-7f3a9c41"  # synthetic
RIGHT, WRONG = SUFFIX[:7], "SNTL-8f"

# (line, what the line reads once masked): every value holds SUFFIX behind an escaped quote or an unclosed one
CASES = (
    ('{"password":"prefix\\"' + SUFFIX + '"}', '{"password":"***"}'),
    ('{"password": "\\"' + SUFFIX + '"}', '{"password": "***"}'),  # a secret that starts with a quote
    ("{'password': 'pre\\'" + SUFFIX + "', 'port': 1883}", "{'password': '***', 'port': 1883}"),
    ("{'token': 'a\\\\', 'note': 'visible'}", "{'token': '***', 'note': 'visible'}"),  # an escaped backslash ends it
    ('authorization: "Digest u=\\"' + SUFFIX + '\\""', 'authorization: "***"'),
    ("Authorization='x\\'" + SUFFIX + "' kept", "Authorization='***' kept"),
    ('Cookie: "sid=a\\"' + SUFFIX + '"; tail', 'Cookie: "***"; tail'),
    ('received {"user": "me", "api_key": "k\\"' + SUFFIX + '", "n": 1}',
     'received {"user": "me", "api_key": "***", "n": 1}'),
    # JSON inside a JSON string: the name and the value have their quotes escaped
    ('payload {"body": "{\\"password\\": \\"' + SUFFIX + '\\"}", "n": 1}',
     'payload {"body": "{\\"password\\": \\"***\\"}", "n": 1}'),
    ('payload {"body": "{\\"token\\": \\"p\\\\\\"' + SUFFIX + '\\"}", "n": 1}',
     'payload {"body": "{\\"token\\": \\"***\\"}", "n": 1}'),
    # a line the logger cut inside the value: masked to its end
    ('password="cut ' + SUFFIX, 'password="***"'),
    ("secret: 'cut " + SUFFIX, "secret: '***'"),
    ('set-cookie: "sid=' + SUFFIX, 'set-cookie: "***"'),
    ('{\\"pin_code\\": \\"' + SUFFIX, '{\\"pin_code\\": \\"***\\"'),
)


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


class EscapedQuoteTest(unittest.TestCase):

    def test_every_case_masks_the_whole_value(self):
        for line, masked in CASES:
            with self.subTest(line=line):
                self.assertEqual(diagnostics.scrub_text(line), masked)
                self.assertEqual(diagnostics.scrub(line), masked)
                self.assertEqual(diagnostics.scrub_lines([line]), [masked])

    def test_the_masked_line_does_not_depend_on_the_value(self):
        for line, _ in CASES:
            for other in ("zz", "SNTL-0000000000000000", "a b c"):
                with self.subTest(line=line, other=other):
                    self.assertEqual(diagnostics.scrub_text(line), diagnostics.scrub_text(line.replace(SUFFIX, other)))

    def test_what_is_left_readable(self):
        """Pins behaviour that already held."""
        for line, masked in (('{"password": "plain", "host": "h"}', '{"password": "***", "host": "h"}'),
                             ("token=abc next=1", "token=*** next=1"),
                             ("Authorization: Bearer abcdefghij", "Authorization: ***"),
                             ("Cookie: a=b; c=d", "Cookie: ***")):
            with self.subTest(line=line):
                self.assertEqual(diagnostics.scrub_text(line), masked)

    def test_log_records_text(self):
        records = [{"ts": "t", "level": "INFO", "logger": "probe", "message": line} for line, _ in CASES]
        text = diagnostics.log_records_text(records)
        self.assertNotIn(SUFFIX, text)
        self.assertEqual(text.split("\n"), [f"t INFO [probe] {masked}" for _, masked in CASES])


class LinearTimeTest(unittest.TestCase):
    """The rules run over every line a search reads: 256 kB of backslashes and quotes, closed or not, stay fast."""

    N = 256 * 1024
    BUDGET_S = 1.5

    def test_adversarial_values(self):
        n = self.N
        texts = [
            'password="' + "\\" * n, 'password="' + '\\"' * (n // 2), "password='" + "\\'" * (n // 2),
            'password=\\"' + '\\\\\\"x' * (n // 4), "password: " + "\\" * n + '"', 'token=\\' + '\\"' * (n // 2),
            'x"' * (n // 2), 'password="' * (n // 10), '\\"token\\": ' * (n // 12), "cookie: " + '\\"' * (n // 2),
            'authorization:\\\\\\"' + "\\" * n, "a" + "\\" * n, "a\\" * (n // 2), "key='" * (n // 5),
            'password="' + "\\" * (n // 2) + '"' + "\\" * (n // 2), "api_key: " + "\\" * (n // 2) + "'" + "a\\" * (n // 4),
        ]
        for text in texts:
            with self.subTest(text=text[:30], size=len(text)):
                start = time.perf_counter()
                diagnostics.scrub_text(text)
                diagnostics.scrub(text)
                self.assertLess(time.perf_counter() - start, self.BUDGET_S)


class DiagnosticsZipTest(unittest.TestCase):

    def setUp(self):
        self.cfg = _tmp(self)
        with open(os.path.join(self.cfg, "probe.log"), "w", encoding="utf-8") as fh:
            fh.write("".join(f"2026-09-17 10:00:00 INFO [probe] {line}\n" for line, _ in CASES))
        self.log = _Log(self)
        for line, _ in CASES:
            self.log.write(line)

    def _zip(self):
        installer = FakeInstaller()
        installer.status = mock.AsyncMock(return_value={})
        installer.settings = SimpleNamespace(data={}, public=lambda: {"theme": "dark"})
        publisher = SimpleNamespace(status=dict, public_config=dict, build_health=dict)
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job,
                               config_entries=SimpleNamespace(async_entries=list))
        view = diagnostics.DiagnosticsView(hass, installer, publisher, SimpleNamespace(status=mock.AsyncMock(return_value={})))
        request = make_mocked_request("GET", "/api/diagnostics", headers={"Host": "10.0.0.2:8196", "X-Requested-With": "fetch"})
        with mock.patch.object(diagnostics.logbuffer, "find", return_value=self.log.handler), \
                mock.patch.object(diagnostics, "memory_snapshot", mock.AsyncMock(return_value={})), \
                mock.patch.object(diagnostics.notifications, "_rows", return_value=[]), \
                mock.patch.object(diagnostics.events, "EVENTS", None), \
                mock.patch.object(diagnostics.DiagnosticsView, "_packages", staticmethod(lambda: "")):
            resp = asyncio.run(view.get(request))
        self.assertEqual(resp.status, 200)
        with zipfile.ZipFile(io.BytesIO(resp.body)) as zf:
            return {name: zf.read(name).decode() for name in zf.namelist()}

    def test_the_zip_carries_no_suffix(self):
        files = self._zip()
        self.assertIn("log.txt", files)
        self.assertIn("probe.log", files["log_file.txt"])
        for name, text in files.items():
            with self.subTest(name=name):
                self.assertNotIn(SUFFIX, text)
        for _, masked in CASES:
            self.assertIn(masked, files["log.txt"])
            self.assertIn(masked, files["log_file.txt"])

    def test_c2_the_readme_says_what_is_left_out_and_what_is_in(self):
        files = self._zip()
        self.assertIn("settings.json", files)
        for text in (files["README.txt"], diagnostics.__doc__):
            with self.subTest(text=text):
                self.assertIn("raw credential-bearing files", text)
                self.assertIn("sanitized public views", text)
                self.assertNotIn("never included", text)
                self.assertNotIn("not included", text)


class LogsSearchTest(unittest.TestCase):
    ROUTINE = 40

    def _answers(self, params):
        log = _Log(self)
        for i in range(self.ROUTINE):
            log.write(f"routine poll {i}")
        for line, _ in CASES:
            log.write(line)
            log.write("routine after", logger="other.lib", level=logging.DEBUG)
        return [log.api(**p) for p in params]

    def test_right_and_wrong_guesses_answer_the_same(self):
        for base in ({"level": "DEBUG", "limit": 200}, {"level": "INFO", "limit": 3}, {"level": "DEBUG", "limit": 1, "since_id": 20},
                     {"level": "INFO", "limit": 200, "prefix": "custom_components.probe"}):
            for right, wrong in ((RIGHT, WRONG), (SUFFIX, "SNTL-7f3a9c42"), ('"' + RIGHT, '"' + WRONG),
                                 ("\\" + '"' + RIGHT, "\\" + '"' + WRONG), ("prefix\\", "prefiy\\")):
                with self.subTest(right=right, **base):
                    a, b = self._answers([dict(base, q=right), dict(base, q=wrong)])
                    self.assertEqual(a, b)
                    self.assertEqual(a["records"], [])

    def test_the_rows_shown_are_masked(self):
        answer, = self._answers([{"level": "DEBUG", "limit": 200}])
        messages = [r["message"] for r in answer["records"]]
        self.assertNotIn(SUFFIX, json.dumps(answer))
        for _, masked in CASES:
            self.assertIn(masked, messages)


class LogFilesSearchTest(unittest.TestCase):

    def setUp(self):
        self.cfg = _tmp(self)
        with open(os.path.join(self.cfg, "probe.log"), "w", encoding="utf-8") as fh:
            for i in range(300):
                fh.write(f"2026-09-17 10:00:01 INFO [probe] routine poll {i}\n")
                if i % 20 == 0:
                    for line, _ in CASES:
                        fh.write(f"2026-09-17 10:00:02 INFO [probe] {line}\n")
        self.installer = FakeInstaller()
        self.installer.settings = SimpleNamespace(data={})
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job)

    def _tail(self, **params):
        request = make_mocked_request("GET", "/api/log_files/tail?" + urlencode(params),
                                      headers={"Host": "10.0.0.2:8222", "X-Requested-With": "fetch"})
        resp = asyncio.run(logfiles_page.LogFileTailView(self.hass, self.installer).get(request))
        return resp.status, resp.body

    def test_right_and_wrong_guesses_answer_byte_for_byte_the_same(self):
        for lines in (1, 10, 50, 5000):
            for right, wrong in ((RIGHT, WRONG), (SUFFIX, "SNTL-7f3a9c42"), ('\\"' + RIGHT, '\\"' + WRONG),
                                 ("'" + RIGHT, "'" + WRONG), ("cut " + RIGHT, "cut " + WRONG)):
                with self.subTest(lines=lines, right=right):
                    a, b = self._tail(lines=lines, q=right), self._tail(lines=lines, q=wrong)
                    self.assertEqual(a[0], 200)
                    self.assertEqual(json.loads(a[1])["lines"], [])
                    self.assertEqual(a, b)

    def test_the_lines_shown_are_masked(self):
        status, body = self._tail(lines=5000)
        self.assertEqual(status, 200)
        self.assertNotIn(SUFFIX, body.decode())
        rows = [row["raw"] for row in json.loads(body)["lines"]]
        for _, masked in CASES:
            self.assertIn(f"2026-09-17 10:00:02 INFO [probe] {masked}", rows)

    def test_every_masked_line_passes_the_prefilter(self):
        for line, _ in CASES:
            with self.subTest(line=line):
                self.assertTrue(logfiles_page._rules_may_change(line))


if __name__ == "__main__":
    unittest.main()
