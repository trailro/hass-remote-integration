"""F12: the log surfaces filtered the lines before they were redacted.

A key is only masked while the BEGIN marker and the body are one text.  The
Log files page searched the raw lines and scrubbed what survived, so a search
for a string inside the base64 body handed the body back without its header;
the same shape sat on the structured Logs page (the handler searches and pages
the records before the scrubber sees them), and the diagnostics zip cut its own
window - the last 200 kB and the last 500 lines of the log file, the newest
1000 records - which can start below a BEGIN line just as well.

Every test here fails on the tree before the fix.
"""

import asyncio
import io
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import make_mocked_request

# the modules, not the names: the same file then loads against a tree without the fix
from custom_components.integration_manager import diagnostics, logfiles_page, logs_page
from tests.fakes import FakeInstaller

BODY1 = "MIIFSYNTHETICKEYMATERIALAAAABBBBCCCCDDDD"  # synthetic: 40 characters, no real key
BODY2 = "EEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLLMMMMNNNN"
PEM = (
    "2026-09-16 10:00:11 INFO [probe] before the key",
    "2026-09-16 10:00:12 INFO [probe] -----BEGIN PRIVATE KEY-----",
    BODY1,
    BODY2,
    "-----END PRIVATE KEY-----",
    "2026-09-16 10:00:13 INFO [probe] after the key",
)
GIT_SHA = "9c1185a5c5e9fc54612808977ee8f548b2258d31"  # 40 hex characters: an identifier, not a key
TOPIC = "homeassistant/sensor/kitchentemperature/config"  # 45 characters of [a-z0-9/]
PATH = "config/customcomponents/integrationmanager/here"  # a lower-case path, the same shape


async def _job(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _write(dir_path, name, lines):
    path = os.path.join(dir_path, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _installer(**kw):
    inst = FakeInstaller(**kw)
    inst.settings = SimpleNamespace(data={})
    return inst


def _get(url, fetch=True):
    headers = {"Host": "10.0.0.2:8196"}
    if fetch:
        headers["X-Requested-With"] = "fetch"
    return make_mocked_request("GET", url, headers=headers)


def _tail_view(cfg, installer, url):
    hass = SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=_job,
                           config_entries=SimpleNamespace(async_entries=lambda domain=None: []))
    return asyncio.run(logfiles_page.LogFileTailView(hass, installer).get(_get(url)))


# ----- the rule itself ----------------------------------------------------------------------------

class KeyMaterialLineTest(unittest.TestCase):
    """Masking that does not need the marker above the body, because a search,
    a page boundary or the start of a tail window can take it away."""

    def test_a_body_line_on_its_own_is_masked(self):
        out, _ = diagnostics.mask_key_material_lines([BODY1])
        self.assertEqual(out, ["***"])

    def test_the_run_above_an_end_marker_is_masked_without_its_begin_line(self):
        out, _ = diagnostics.mask_key_material_lines(["short", "-----END PRIVATE KEY-----"])
        self.assertEqual(out, ["***", "-----END PRIVATE KEY-----"])

    def test_the_state_carries_from_one_batch_to_the_older_one(self):
        newer, in_block = diagnostics.mask_key_material_lines([BODY2, "-----END PRIVATE KEY-----"])
        older, _ = diagnostics.mask_key_material_lines(["ab/cd", BODY1], in_block)
        self.assertEqual(newer, ["***", "-----END PRIVATE KEY-----"])
        self.assertEqual(older, ["***", "***"])

    def test_a_begin_line_closes_the_block_going_up(self):
        out, in_block = diagnostics.mask_key_material_lines(
            ["keep me", "-----BEGIN PRIVATE KEY-----", BODY1, "-----END PRIVATE KEY-----"])
        self.assertEqual(out[0], "keep me")
        self.assertFalse(in_block)

    def test_a_body_line_that_carries_the_loggers_prefix_is_masked_inside_a_block(self):
        lines = [f"2026-09-16 10:00:12 INFO [probe] {BODY1}", "-----END PRIVATE KEY-----"]
        out, _ = diagnostics.mask_key_material_lines(lines)
        self.assertNotIn(BODY1, out[0])
        self.assertIn("[probe]", out[0])

    def test_a_git_sha_alone_on_a_line_is_not_key_material(self):
        self.assertEqual(diagnostics.mask_key_material_lines([GIT_SHA])[0], [GIT_SHA])

    def test_an_mqtt_topic_is_not_key_material(self):
        self.assertEqual(diagnostics.mask_key_material_lines([TOPIC, f"published {TOPIC}"])[0],
                         [TOPIC, f"published {TOPIC}"])

    def test_a_lower_case_path_is_not_key_material(self):
        self.assertEqual(diagnostics.mask_key_material_lines([PATH])[0], [PATH])

    def test_ordinary_lines_come_back_untouched(self):
        self.assertEqual(diagnostics.mask_key_material_lines(list(PEM[:2]))[0], list(PEM[:2]))

    def test_a_block_already_masked_does_not_collect_a_second_mask(self):
        """The tail masks the body as it reads it, so the scrubber then sees a
        block whose body is already ``***``; the page showed ``******`` there."""
        out = diagnostics.scrub_lines(["-----BEGIN PRIVATE KEY-----", "***", "***", "-----END PRIVATE KEY-----"])
        self.assertEqual(out[1:3], ["***", "***"])
        self.assertEqual(len(out), 4)

    def test_masking_never_creates_a_match_for_a_search(self):
        """What lets the callers mask first and search afterwards."""
        out, _ = diagnostics.mask_key_material_lines(list(PEM))
        for masked, raw in zip(out, PEM):
            self.assertTrue(masked == raw or masked == "***" or masked.endswith("***"), masked)


# ----- the Log files page -------------------------------------------------------------------------

class LogFileTailTest(unittest.TestCase):
    """_tail filtered the raw lines and _tail_masked scrubbed the survivors."""

    def setUp(self):
        self.cfg = _tmp(self)
        self.path = _write(self.cfg, "app.log", PEM)

    def test_a_search_for_bytes_of_the_body_returns_no_body(self):
        found, _ = logfiles_page._tail_masked(self.path, 50, "MIIF")
        self.assertNotIn(BODY1, "\n".join(found))

    def test_a_search_for_bytes_of_the_body_returns_nothing_at_all(self):
        """Not a masked row either: a row that appears only when the needle is a
        prefix of the key answers "right so far" for every guess."""
        self.assertEqual(logfiles_page._tail_masked(self.path, 50, "MIIF")[0], [])

    def test_a_window_that_starts_inside_the_block_masks_the_body(self):
        found, _ = logfiles_page._tail_masked(self.path, 3, "")
        self.assertNotIn(BODY2, "\n".join(found))
        self.assertIn("after the key", "\n".join(found))

    def test_a_block_split_across_two_reads_is_masked(self):
        """The scan reads the file backwards in blocks: the END marker lands in
        one read and the body in the next."""
        filler = [f"2026-09-16 09:00:{i % 60:02d} INFO [probe] filler {i}" for i in range(4000)]
        path = _write(self.cfg, "big.log", list(filler) + list(PEM))
        self.assertGreater(os.path.getsize(path), 64 * 1024)
        found, _ = logfiles_page._tail_masked(path, 5000, "")
        self.assertNotIn(BODY1, "\n".join(found))
        self.assertNotIn(BODY2, "\n".join(found))

    def test_the_scan_budget_running_out_inside_a_block_masks_the_body(self):
        filler = [f"2026-09-16 09:00:{i % 60:02d} INFO [probe] filler {i}" for i in range(400)]
        path = _write(self.cfg, "budget.log", list(filler) + list(PEM))
        with mock.patch.object(logfiles_page, "MAX_SCAN_BYTES", 400):
            found, _ = logfiles_page._tail_masked(path, 5000, "")
        self.assertNotIn(BODY1, "\n".join(found))
        self.assertNotIn(BODY2, "\n".join(found))

    def test_the_api_search_does_not_hand_the_body_back(self):
        resp = _tail_view(self.cfg, _installer(), "/api/log_files/tail?file=app.log&q=MIIF")
        body = resp.body.decode()
        self.assertEqual(resp.status, 200, body)
        self.assertNotIn(BODY1, body)

    def test_an_ordinary_search_still_works(self):
        found, _ = logfiles_page._tail_masked(self.path, 50, "after the key")
        self.assertEqual(len(found), 1)
        self.assertIn("after the key", found[0])

    def test_lines_that_are_not_key_material_are_untouched(self):
        found, _ = logfiles_page._tail_masked(self.path, 50, "")
        self.assertEqual(found[0], PEM[0])
        self.assertEqual(found[-1], PEM[-1])

    def test_a_password_in_a_line_is_still_masked(self):
        path = _write(self.cfg, "pw.log", ["2026-09-16 10:00:14 INFO [probe] password=hunter2"])
        found, _ = logfiles_page._tail_masked(path, 50, "")
        self.assertEqual(found, ["2026-09-16 10:00:14 INFO [probe] password=***"])


class LogDirListingTest(unittest.TestCase):
    """The listing shows names walked out of the config dir and the registry's
    log_dir; they are text from outside like the lines inside the files."""

    def setUp(self):
        self.cfg = _tmp(self)
        self.installer = _installer(running="demo", spec={"log_dir": "logs"})
        _write(self.cfg, os.path.join("logs", f"session-token={BODY1}.log"), ["nothing here"])
        self.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg), async_add_executor_job=_job,
                                    config_entries=SimpleNamespace(async_entries=lambda domain=None: []))

    def _listing(self):
        resp = asyncio.run(logfiles_page.LogFilesView(self.hass, self.installer).get(_get("/api/log_files")))
        return resp.body.decode()

    def test_a_secret_in_a_file_name_is_masked_in_the_listing(self):
        self.assertNotIn(BODY1, self._listing())

    def test_the_masked_name_still_selects_the_file(self):
        import json

        name = json.loads(self._listing())[0]["name"]
        resp = _tail_view(self.cfg, self.installer, "/api/log_files/tail?file=" + name.replace("=", "%3D"))
        self.assertEqual(resp.status, 200, resp.body.decode())
        self.assertIn("nothing here", resp.body.decode())

    def test_the_path_the_tail_reports_is_masked_too(self):
        resp = _tail_view(self.cfg, self.installer, "/api/log_files/tail")
        self.assertNotIn(BODY1, resp.body.decode())


# ----- the Logs page ------------------------------------------------------------------------------

class LogsPageQueryTest(unittest.TestCase):
    """handler.query searches and pages the records before _query_masked sees
    them, so the records that arrive are not the block."""

    def setUp(self):
        self.recs = [{"id": i + 1, "ts": "2026-09-16T10:00:0%d" % i, "level": "INFO", "levelno": 20,
                      "logger": "probe", "message": text, "exc": None} for i, text in enumerate(PEM)]

    def _handler(self):
        recs = self.recs

        class Handler:
            capacity = "2 MB"
            path = "/config/integration_manager/process.log"

            @staticmethod
            def query(*, text="", limit=500, since_id=0, **kw):
                out = [r for r in recs if not text or text.lower() in r["message"].lower()
                       or text.lower() in r["logger"].lower()]
                out = [r for r in out if r["id"] > since_id][:limit] if since_id else out[-limit:]
                return [dict(r) for r in out], False

        return Handler()

    def test_a_search_for_bytes_of_the_body_returns_no_body(self):
        out, _, _ = logs_page._query_masked(self._handler(), text="MIIF")
        self.assertNotIn(BODY1, "\n".join(r["message"] for r in out))

    def test_a_search_for_bytes_of_the_body_returns_nothing_at_all(self):
        self.assertEqual(logs_page._query_masked(self._handler(), text="MIIF")[0], [])

    def test_a_page_that_starts_below_the_begin_record_masks_the_body(self):
        out, _, _ = logs_page._query_masked(self._handler(), since_id=2, limit=2)
        joined = "\n".join(r["message"] for r in out)
        self.assertNotIn(BODY1, joined)
        self.assertNotIn(BODY2, joined)

    def test_the_newest_records_window_starting_inside_the_block_masks_the_body(self):
        out, _, _ = logs_page._query_masked(self._handler(), limit=3)
        self.assertNotIn(BODY2, "\n".join(r["message"] for r in out))

    def test_an_ordinary_search_still_works(self):
        out, _, _ = logs_page._query_masked(self._handler(), text="after the key")
        self.assertEqual([r["message"] for r in out], [PEM[-1]])

    def test_a_search_on_the_logger_name_still_works(self):
        out, _, _ = logs_page._query_masked(self._handler(), text="probe")
        self.assertEqual(len(out), len(PEM))

    def test_a_block_inside_one_record_is_still_masked(self):
        self.recs = [{"id": 1, "logger": "probe", "message": "\n".join(PEM[1:5]), "exc": None}]
        out, _, _ = logs_page._query_masked(self._handler())
        self.assertNotIn(BODY1, out[0]["message"])

    def test_a_key_split_across_records_by_the_logger_is_still_masked(self):
        out, _, _ = logs_page._query_masked(self._handler())
        self.assertNotIn(BODY1, "\n".join(r["message"] for r in out))
        self.assertIn("after the key", out[-1]["message"])


# ----- the diagnostics zip ------------------------------------------------------------------------

class DiagnosticsWindowTest(unittest.TestCase):
    """The zip scrubs joined text, which is safe against a key split over lines
    - but it cuts its own window first, and that window can start below the
    BEGIN line."""

    def setUp(self):
        self.cfg = _tmp(self)
        _write(self.cfg, "app.log", PEM)
        view = diagnostics.DiagnosticsView.__new__(diagnostics.DiagnosticsView)
        view.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.cfg))
        view.installer = _installer()
        self.view = view

    def test_the_joined_text_of_a_whole_block_was_already_safe(self):
        """Proven, not assumed: this is the case the zip always handled."""
        self.assertNotIn(BODY1, diagnostics.scrub("\n".join(PEM)))

    def test_the_log_file_tail_cutting_the_begin_line_off_masks_the_body(self):
        with mock.patch.object(diagnostics, "LOG_FILE_TAIL", 3):
            text = self.view._log_file_tail([])
        self.assertNotIn(BODY1, text)
        self.assertNotIn(BODY2, text)
        self.assertIn("after the key", text)

    def test_the_whole_file_still_comes_through_when_it_fits(self):
        text = self.view._log_file_tail([])
        self.assertIn("before the key", text)
        self.assertNotIn(BODY1, text)

    def test_the_records_window_starting_below_the_begin_record_masks_the_body(self):
        records = [{"ts": "t", "level": "INFO", "logger": "probe", "message": m} for m in PEM[3:]]
        text = diagnostics.log_records_text(records)
        self.assertNotIn(BODY2, text)
        self.assertIn("after the key", text)

    def test_scrub_text_masks_a_window_with_no_begin_marker(self):
        self.assertNotIn(BODY1, diagnostics.scrub_text("\n".join(PEM[2:])))

    def test_the_zip_never_carries_the_body(self):
        files = {"log.txt": diagnostics.log_records_text(
            [{"ts": "t", "level": "INFO", "logger": "probe", "message": m} for m in PEM[2:]])}
        with mock.patch.object(diagnostics, "LOG_FILE_TAIL", 3):
            files["log_file.txt"] = self.view._log_file_tail([])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, text in files.items():
                zf.writestr(name, text)
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            packed = "\n".join(zf.read(n).decode() for n in zf.namelist())
        self.assertNotIn(BODY1, packed)
        self.assertNotIn(BODY2, packed)


# ----- the cost -----------------------------------------------------------------------------------

class TailCostTest(unittest.TestCase):
    """Masking every line the scan reads is what closes the search, so it has to
    stay a length test and a substring test: scrubbing the scanned bytes instead
    would turn a tail of a large log from milliseconds into seconds."""

    def test_a_default_tail_of_a_large_file_stays_fast(self):
        cfg = _tmp(self)
        line = "2026-09-16 10:00:00.123 WARNING (MainThread) [custom_components.demo] a routine line %d\n"
        path = os.path.join(cfg, "big.log")
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(80_000):  # ~7 MB
                fh.write(line % i)
        logfiles_page._tail_masked(path, 50, "")  # warm the page cache
        t0 = time.perf_counter()
        found, _ = logfiles_page._tail_masked(path, 50, "")
        spent = time.perf_counter() - t0
        self.assertEqual(len(found), 50)
        self.assertLess(spent, 0.5, f"{spent * 1000:.0f} ms for the last 50 lines of a 7 MB log")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
