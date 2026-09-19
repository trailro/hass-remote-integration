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

R4: the same rule leaked a fourth time, in shapes nobody had described yet (a
``)`` inside a quoted password, a truncated tuple, a triple-quoted value, a
plural name, a percent-encoded ``=``), so where a value ends is now decided
against the value: after a name the rule knows it runs to the end of the line
unless the text says where it ends.  The cases of the first three rounds are
kept, with the expected text of the ones the new rule masks further - each of
those says so in its docstring - and ``ConservativeValueExtentTest``,
``OrdinaryLogLinesTest`` and ``ValueExtentCostTest`` below hold the fourth
round's own.

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
        """R4 reads the wrapper as part of the value: it is masked with it."""
        self.assertEqual(self.assertMasked(f"password=b'{SECRET}'"), "password=***")
        self.assertEqual(self.assertMasked(f'api_key=b"{SECRET}"'), "api_key=***")
        self.assertEqual(self.assertMasked(f"{{'password': b'{SECRET}'}}"), "{'password': ***}")

    def test_a_wrapper_call_before_the_quote(self):
        """R4: the wrapper goes with the value instead of being kept around it."""
        self.assertEqual(self.assertMasked(f"password=SecretStr('{SECRET}')"), "password=***")
        self.assertEqual(self.assertMasked(f'password: SecretStr("{SECRET}")'), "password: ***")
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
        """The value is masked to the end of the string it is written inside: R4
        keeps the quote that closes that string and nothing between it and the
        name, where R3 kept the wrapper and the escaped quotes around it."""
        out = self.assertMasked(f'log: "{{\\"password\\": b\\"{SECRET}\\"}}"')
        self.assertEqual(out, 'log: "{\\"password\\": ***"')

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


class NestedWrapperTest(unittest.TestCase):
    """R2-02: a named value is masked whatever it is wrapped in.

    The first round read one wrapper - one ``(`` or one string prefix - so a
    value that arrived in two (``SecretStr(b'x')``), inside a container
    (``{'password': ['x']}``), as a keyword argument (``SecretStr(value='x')``)
    or under a dotted name (``pydantic.SecretStr('x')``) still had its wrapper
    masked and its secret printed.  ``auth=('user', 'x')`` masked the first
    element of the tuple and printed the second next to it.  And the auth scheme
    the first round added was possessive and lived in the unquoted branch only,
    so ``token: Bearer 'x'`` ate the scheme, failed on the quote after it, and
    came out with nothing masked at all - a leak the round-one fix introduced.
    """

    def assertMasked(self, text, expected=None, secret=None):
        out = _scrub(text)
        self.assertNotIn(secret or SECRET, out, f"{text!r} -> {out!r}")
        self.assertIn("***", out, f"{text!r} -> {out!r}")
        if expected is not None:
            self.assertEqual(out, expected)
        return out

    def test_two_wrappers_around_one_value(self):
        """R4 no longer counts the layers: whatever the value came in goes with it."""
        self.assertMasked(f"password=SecretStr(b'{SECRET}')", "password=***")
        self.assertMasked(f'password=SecretStr(rb"{SECRET}")', "password=***")

    def test_a_dotted_wrapper_name(self):
        self.assertMasked(f"password=pydantic.SecretStr('{SECRET}')", "password=***")

    def test_a_keyword_argument_inside_the_wrapper(self):
        """The spaces around the ``=`` are the fourth round's own case: the
        wrapper rule read ``value=`` and not ``value = ``, and printed the
        secret after it."""
        self.assertMasked(f"password=SecretStr(value='{SECRET}')", "password=***")
        self.assertMasked(f"password=SecretStr(value = '{SECRET}')", "password=***")

    def test_a_repr_that_names_its_type_inside_the_brackets(self):
        self.assertMasked(f"password=<SecretStr '{SECRET}'>", "password=***")

    def test_a_value_inside_a_list(self):
        self.assertMasked("{'password': ['" + SECRET + "']}", "{'password': ***}")
        self.assertMasked('{"api_key": ["' + SECRET + '"]}', '{"api_key": ***}')

    def test_a_tuple_goes_whole(self):
        """R3 masked through the closing bracket and kept the brackets; R4 masks
        the tuple whether or not the line still holds its closing bracket - the
        truncated form printed the password next to the mask."""
        self.assertMasked(f"auth=('user', '{SECRET}')", "auth=***")
        self.assertMasked(f"auth=('user', '{SECRET}'", "auth=***")

    def test_a_quoted_value_after_a_known_auth_scheme(self):
        for scheme in ("Bearer", "Basic", "Token", "bearer"):
            with self.subTest(scheme=scheme):
                self.assertMasked(f"token: {scheme} '{TOKEN}'", "token: ***", secret=TOKEN)

    def test_an_auth_scheme_the_bearer_rule_does_not_know(self):
        for scheme in ("Digest", "Negotiate", "NTLM"):
            with self.subTest(scheme=scheme):
                self.assertMasked(f"token: {scheme} {TOKEN}", "token: ***", secret=TOKEN)

    def test_a_bare_identifier_is_still_not_a_wrapper(self):
        """Pins behaviour that already held, and that this must not undo: the
        round-one attempt at a wider prefix printed a token the scrubber had
        been masking.  tests/test_r4_web.py pins the same shape."""
        self.assertEqual(_scrub(f'raise RuntimeError("refresh failed with access_token={SECRET}")'),
                         'raise RuntimeError("refresh failed with access_token=***")')
        self.assertEqual(_scrub(f"msg='token={SECRET}'"), "msg='token=***'")

    def test_a_value_with_no_bracket_keeps_what_follows_it(self):
        """Pins behaviour that already held: the run to a closing bracket only
        exists for a value that opened one, so an ordinary JSON line keeps its
        comma, its next field and its own closing brace."""
        self.assertEqual(_scrub(f'{{"password": "{SECRET}", "user": "bob"}}'),
                         '{"password": "***", "user": "bob"}')


class UrlCredentialLengthTest(unittest.TestCase):
    """R2-04: the bound the first round put on the credential run.

    Bounding the run at 256 characters left a credential longer than that with
    no match at all, so it went into the zip whole - the unbounded rule it
    replaced had masked it.  And the run was lazy, so it stopped at the first
    ``@`` in the authority rather than the last, which leaves the rest of a
    password that holds an ``@`` printed
    (``rtsp://admin:p@ss/w0rd@192.168.1.5``).  That second one is not fixed
    here and has no test: ``tests/test_r3_web.py`` pins the opposite answer for
    the same text (``http://u:p@host/users/@me`` keeps its path), and the two
    are indistinguishable - the first ``@`` of each is followed by a run of
    host characters and a ``/``.
    """

    def test_a_credential_longer_than_the_bound(self):
        jwt = "eyJ" + "A" * 300  # synthetic, longer than _URL_CRED_MAX
        out = _scrub(f"git fetch https://oauth2:{jwt}@gitlab.example.com/x.git")
        self.assertNotIn(jwt, out, out)
        self.assertEqual(out, "git fetch https://oauth2:***")

    def test_a_credential_longer_than_the_bound_inside_a_json_string(self):
        jwt = "eyJ" + "B" * 400
        out = _scrub(f'{{"remote": "https://oauth2:{jwt}@gitlab.example.com/x.git"}}')
        self.assertNotIn(jwt, out, out)
        self.assertEqual(out, '{"remote": "https://oauth2:***"}')

    def test_two_credentialed_urls_on_one_line(self):
        """The shape a greedy run must not swallow: the second URL's host is a
        host, so a run reaching for the last ``@`` on the line masks everything
        between the two and prints neither.  The ``&`` form leaked before this
        round as well - the host ended at an ``&`` the rule did not accept as
        an end, so neither URL matched."""
        self.assertEqual(_scrub(f'{{"a":"http://u:{SECRET}@h1","b":"http://u2:{SECRET}2@h2"}}'),
                         '{"a":"http://u:***@h1","b":"http://u2:***@h2"}')
        self.assertEqual(_scrub(f"url=http://a:{SECRET}@c.example&next=http://d:{SECRET}@f.example"),
                         "url=http://a:***@c.example&next=http://d:***@f.example")


class UrlCredentialGrowthTest(unittest.TestCase):
    """R2-03: the scheme backtracked, and the long-credential runs must not.

    ``\\b[a-z][a-z0-9+.-]*://`` let the scheme run to the end of the line and
    walk back looking for ``://`` from every letter of it, which is quadratic
    on a line of ``a.a.a...`` - 2.3 s on 40 kB and 9.1 s on 80 kB, four times
    the time for twice the input.  A single ``@`` anywhere on the line is
    enough to defeat the caller's short-circuit and pay it.

    Both checks are on growth rather than on a stopwatch: twice the input for
    roughly twice the time is what separates the fix from the bug, and a
    threshold tight enough to catch the bug by its absolute time is one a busy
    runner fails for being busy.  The floor keeps a measurement of a few
    milliseconds from turning scheduler noise into a failure.
    """

    FLOOR_S = 0.25

    @staticmethod
    def _time(line):
        return min(UrlCredentialGrowthTest._once(line) for _ in range(3))

    @staticmethod
    def _once(line):
        start = time.perf_counter()
        diagnostics._scrub_one_line_rules(line)
        return time.perf_counter() - start

    def assertLinear(self, make):
        small, large = self._time(make(10000)), self._time(make(20000))
        self.assertLess(large, max(3 * small, self.FLOOR_S),
                        f"{small:.3f}s on half the input, {large:.3f}s on all of it")

    def test_the_scheme_does_not_backtrack(self):
        self.assertLinear(lambda n: "a." * n + "@")

    def test_the_long_credential_branches_do_not_rescan_the_line(self):
        """The new branches, pinned the same way: an ``@`` early on defeats the
        short-circuit and none follows the URLs, so every ``scheme://x:`` on the
        line runs both of them and fails."""
        self.assertLinear(lambda n: "x@y" + "a://b:c" * n)


class DictCodeNamesTest(unittest.TestCase):
    """R2-14 / R2-15: one list of code names for the dict rule and the text rule.

    ``scrub()`` on a dict knew ``pin_code`` and nothing else, so an OAuth code
    in a status or in a config entry's options went into the zip whole although
    the same name in a log line was masked.  In the other direction the text
    rule masked ``http_code`` and ``response_code``, which are HTTP statuses
    like the siblings already excluded by name.
    """

    def test_an_oauth_code_in_a_dict_is_masked(self):
        for name in ("code", "user_code", "device_code", "auth_code", "pin_code", "device-code"):
            with self.subTest(name=name):
                self.assertEqual(diagnostics.scrub({name: SECRET}), {name: "***"})

    def test_a_result_code_in_a_dict_stays_readable(self):
        for name in ("status_code", "error_code", "exit_code", "return_code",
                     "reason_code", "http_code", "response_code"):
            with self.subTest(name=name):
                self.assertEqual(diagnostics.scrub({name: 404}), {name: 404})

    def test_an_http_code_in_a_line_stays_readable(self):
        for line in ("http_code: 404", "response_code=500"):
            with self.subTest(line=line):
                self.assertEqual(_scrub(line), line)

    def test_a_name_that_merely_ends_in_code_is_not_one(self):
        """Pins what mqtt_publisher documents: zipcode, barcode, not a code."""
        for name in ("zipcode", "barcode"):
            with self.subTest(name=name):
                self.assertEqual(diagnostics.scrub({name: "12345"}), {name: "12345"})


class ConservativeValueExtentTest(unittest.TestCase):
    """R4: where a value ends is decided against the value, not for it.

    Three rounds each closed the case the reviewer brought and left the class
    open, because the rule described the shapes a value can take and masked
    what it recognised - a wrapper, a bracket, an auth scheme - so the shape
    nobody had described yet was printed next to the mask.  The fourth round
    inverts it: after a name the rule knows, the value is the rest of the line
    unless the text itself says where it ends (a delimiter that was open before
    the name, the next name=value pair, the quote a quoted value opened with).

    The leaks below are the ones the fourth reviewer reproduced on the tree
    before this, each of them a shape the third round's wrapper prefix did not
    describe.
    """

    def assertMasked(self, text, expected=None, secret=SECRET):
        out = _scrub(text)
        self.assertNotIn(secret, out, f"{text!r} -> {out!r}")
        self.assertIn("***", out, f"{text!r} -> {out!r}")
        if expected is not None:
            self.assertEqual(out, expected)
        return out

    def test_a_closing_bracket_inside_the_value_ends_nothing(self):
        """aiohttp's own repr of BasicAuth, with a ")" in the password: the run
        to the closing bracket stopped at that one and printed the rest."""
        self.assertMasked(f"auth=BasicAuth(login='bob', password='hunt){SECRET}', encoding='latin1')", "auth=***")

    def test_a_value_whose_bracket_never_closes(self):
        """A line the logger cut: with no closing bracket there was no match at
        all, and the tuple was printed whole."""
        self.assertMasked(f"auth=('bob', '{SECRET}'", "auth=***")

    def test_a_triple_quoted_value(self):
        self.assertMasked(f'password="""{SECRET}"""', 'password="""***"""')
        self.assertMasked(f"password='''{SECRET}'''", "password='''***'''")

    def test_a_plural_name_is_the_same_name(self):
        for name in ("passwords", "tokens", "api_keys", "secrets", "credentials", "pin_codes", "otps",
                     "sigs", "psks", "passphrases", "access_tokens", "security_keys", "session_ids"):
            with self.subTest(name=name):
                self.assertMasked(f"{name}=['{SECRET}']", f"{name}=***")
                self.assertMasked(f"{name}: ['{SECRET}']", f"{name}: ***")

    def test_a_percent_encoded_separator(self):
        """?password%3Dx: a query string whose "=" is encoded had no separator
        any rule read, so the value went into the zip whole."""
        self.assertMasked(f"GET /x?password%3D{SECRET}", "GET /x?password%3D***")
        self.assertMasked(f"?token%3d{SECRET}", "?token%3d***")
        self.assertMasked(f"?api_key%3A{SECRET}", "?api_key%3A***")
        self.assertMasked(f"password %3D {SECRET}", "password %3D ***")

    def test_a_wrapper_the_rule_never_described(self):
        """Spaces around a keyword argument, and a wrapper whose first argument
        is not the secret: both printed the value before this round."""
        self.assertMasked(f"password=SecretStr(value = '{SECRET}')", "password=***")
        self.assertMasked(f"password=Secret(1, '{SECRET}')", "password=***")

    def test_an_authorization_header_goes_whole(self):
        """Digest writes its value as name=value pairs, so the stop at the next
        pair would end the value inside the header: it does not apply here."""
        self.assertMasked(f'Authorization: Digest username="bob", realm="r", response="{SECRET}"',
                          "Authorization: ***")
        self.assertMasked(f"Authorization: Custom-Scheme {SECRET} realm=x", "Authorization: ***")

    def test_a_header_still_ends_at_a_url_or_at_another_name(self):
        """Neither prints anything: a credential in the URL is masked by its own
        rule, and the name that follows has its own value masked."""
        out = self.assertMasked(f"Authorization: Bearer {TOKEN} mqtt://user:{SECRET}@host:1883", secret=TOKEN)
        self.assertEqual(out, "Authorization: *** mqtt://user:***@host:1883")
        self.assertEqual(_scrub(f"auth header Authorization: Bearer {TOKEN} access_token={SECRET}"),
                         "auth header Authorization: *** access_token=***")

    def test_what_the_rule_still_leaves_readable(self):
        """The three stops, and the names that report a result."""
        self.assertEqual(_scrub(f'{{"password": "{SECRET}", "user": "bob"}}'),
                         '{"password": "***", "user": "bob"}')  # the quote the value opened with
        self.assertEqual(_scrub(f'raise RuntimeError("refresh failed with access_token={SECRET}")'),
                         'raise RuntimeError("refresh failed with access_token=***")')  # a quote opened before the name
        self.assertEqual(_scrub(f"token={SECRET} next=1"), "token=*** next=1")  # the next name=value pair
        self.assertEqual(_scrub(f"{{'password': '{SECRET}', 'port': 1883}}"), "{'password': '***', 'port': 1883}")
        self.assertEqual(_scrub("status_code: 404, reason: Not Found"), "status_code: 404, reason: Not Found")

    def test_a_value_that_is_masked_already_is_not_masked_again(self):
        """logbuffer masks a query value before the record is written; masking
        the mask again would take the rest of the line with it."""
        self.assertEqual(_scrub('"GET /x?token=***&page=2 HTTP/1.1" 200'), '"GET /x?token=***&page=2 HTTP/1.1" 200')

    def test_an_empty_value_invents_nothing(self):
        self.assertEqual(_scrub("password="), "password=")
        self.assertEqual(_scrub("Cookie: "), "Cookie: ")


class OrdinaryLogLinesTest(unittest.TestCase):
    """The other half of the trade: a bundle nobody can read is a bundle nobody
    can debug from.  A line that holds a word like ``token`` or ``key`` as
    prose, and a line whose names are not secrets, comes out as it went in."""

    LINES = (
        "2026-09-19 10:21:33 INFO [custom_components.demo] Setting up entry demo (0.31 s)",
        "GET /api/states 200 in 0.012 s",
        "Connection to 192.168.1.5:1883 established",
        "Refreshing access token for entry abc, expires in 3600 s",
        "the token expired at 10:00 and the refresh token is gone",
        "A key was rotated by the user at 10:00",
        "status_code: 404, reason: Not Found",
        "http_code: 500",
        "exit_code=1 duration=3.2s",
        "Retrying (attempt 2 of 5) after ConnectionResetError",
        "Entity sensor.demo_temperature changed to 21.5 (was 21.0)",
        "Unable to authenticate: invalid credentials for user bob",
        "Setting up MQTT: broker=192.168.1.9 port=1883 keepalive=60",
        "Config entry 'Demo' for demo integration not ready yet: timeout",
        "zigbee2mqtt: device 0x00124b00 linkquality 84, battery 97",
        "author=Jane", "authority: local", "oauth=ok", "spin=3", "oauth_scope=read",
        "spinning: 5", "design=ok", "insignia: red", "assigns=3",
        "http://host:8123/api/states", "translation_key: sensor_state",
    )

    def test_every_line_comes_out_as_it_went_in(self):
        for line in self.LINES:
            with self.subTest(line=line):
                self.assertEqual(_scrub(line), line)
                self.assertEqual(diagnostics.scrub_text(line), line)


class ValueExtentCostTest(unittest.TestCase):
    """The scan that replaced the value patterns reads the text once.

    The rules run on every line a Logs page search reads, on an executor thread
    holding the diagnostics lock, so a line that is all names, all separators
    or all quotes must cost what its length costs and not its square.  Growth,
    not a stopwatch: twice the input for roughly twice the time is what
    separates a linear scan from one that re-reads what it has read, and a
    threshold tight enough to catch the square is one a busy runner fails for
    being busy."""

    FLOOR_S = 0.25

    @staticmethod
    def _time(line):
        best = None
        for _ in range(3):
            start = time.perf_counter()
            diagnostics._scrub_one_line_rules(line)
            taken = time.perf_counter() - start
            best = taken if best is None else min(best, taken)
        return best

    def assertLinear(self, make):
        small, large = self._time(make(20000)), self._time(make(40000))
        self.assertLess(large, max(3 * small, self.FLOOR_S),
                        f"{small:.3f}s on half the input, {large:.3f}s on all of it")

    def test_a_line_that_is_all_names(self):
        self.assertLinear(lambda n: "password=" * n)

    def test_a_line_that_is_all_separators_after_one_name(self):
        self.assertLinear(lambda n: "password=" + " , ; " * n)

    def test_a_line_that_is_all_quotes(self):
        self.assertLinear(lambda n: "password=" + "'x'" * n)

    def test_a_line_that_is_all_brackets(self):
        self.assertLinear(lambda n: "password=" + "([{}])" * n)

    def test_a_value_inside_a_string_that_never_closes(self):
        self.assertLinear(lambda n: 'log: "{\\"password\\": ' + "a\\" * n)

    def test_a_header_value_of_pairs(self):
        self.assertLinear(lambda n: "Cookie: " + "a=b; " * n)
