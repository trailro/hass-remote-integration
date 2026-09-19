"""The masking and the log-file opener after the external review.

F-01: the value alternative of ``_SECRET_TEXT`` stopped its unquoted branch at
the first quote, so a value that came wrapped had only its wrapper masked:
``password=b'hunter2'`` -> ``password=***'hunter2'``, and the same for
``SecretStr('hunter2')``.  An auth scheme was eaten as the whole value
(``token: Bearer abc...`` -> ``token: *** abc...``), and ``_BEARER``, which
would have caught what was left, was case-sensitive and ran after the value had
already been cut.  ``\\bcode`` could not match after an underscore, because
``_`` is a word character, so ``user_code: 1234`` came out whole although
SECURITY.md promises that a known name is masked.  The same scrubber feeds the
Logs page, the log tail, search and download, and the diagnostics zip people
attach to public issues.

F-03: ``_URL_CRED`` ran an unbounded lazy run to the next ``@`` from every
``scheme://x:`` on the line, which is quadratic on a long line with no
whitespace - log content is device-influenced, and the cost is paid on an
executor thread holding the diagnostics lock.

F-15: the tail and the diagnostics zip opened a listed log path with a plain
``open()``, while the download opened it with ``O_NOFOLLOW`` and checked the
opened file.  A listed ``my.log`` swapped for a symlink to ``secrets.yaml``
between the listing and the request was read by the first two and refused by
the third.  All three now share one opener.

Every test here fails on the tree before the fix unless its docstring says it
pins behaviour that already held.
"""

import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

# the modules, not the names: the same file then loads against a tree without the fix
from custom_components.integration_manager import diagnostics, logfiles_page
from tests.fakes import FakeInstaller

SECRET = "hunter2-SNTL-4b91c0"  # synthetic
TOKEN = "AbCdEf0123456789xyz"  # synthetic, long enough for _BEARER


def _scrub(text):
    return diagnostics._scrub_one_line_rules(text)


class WrappedSecretValueTest(unittest.TestCase):
    """F-01: a value the scrubber can still see through its wrapper."""

    def assertMasked(self, text):
        out = _scrub(text)
        self.assertNotIn(SECRET, out, f"{text!r} -> {out!r}")
        self.assertIn("***", out, f"{text!r} -> {out!r}")
        return out

    def test_a_bytes_repr_before_the_quote(self):
        self.assertEqual(self.assertMasked(f"password=b'{SECRET}'"), "password=b'***'")
        self.assertEqual(self.assertMasked(f'api_key=b"{SECRET}"'), 'api_key=b"***"')
        self.assertMasked(f"{{'password': b'{SECRET}'}}")

    def test_a_wrapper_call_before_the_quote(self):
        self.assertEqual(self.assertMasked(f"password=SecretStr('{SECRET}')"), "password=SecretStr('***')")
        self.assertEqual(self.assertMasked(f'password: SecretStr("{SECRET}")'), 'password: SecretStr("***")')
        self.assertMasked(f"{{'token': SecretStr('{SECRET}')}}")

    def test_a_parenthesised_value(self):
        self.assertMasked(f"secret=('{SECRET}')")

    def test_an_auth_scheme_belongs_to_the_value(self):
        for scheme in ("Bearer", "Basic", "Token", "bearer"):
            with self.subTest(scheme=scheme):
                out = _scrub(f"token: {scheme} {TOKEN}")
                self.assertNotIn(TOKEN, out)
                self.assertEqual(out, "token: ***")

    def test_the_bearer_rule_is_case_insensitive(self):
        # a name _SECRET_TEXT does not know, so only _BEARER can mask this line
        out = _scrub(f"hdr: bearer {TOKEN}")
        self.assertNotIn(TOKEN, out)
        self.assertEqual(out, "hdr: bearer ***")

    def test_a_code_name_after_an_underscore(self):
        for name in ("user_code", "device_code", "pairing_code"):
            with self.subTest(name=name):
                out = _scrub(f"{name}: 123456")
                self.assertEqual(out, f"{name}: ***")

    def test_a_wrapped_value_inside_an_escaped_json_string(self):
        out = self.assertMasked(f'log: "{{\\"password\\": b\\"{SECRET}\\"}}"')
        self.assertIn('b\\"***\\"', out)

    def test_the_plain_forms_still_mask(self):
        """Pins behaviour that already held."""
        self.assertEqual(_scrub(f"password={SECRET}"), "password=***")
        self.assertEqual(_scrub(f'{{"password": "{SECRET}"}}'), '{"password": "***"}')
        self.assertEqual(_scrub(f"Authorization: Bearer {TOKEN}"), "Authorization: ***")
        self.assertEqual(_scrub(f"Cookie: session={SECRET}; other=1"), "Cookie: ***")

    def test_a_plain_word_after_basic_is_not_a_token(self):
        """Pins behaviour that already held: case-insensitivity must not mask prose."""
        self.assertEqual(_scrub("Basic information about the device"), "Basic information about the device")
        self.assertEqual(_scrub("basic information about the device"), "basic information about the device")

    def test_a_value_that_is_not_quoted_after_all(self):
        """The wrapper prefix must not swallow an ordinary unquoted value."""
        self.assertEqual(_scrub("password=repr(x)"), "password=***")
        self.assertEqual(_scrub(f"password={SECRET}, user=bob"), "password=***, user=bob")

    def test_a_secret_that_merely_abuts_a_quote_is_not_a_wrapper(self):
        """A bare identifier is not a wrapper: a token at the end of an
        exception message has the message's own closing quote right after it,
        and reading that as ``<prefix><quoted value>`` printed the token and
        masked the quote.  tests/test_r4_web.py pins the same shape."""
        out = _scrub(f'raise RuntimeError("refresh failed with access_token={SECRET}")')
        self.assertNotIn(SECRET, out)
        self.assertEqual(out, 'raise RuntimeError("refresh failed with access_token=***")')
        self.assertEqual(_scrub(f"msg='token={SECRET}'"), "msg='token=***'")


class UrlCredentialCostTest(unittest.TestCase):
    """F-03: the one rule that was not linear.

    The bound is a second, not the tenth of a second the fixed rule actually needs: this runs on
    whatever machine CI was given, and a threshold close to the measurement fails for being on a
    busy runner rather than for being quadratic.  A second still separates the two cases by more
    than an order of magnitude in both directions - the fix measures 0.07 s here and 0.21 s on a
    loaded CI runner, the bug measured 8.3 s on the same input.
    """

    BUDGET_S = 1.0

    def test_a_long_run_without_whitespace_is_bounded(self):
        line = "a://b:c" * 20000  # 140 kB, no whitespace, no "@": 8.3 s before the fix
        best = min(self._time(line) for _ in range(3))
        self.assertLess(best, self.BUDGET_S, f"_scrub_one_line_rules took {best:.3f}s on {len(line)} characters")

    def test_a_long_run_that_does_hold_an_at_sign_is_bounded(self):
        line = "a://b:c" * 20000 + "@host/ "  # the "@" short-circuit cannot help here
        best = min(self._time(line) for _ in range(3))
        self.assertLess(best, self.BUDGET_S, f"_scrub_one_line_rules took {best:.3f}s on {len(line)} characters")

    @staticmethod
    def _time(line):
        start = time.perf_counter()
        diagnostics._scrub_one_line_rules(line)
        return time.perf_counter() - start

    def test_url_credentials_are_still_masked(self):
        """Pins behaviour that already held, including a password holding "@"."""
        self.assertEqual(_scrub("amqp://u:secret@broker:5672/"), "amqp://u:***@broker:5672/")
        self.assertEqual(_scrub("connect to https://user:p@ss@mqtt.local/x"),
                         "connect to https://user:***@mqtt.local/x")
        self.assertEqual(_scrub("see http://a:b@c.example,next"), "see http://a:***@c.example,next")


class LogFileOpenerTest(unittest.TestCase):
    """F-15: the tail, the zip and the download open a listed path the same way."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.secret_file = os.path.join(self.dir, "secrets.yaml")
        with open(self.secret_file, "w", encoding="utf-8") as fh:
            fh.write(f"mqtt_password: {SECRET}\n")
        self.plain = os.path.join(self.dir, "plain.log")
        with open(self.plain, "w", encoding="utf-8") as fh:
            fh.write("2026-09-19 INFO one line\n")

    def _swapped(self, name="my.log"):
        """A listed name that has become a symlink to a file nobody listed."""
        path = os.path.join(self.dir, name)
        os.symlink(self.secret_file, path)
        return path

    def _hard_linked(self, name="two-names.log"):
        path = os.path.join(self.dir, name)
        os.link(self.secret_file, path)
        return path

    def _zip_tail(self, path):
        view = diagnostics.DiagnosticsView.__new__(diagnostics.DiagnosticsView)
        view.hass = SimpleNamespace(config=SimpleNamespace(config_dir=self.dir))
        view.installer = FakeInstaller()
        listing = [{"name": os.path.basename(path), "path": path}]
        with mock.patch.object(diagnostics, "_log_files", return_value=listing):
            return view._log_file_tail([])

    def test_the_tail_refuses_a_symlink(self):
        with self.assertRaises(OSError):
            logfiles_page._tail(self._swapped(), 50, "")

    def test_the_diagnostics_zip_refuses_a_symlink(self):
        text = self._zip_tail(self._swapped())
        self.assertNotIn(SECRET, text)
        self.assertIn("unreadable", text)

    def test_the_download_refuses_a_symlink(self):
        """Pins behaviour that already held: the opener the other two now share."""
        with self.assertRaises(OSError):
            logfiles_page._MaskedDownload(self._swapped()).open()

    def test_a_second_hard_link_is_refused_by_all_three(self):
        path = self._hard_linked()
        with self.assertRaises(OSError):
            logfiles_page._tail(path, 50, "")
        with self.assertRaises(OSError):
            logfiles_page._MaskedDownload(path).open()
        text = self._zip_tail(path)
        self.assertNotIn(SECRET, text)
        self.assertIn("unreadable", text)

    def test_a_plain_file_still_reads(self):
        lines, scanned = logfiles_page._tail(self.plain, 50, "")
        self.assertEqual(lines, ["2026-09-19 INFO one line"])
        self.assertEqual(scanned, 1)
        reader = logfiles_page._MaskedDownload(self.plain)
        reader.open()
        self.addCleanup(reader.close)
        self.assertIn("one line", reader.chunk().decode())
        self.assertIn("one line", self._zip_tail(self.plain))


if __name__ == "__main__":
    unittest.main()


class CodeNamesTest(unittest.TestCase):
    """"code" is an OAuth secret; status_code is a number everybody needs to read.

    Making `code` match after an underscore (so `user_code` masks) also caught `status_code`, and a
    diagnostics bundle with every HTTP status masked is one nobody can debug from.
    """

    def test_an_oauth_code_is_masked_whatever_it_is_called(self):
        for line in ("code: secret99", "user_code: 1234", "device_code: abcd", "auth_code=zz9"):
            with self.subTest(line=line):
                self.assertIn("***", diagnostics.scrub(line))

    def test_a_status_or_result_number_stays_readable(self):
        for line in ("status_code: 404", "error_code: 12", "exit_code=1", "return_code: 2", "reason_code: 5"):
            with self.subTest(line=line):
                self.assertEqual(diagnostics.scrub(line), line)
