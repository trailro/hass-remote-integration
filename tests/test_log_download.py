"""GET /api/log_files/download: a whole log file, masked as the page masks it.

The Log files page shows a masked tail; the Download button next to its
controls saves the same file.  What is checked here is that the download can
only be reached the way the tail can (the header, and a file of the current
listing by its id or its masked name), that the masking is never skipped -
including a key block whose BEGIN marker is in an earlier chunk, or whose END
marker the log never got -, that the real name reaches neither the answer nor
the access log, and that a file longer than the budget is sent from its end
and says so.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.web_log import AccessLogger
from aiohttp.test_utils import make_mocked_request

import logbuffer
from custom_components.integration_manager import logfiles_page
from tests.fakes import FakeInstaller

SECRET = "hunter2synthetic"
KEY_BODY = "MIIFSYNTHETICKEYMATERIALAAAABBBBCCCCDDDD"  # synthetic, no real key
SHORT_BODY = "QUJDREVGSHORT"  # short enough that no rule masks it on its own


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


class _Writer:
    """The payload writer of a mocked request: keeps what the stream wrote."""

    def __init__(self):
        self.chunks = []
        self.length = None
        self.output_size = 0
        self.transport = None

    async def write(self, data, *args, **kw):
        self.chunks.append(bytes(data))

    async def write_eof(self, data=b"", *args, **kw):
        if data:
            self.chunks.append(bytes(data))

    async def drain(self):
        pass

    def enable_chunking(self):
        pass

    def enable_compression(self, *args, **kw):
        pass

    def send_headers(self):
        pass

    async def write_headers(self, status_line, headers):
        self.status_line, self.written_headers = status_line, headers

    @property
    def body(self):
        return b"".join(self.chunks).decode("utf-8", "replace")


class DownloadTest(unittest.TestCase):

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.installer = FakeInstaller(running="demo", spec={"log_dir": "logs"})
        self.installer.settings = SimpleNamespace(data={})
        os.makedirs(os.path.join(self.cfg, "logs"))
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job,
                                    config_entries=SimpleNamespace(async_entries=lambda domain=None: []))

    # ----- helpers ---------------------------------------------------------

    def write(self, name, text):
        path = os.path.join(self.cfg, "logs", name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def listing(self):
        resp = asyncio.run(logfiles_page.LogFilesView(self.hass, self.installer).get(self._req("/api/log_files")))
        return json.loads(resp.body)

    @staticmethod
    def _req(url, header=True):
        headers = {"Host": "10.0.0.2:8196"}
        if header:
            headers["X-Requested-With"] = "fetch"
        return make_mocked_request("GET", url, headers=headers, writer=_Writer())

    def download(self, header=True, **params):
        """(status, headers, body) of one download, as the page asks for it."""
        request = self._req("/api/log_files/download" + ("?" + urlencode(params) if params else ""), header=header)
        view = logfiles_page.LogFileDownloadView(self.hass, self.installer)
        resp = asyncio.run(view.get(request))
        if not resp.prepared:  # a refusal: a plain JSON answer
            return resp.status, dict(resp.headers), resp.body.decode()
        return resp.status, dict(resp.headers), request._payload_writer.body

    def only_id(self):
        files = self.listing()
        self.assertEqual(len(files), 1, files)
        return files[0]

    # ----- masking ---------------------------------------------------------

    def test_a_secret_in_the_file_is_masked_in_the_download(self):
        self.write("app.log", f"start\npassword={SECRET}\nAuthorization: Bearer {KEY_BODY}\nend\n")
        status, _, body = self.download(id=self.only_id()["id"])
        self.assertEqual(status, 200, body)
        self.assertNotIn(SECRET, body)
        self.assertNotIn(KEY_BODY, body)
        self.assertIn("password=***", body)
        self.assertIn("start", body)  # the rest of the file is still there

    def test_a_key_block_the_log_never_closed_is_masked_to_its_end(self):
        """No END marker: nothing above the body says it is a body once the
        window is a chunk, so the BEGIN marker is carried from chunk to chunk."""
        self.write("app.log", f"before\n-----BEGIN PRIVATE KEY-----\n{SHORT_BODY}\n{SHORT_BODY}2\n")
        status, _, body = self.download(id=self.only_id()["id"])
        self.assertEqual(status, 200, body)
        self.assertNotIn(SHORT_BODY, body)
        self.assertIn("before", body)

    def test_a_key_block_whose_begin_marker_was_in_an_earlier_chunk_is_masked(self):
        self.write("app.log", "before\n-----BEGIN PRIVATE KEY-----\n" + f"{KEY_BODY}\n" * 4
                   + f"{SHORT_BODY}\n-----END PRIVATE KEY-----\nafter\n")
        with mock.patch.object(logfiles_page, "DOWNLOAD_CHUNK", 48):  # several chunks inside the block
            status, _, body = self.download(id=self.only_id()["id"])
        self.assertEqual(status, 200, body)
        self.assertNotIn(KEY_BODY, body)
        self.assertNotIn(SHORT_BODY, body)
        self.assertIn("after", body)

    def test_the_file_comes_back_whole_when_it_holds_no_secret(self):
        text = "".join(f"line {n:04d} nothing to hide here\n" for n in range(500))
        self.write("app.log", text)
        with mock.patch.object(logfiles_page, "DOWNLOAD_CHUNK", 512):
            status, _, body = self.download(id=self.only_id()["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body, text)

    def test_a_file_that_does_not_end_with_a_newline_gains_none(self):
        self.write("app.log", "one\ntwo")
        self.assertEqual(self.download(id=self.only_id()["id"])[2], "one\ntwo")

    def test_a_multi_byte_character_across_a_chunk_boundary_survives(self):
        text = "".join(f"ligne {n} — café ✓\n" for n in range(200))
        self.write("app.log", text)
        with mock.patch.object(logfiles_page, "DOWNLOAD_CHUNK", 37):
            self.assertEqual(self.download(id=self.only_id()["id"])[2], text)

    # ----- the gates -------------------------------------------------------

    def test_the_header_is_required(self):
        self.write("app.log", "line\n")
        status, _, body = self.download(header=False, id=self.only_id()["id"])
        self.assertEqual(status, 400)
        self.assertIn("X-Requested-With", body)

    def test_an_unknown_id_downloads_nothing(self):
        self.write("app.log", f"password={SECRET}\n")
        for bad in ("secrets.yaml", "../secrets.yaml", "0" * 32, logfiles_page._file_id("secrets.yaml"), "é"):
            with self.subTest(id=bad):
                status, _, body = self.download(id=bad)
                self.assertEqual(status, 404, body)
                self.assertNotIn(SECRET, body)

    def test_nothing_to_download_when_the_integration_writes_no_log_file(self):
        self.assertEqual(self.download()[0], 404)

    def test_a_real_name_does_not_confirm_a_guess(self):
        self.write(f"session-token={SECRET}.log", "contents\n")
        status, _, body = self.download(file=f"logs/session-token={SECRET}.log")
        self.assertEqual(status, 404, body)
        self.assertNotIn("contents", body)

    def test_an_ambiguous_masked_name_is_refused_not_resolved_to_either(self):
        self.write(f"session-token={SECRET}alpha.log", "contents of alpha\n")
        self.write(f"session-token={SECRET}beta.log", "contents of beta\n")
        status, _, body = self.download(file="logs/session-token=***")
        self.assertEqual(status, 409, body)
        self.assertNotIn("contents of", body)

    def test_a_symlink_is_neither_listed_nor_downloaded(self):
        with open(os.path.join(self.cfg, "secrets.yaml"), "w", encoding="utf-8") as fh:
            fh.write(f"mqtt_password: {SECRET}\n")
        os.symlink(os.path.join(self.cfg, "secrets.yaml"), os.path.join(self.cfg, "logs", "link.log"))
        self.assertEqual(self.listing(), [])
        status, _, body = self.download(file="logs/link.log")
        self.assertEqual(status, 404, body)
        self.assertNotIn(SECRET, body)

    def test_a_hard_linked_file_is_neither_listed_nor_downloaded(self):
        with open(os.path.join(self.cfg, "secrets.yaml"), "w", encoding="utf-8") as fh:
            fh.write(f"mqtt_password: {SECRET}\n")
        os.link(os.path.join(self.cfg, "secrets.yaml"), os.path.join(self.cfg, "logs", "hard.log"))
        self.assertEqual(self.listing(), [])
        status, _, body = self.download(file="logs/hard.log")
        self.assertEqual(status, 404, body)
        self.assertNotIn(SECRET, body)

    def test_the_opener_refuses_a_link_that_appeared_after_the_listing(self):
        """The listing is taken before the request: what is opened is checked again."""
        plain = self.write("app.log", "line\n")
        os.symlink(plain, os.path.join(self.cfg, "logs", "later.log"))
        os.link(plain, os.path.join(self.cfg, "logs", "second-name.log"))
        for path in ("later.log", "second-name.log"):
            with self.subTest(path=path):
                with self.assertRaises(OSError):
                    logfiles_page._MaskedDownload(os.path.join(self.cfg, "logs", path)).open()

    def test_a_file_that_went_away_is_reported_without_naming_it(self):
        self.write(f"session-token={SECRET}.log", "line\n")
        chosen = self.only_id()
        with mock.patch.object(logfiles_page._MaskedDownload, "open",
                               side_effect=FileNotFoundError(2, "No such file or directory",
                                                             os.path.join(self.cfg, "logs", f"session-token={SECRET}.log"))):
            status, _, body = self.download(id=chosen["id"])
        self.assertEqual(status, 404)
        self.assertNotIn(SECRET, body)
        self.assertNotIn(self.cfg, body)

    # ----- the name of the saved file --------------------------------------

    def test_the_content_disposition_carries_the_masked_name(self):
        self.write(f"session-token={SECRET}.log", "line\n")
        status, headers, _ = self.download(id=self.only_id()["id"])
        self.assertEqual(status, 200)
        disposition = headers["Content-Disposition"]
        self.assertNotIn(SECRET, disposition)
        self.assertNotIn("/", disposition.split("filename=")[1])
        # the mask took the extension with it (masked from the "=" on): a log file still opens as one
        self.assertEqual(disposition, 'attachment; filename="logs-session-token.log"')

    def test_a_name_that_masks_to_nothing_usable_still_gets_one(self):
        self.assertEqual(logfiles_page._download_name("***", False), "log.log")
        self.assertEqual(logfiles_page._download_name("a" * 400 + ".log", False),
                         ("a" * 400 + ".log")[-logfiles_page.MAX_DOWNLOAD_NAME:])

    # ----- the size decision -----------------------------------------------

    def test_a_file_longer_than_the_budget_is_sent_from_its_end_and_says_so(self):
        budget = 256 * 1024
        line = "line {:06d} nothing to hide here, padding padding padding\n"
        text = "".join(line.format(n) for n in range(20_000))  # well past the budget
        self.write("app.log", text)
        with mock.patch.object(logfiles_page, "MAX_SCAN_BYTES", budget):
            status, headers, body = self.download(id=self.only_id()["id"])
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body.encode()), budget)
        self.assertEqual(headers["X-Log-Truncated"], str(budget))
        self.assertIn("-last-0MiB.log", headers["Content-Disposition"])  # MiB of the patched budget
        self.assertTrue(text.endswith(body), "the newest lines, ending where the file ends")
        self.assertRegex(body.splitlines()[0], r"^line \d{6} ")  # no half a line at the front

    def test_a_file_within_the_budget_is_not_marked_truncated(self):
        self.write("app.log", "short\n")
        _, headers, _ = self.download(id=self.only_id()["id"])
        self.assertNotIn("X-Log-Truncated", headers)
        self.assertNotIn("last-", headers["Content-Disposition"])

    def test_a_file_with_no_newline_in_it_is_masked_in_pieces_not_held_whole(self):
        self.write("app.log", "x" * 5000)
        with mock.patch.object(logfiles_page, "MAX_LINE_CHARS", 512), \
             mock.patch.object(logfiles_page, "DOWNLOAD_CHUNK", 256):
            status, _, body = self.download(id=self.only_id()["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body, "x" * 5000)  # put back together without a newline the file never had

    # ----- the answer's own headers ----------------------------------------

    def test_the_answer_carries_the_policy_and_is_not_sniffed_or_cached(self):
        self.write("app.log", "line\n")
        _, headers, _ = self.download(id=self.only_id()["id"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")


class AccessLogTest(unittest.TestCase):
    """The request line of a download must not carry the name either."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.path = os.path.join(tmp, "process.log")
        self.handler = logbuffer.FileLogHandler(self.path)
        self.addCleanup(self.handler.close)
        self.logger = logging.getLogger("hri.test.download.access")
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers = [self.handler]
        self.addCleanup(setattr, self.logger, "handlers", [])

    def _log(self, url):
        request = make_mocked_request("GET", url, headers={"Host": "10.0.0.2:8213", "User-Agent": "probe/1"})
        AccessLogger(self.logger, AccessLogger.LOG_FORMAT).log(request, web.Response(text="{}"), 0.01)

    def _messages(self):
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line)["message"] for line in fh.read().splitlines()]

    def test_the_masked_name_asked_for_never_reaches_process_log(self):
        self._log("/api/log_files/download?" + urlencode({"file": f"logs/{SECRET}.log"}))
        self._log("/api/log_files/download?" + urlencode({"id": "a" * 32}))
        messages = self._messages()
        self.assertFalse([m for m in messages if SECRET in m], messages)
        self.assertIn('"GET /api/log_files/download?file=*** HTTP/1.1" 200', messages[0])
        # an id is a keyed hash of the name and says nothing about it: it stays, so a log can be followed
        self.assertIn(f'"GET /api/log_files/download?id={"a" * 32} HTTP/1.1" 200', messages[1])

    def test_a_parameter_this_endpoint_does_not_have_is_masked_as_well(self):
        """Everything but the listed parameters is masked, so a parameter added
        later cannot leak before its rule is written."""
        self._log("/api/log_files/download?" + urlencode({"name": f"logs/{SECRET}.log", "q": SECRET}))
        message = self._messages()[0]
        self.assertNotIn(SECRET, message)
        self.assertIn("?name=***&q=***", message)


if __name__ == "__main__":
    unittest.main()
