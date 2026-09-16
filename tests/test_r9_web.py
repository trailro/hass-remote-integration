"""Review round 9, web part: the Log files search's budget as an oracle (F2), the patch editor without the
X-Requested-With gate (F8), a host guard that failed open (F9), a patch name with a trailing newline (F22), a Host
with a trailing dot (F23), tests that took HRI_PORT away from the tests after them (T1), and the cleanups.

The page half (static/hri.js post and del, static/config.js pollProgress) is in tests/test_camp_config_js.py,
which CI runs under node without Home Assistant."""

import ast
import asyncio
import inspect
import json
import os
import random
import re
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import diagnostics, hostguard, import_views, logfiles_page, manage_views, patches, ui

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(TESTS_DIR, os.pardir, "custom_components", "integration_manager", "static")


def _tmp(test):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def _json_request(body, headers=None):
    return SimpleNamespace(headers=headers or {}, query={}, content_type="application/json",
                           json=mock.AsyncMock(return_value=body))


# ----- F2 -----------------------------------------------------------------------------------------

class LogFileSearchBudgetTest(unittest.TestCase):
    """The one-line rules ran only on lines whose raw text held the search, and the scan stopped after
    MAX_MASKED_OUT such lines that no longer matched once masked.  A search inside a password logged on every
    line reached that budget and a wrong guess did not: total_lines_scanned (20001 against 60001) and the time
    it took (0.27 s against 0.01 s) told the two apart while the rows were empty for both."""

    PROBES = (
        ("api_password=hunter2syn", "hunter2", "hunterX"),
        ("Authorization: Bearer abcDEF123456syn", "abcDEF", "abcXYZ"),
        ("Set-Cookie: session=zqVALsyn; Path=/", "zqVAL", "zqXYZ"),
    )

    def _log(self, lines):
        path = os.path.join(_tmp(self), "app.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def test_a_right_and_a_wrong_guess_scan_the_same_lines(self):
        for secret_line, right, wrong in self.PROBES:
            with self.subTest(secret=secret_line):
                path = self._log([f"2026-09-16 10:00:{i % 60:02d} DEBUG [probe] poll {i} {secret_line}" for i in range(21_000)]
                                 + ["2026-09-16 10:01:00 INFO [probe] done"])
                answers = {}
                for guess in (right, wrong):
                    with mock.patch.object(diagnostics, "_scrub_one_line_rules", wraps=diagnostics._scrub_one_line_rules) as rules:
                        rows, scanned = logfiles_page._tail_masked(path, 50, guess)
                    answers[guess] = (rows, scanned, rules.call_count)
                self.assertEqual(answers[right][0], [])
                self.assertEqual(answers[right], answers[wrong])  # rows, lines read, and the work (the time) the rules took

    def test_the_small_probe_and_its_prefixes_stay_invisible(self):
        path = self._log(["2026-09-16 10:00:00 INFO [probe] start"] + [f"2026-09-16 10:00:01 DEBUG [probe] {s}" for s, _, _ in self.PROBES]
                         + ["2026-09-16 10:00:02 INFO [probe] end"])
        for secret_line, right, wrong in self.PROBES:
            baseline = logfiles_page._tail_masked(path, 50, wrong)
            for guess in (right[:3], right[:4], right):
                with self.subTest(guess=guess):
                    rows, scanned = logfiles_page._tail_masked(path, 50, guess)
                    self.assertNotIn("***", "\n".join(rows))
                    self.assertEqual(scanned, baseline[1])

    def test_a_search_still_finds_what_the_mask_leaves(self):
        path = self._log(["2026-09-16 10:00:00 DEBUG [probe] api_password=hunter2syn", "2026-09-16 10:00:01 INFO [probe] hunter2syn in prose"])
        self.assertEqual(logfiles_page._tail_masked(path, 50, "hunter2syn")[0], ["2026-09-16 10:00:01 INFO [probe] hunter2syn in prose"])
        self.assertEqual(logfiles_page._tail_masked(path, 50, "api_password")[0], ["2026-09-16 10:00:00 DEBUG [probe] api_password=***"])

    def test_the_budget_still_bounds_a_log_with_a_token_on_every_line(self):
        path = self._log([f"2026-09-16 10:00:00 DEBUG [probe] retry {i} token=abc{i}" for i in range(80_000)])
        t0 = time.perf_counter()
        rows, scanned = logfiles_page._tail_masked(path, 50, "nothing matches this")
        self.assertEqual(rows, [])
        self.assertLessEqual(scanned, logfiles_page.MAX_MASKED_OUT + 1)
        self.assertLess(time.perf_counter() - t0, 2.0)


def _rules_run_by(fn, seen=None):
    """Every module-level name a function reads to decide what it masks - patterns, tables, the decoding it does -
    as ``module.name``, following the functions of diagnostics and logbuffer it calls or hands to another (the
    callback of a ``.sub``).  Counting only the ``.sub(`` calls of _scrub_one_line_rules missed every rule behind
    logbuffer.mask_query_secrets, the percent-decoding of a parameter name among them (R11 N2)."""
    import logbuffer

    ours = {diagnostics.__name__, logbuffer.__name__}
    seen = set() if seen is None else seen
    found = set()
    scope = fn.__globals__
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and inspect.ismodule(scope.get(node.value.id)):
            owner, name = scope[node.value.id], node.attr
        elif isinstance(node, ast.Name) and node.id in scope and not inspect.ismodule(scope[node.id]):
            owner, name = sys.modules[fn.__module__], node.id
        else:
            continue
        obj = getattr(owner, name, None)
        if inspect.isclass(obj):
            continue  # an annotation
        label = f"{owner.__name__}.{name}"
        found.add(label)
        if inspect.isfunction(obj) and obj.__module__ in ours and label not in seen:
            seen.add(label)
            found |= _rules_run_by(obj, seen)
    return found


class LogFileSearchPrefilterTest(unittest.TestCase):
    """The literals the search checks before it runs the one-line rules: a line the rules change must never be
    skipped, or the search would decide on its raw text again."""

    def test_the_rules_are_the_ones_the_literals_were_written_for(self):
        self.assertEqual(sorted(_rules_run_by(diagnostics._scrub_one_line_rules)), [
            "custom_components.integration_manager.diagnostics._AUTH_TEXT",
            "custom_components.integration_manager.diagnostics._BEARER",
            "custom_components.integration_manager.diagnostics._COOKIE_TEXT",
            "custom_components.integration_manager.diagnostics._GH_TOKEN",
            "custom_components.integration_manager.diagnostics._SECRET_TEXT",
            "custom_components.integration_manager.diagnostics._SECRET_TEXT_HINT",
            "custom_components.integration_manager.diagnostics._URL_CRED",
            "logbuffer._CREDENTIAL_PARAM",
            "logbuffer._LOG_SEARCH_PATHS",
            "logbuffer._LOG_SEARCH_PLAIN",
            "logbuffer._PLAIN_PARAM",
            "logbuffer._URL_ORIGIN",
            "logbuffer._URL_QUERY",
            "logbuffer._is_log_search",
            "logbuffer._mask_query",
            "logbuffer.mask_query_secrets",
            "logbuffer.unquote_plus",
            "re.fullmatch",
        ], "a rule of the one-line rules changed: logfiles_page._RULE_LITERALS must find every line it changes, as "
           "written and percent-decoded (extend the corpus in tests/test_r11_logs.py with its cases)")

    def test_every_line_the_rules_change_passes_the_prefilter(self):
        from tests.test_r3_web import ScrubCasesTest

        corpus = [line for text in ScrubCasesTest.TEXTS for line in text.split("\n")] + [
            "password: x1", "PASSWD=x1", "passphrase=x1", "token=x1", "secret:x1", "credential=x1", "psk=x1", "hmac=x1",
            "passkey=x1", "bindkey=x1", "webhook_id=x1", "cloudhook_url=x1", "pin_code=x1", "signature=x1", "pin=1234",
            "code=1234", "otp=1234", "pwd=x1", "db_pw=x1", "session_id=x1", "sessionid=x1", "irk=x1", "ltk=x1", "csrk=x1",
            "sig=x1", "network_key=x1", "wifikey=x1", "Cookie: a=b", "set-cookie: a=b", "Authorization: Digest x",
            "Bearer abcdefgh123", "Basic dXNlcjpwYXNz", "mqtt://user:pw@broker.local", "ghp_" + "a" * 30,
            "github_pat_" + "a" * 30, "PAſſWORD=x1", "Key=x1", "SıG=x1", "ToKeN = 'x1'", '"password": "x1"',
        ]
        rng = random.Random(9)
        parts = ["pass", "word", "tok", "en", "key", "_", "-", "=", ":", " ", "'", '"', "Bearer ", "Basic ", "://", "@", "gh",
                 "p_", "sig", "pin", "code", "otp", "Cookie", "auth", "orization", "session", "id", "x9Y", "ſ", "ı", "K"]
        corpus += ["".join(rng.choice(parts) for _ in range(rng.randint(2, 12))) for _ in range(20_000)]
        missed = [line for line in corpus if diagnostics._scrub_one_line_rules(line) != line and not logfiles_page._rules_may_change(line)]
        self.assertEqual(missed, [])

    def test_every_character_ignorecase_matches_is_folded(self):
        """The rules are case-insensitive: a literal spelled with a character re takes for its letter (the Kelvin
        sign for k, the long s, the dotless i) must still be found."""
        any_letter = re.compile("[a-z]", re.I)
        missed = []
        for cp in range(128, sys.maxunicode + 1):
            c = chr(cp)
            if not any_letter.fullmatch(c):
                continue
            for letter in "abcdefghijklmnopqrstuvwxyz":
                if not re.fullmatch(letter, c, re.I):
                    continue
                for literal in logfiles_page._RULE_LITERALS:
                    if letter in literal and not logfiles_page._rules_may_change(literal.replace(letter, c)):
                        missed.append((literal, hex(cp)))
        self.assertEqual(missed, [])


# ----- F8 -----------------------------------------------------------------------------------------

class PatchEditorGateTest(unittest.TestCase):
    """POST /api/patch_editor/<domain>/check ran the submitted module for any request with a JSON body."""

    PATCH = "def apply(ctx):\n    return 'applied'\ndef status(ctx):\n    return 'pending'\n"

    def setUp(self):
        cfg = _tmp(self)

        async def job(fn, *args):
            return fn(*args)

        self.view = manage_views.PatchEditView(SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=job),
                                               SimpleNamespace(running=None, running_tag=None, site_packages_for=lambda d: cfg,
                                                               component_dir=lambda d: cfg))

    def test_check_and_save_are_refused_without_the_header(self):
        for op in ("check", "save"):
            with self.subTest(op=op), mock.patch.object(patches, "check", return_value={"ok": True}) as check:
                resp = asyncio.run(self.view.post(_json_request({"name": "p.py", "text": self.PATCH}), domain="demo", op=op))
                self.assertEqual(resp.status, 400)
                self.assertIn(b"X-Requested-With", resp.body)
                check.assert_not_called()

    def test_the_page_request_still_runs_the_check(self):
        with mock.patch.object(patches, "check", return_value={"ok": True}) as check:
            resp = asyncio.run(self.view.post(_json_request({"name": "p.py", "text": self.PATCH}, {"X-Requested-With": "fetch"}),
                                              domain="demo", op="check"))
        self.assertEqual(json.loads(resp.body), {"ok": True})
        check.assert_called_once()

    def test_the_shared_post_helper_sends_the_header(self):
        with open(os.path.join(STATIC, "hri.js"), encoding="utf-8") as fh:
            src = fh.read()
        helper = next(line for line in src.splitlines() if line.startswith("async function post("))
        self.assertIn("'X-Requested-With':'fetch'", helper)


# ----- F9 -----------------------------------------------------------------------------------------

class HostGuardFailsClosedTest(unittest.TestCase):
    """A frozen app was logged and the manager served every page without the guard, while auth.py re-raised."""

    def test_a_guard_that_cannot_be_installed_stops_the_setup(self):
        class Frozen(list):
            def append(self, item):
                raise RuntimeError("Cannot modify frozen list.")

        hass = SimpleNamespace(http=SimpleNamespace(app=SimpleNamespace(middlewares=Frozen())))
        with self.assertLogs(hostguard._LOGGER, "ERROR"), self.assertRaises(RuntimeError):
            hostguard.install_host_guard(hass, SimpleNamespace(settings=SimpleNamespace(data={})))


# ----- F22 ----------------------------------------------------------------------------------------

class PatchNameNewlineTest(unittest.TestCase):
    def test_a_trailing_newline_is_not_a_patch_name(self):
        for name in ("fix.py\n", "fix.patch\n"):
            with self.subTest(name=name):
                self.assertFalse(patches.valid_name(name))
                self.assertIsNotNone(patches.validate(name, "def apply(ctx):\n    pass\ndef status(ctx):\n    pass\n"))
        self.assertTrue(patches.valid_name("fix.py"))

    def test_the_patch_editor_does_not_read_it(self):
        cfg = _tmp(self)
        os.makedirs(patches.patch_dir(cfg, "demo"))
        with open(os.path.join(patches.patch_dir(cfg, "demo"), "fix.py\n"), "w", encoding="utf-8") as fh:
            fh.write("def apply(ctx):\n    pass\ndef status(ctx):\n    pass\n")

        async def job(fn, *args):
            return fn(*args)

        view = manage_views.PatchReadView(SimpleNamespace(config=SimpleNamespace(config_dir=cfg), async_add_executor_job=job))
        request = SimpleNamespace(headers={"X-Requested-With": "fetch"}, query={"name": "fix.py\n"})
        self.assertEqual(json.loads(asyncio.run(view.get(request, "demo")).body), {"ok": False, "error": "bad domain or patch name"})


# ----- F23 ----------------------------------------------------------------------------------------

class HostTrailingDotTest(unittest.TestCase):
    def test_a_fully_qualified_name_is_the_same_host(self):
        for host in ("hri.local.", "hri.local.:8087", "localhost.", "10.0.0.2.:8087", "hri.home.arpa."):
            with self.subTest(host=host):
                self.assertTrue(hostguard._host_ok(host, set()))
        self.assertTrue(hostguard._host_ok("hri.example.com.:443", hostguard._allowed("hri.example.com")))
        self.assertTrue(hostguard._host_ok("hri.example.com", hostguard._allowed("hri.example.com.:443")))

    def test_only_one_dot_and_never_a_public_name(self):
        for host in ("evil.example.", "hri.local..", ".", ":8087", "evil.example.:8087"):
            with self.subTest(host=host):
                self.assertFalse(hostguard._host_ok(host, set()))


class HostGuardPerRequestWorkTest(unittest.TestCase):
    """socket.gethostname() and the allowed_hosts setting ran and were parsed on the event loop for every request."""

    def test_hostname_and_setting_are_computed_once(self):
        hostguard._own_hostname.cache_clear()
        self.addCleanup(hostguard._own_hostname.cache_clear)
        app = SimpleNamespace(middlewares=[])
        settings = {"allowed_hosts": "hri.example.com:443, other.example"}
        hostguard.install_host_guard(SimpleNamespace(http=SimpleNamespace(app=app)), SimpleNamespace(settings=SimpleNamespace(data=settings)))

        async def handler(request):
            return web.Response(text="ok")

        async def run():
            return [await app.middlewares[0](SimpleNamespace(headers={"Host": host}, path="/"), handler)
                    for host in ("hri.example.com", "other.example:8087", "box", "evil.example")]

        with mock.patch.object(hostguard.socket, "gethostname", return_value="box") as hostname, \
                mock.patch.object(hostguard, "_bare", wraps=hostguard._bare) as bare:
            hostguard._allowed.cache_clear()
            statuses = [r.status for r in asyncio.run(run())]
        self.assertEqual(statuses, [200, 200, 200, 403])
        self.assertEqual(hostname.call_count, 1)
        self.assertEqual(bare.call_count, 2 + 4)  # the two names of the setting once, then one Host per request
        settings["allowed_hosts"] = "new.example"  # a save: the new value is what counts
        self.assertEqual(asyncio.run(app.middlewares[0](SimpleNamespace(headers={"Host": "new.example"}, path="/"), handler)).status, 200)


# ----- T1 -----------------------------------------------------------------------------------------

class TestsKeepTheEnvironmentTest(unittest.TestCase):
    """tests/test_camp_amd64.py set HRI_PORT and its cleanup popped it: with the container on another port than
    8087, test_r3_web compared auth.COOKIE (computed from the real port at import) against the default."""

    MUTATORS = {"update", "pop", "popitem", "setdefault", "clear", "__setitem__", "__delitem__"}

    @staticmethod
    def _is_environ(node):
        return isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"

    def _patched(self, node):
        if not isinstance(node, ast.With):
            return False
        for item in node.items:
            call = item.context_expr
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "dict"
                    and call.args and self._is_environ(call.args[0])):
                return True
        return False

    def _leaks(self, tree):
        out = []

        def walk(node, inside):
            inside = inside or self._patched(node)
            if not inside:
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
                    targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                    out.extend(node.lineno for t in targets if isinstance(t, ast.Subscript) and self._is_environ(t.value))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and ((node.func.attr in self.MUTATORS and self._is_environ(node.func.value))
                             or (node.func.attr in ("putenv", "unsetenv") and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"))):
                    out.append(node.lineno)
            for child in ast.iter_child_nodes(node):
                walk(child, inside)

        walk(tree, False)
        return out

    def test_no_test_changes_os_environ_outside_a_patch(self):
        leaks = {}
        for name in sorted(os.listdir(TESTS_DIR)):
            if name.endswith(".py"):
                with open(os.path.join(TESTS_DIR, name), encoding="utf-8") as fh:
                    if lines := self._leaks(ast.parse(fh.read())):
                        leaks[name] = lines
        self.assertEqual(leaks, {}, "use mock.patch.dict(os.environ, ...) (tests.fakes.entrypoint_for for entrypoint)")

    def test_the_install_page_tests_leave_the_port_and_the_module_as_they_found_them(self):
        from tests import test_camp_amd64

        before = sys.modules.get("entrypoint")
        with mock.patch.dict(os.environ, {"HRI_PORT": "8204", "HRI_CONFIG": "/config-of-this-container"}):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(test_camp_amd64.StatusPageIsNotHealthyTest).run(result)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            self.assertEqual((os.environ.get("HRI_PORT"), os.environ.get("HRI_CONFIG")), ("8204", "/config-of-this-container"))
        self.assertIs(sys.modules.get("entrypoint"), before)


# ----- cleanups -----------------------------------------------------------------------------------

class PatchStatusCacheTest(unittest.TestCase):
    """The status(ctx) cache of a .py patch ignored what pip changed in site-packages."""

    def test_a_package_installed_after_the_status_runs_it_again(self):
        cfg, site, comp = _tmp(self), _tmp(self), _tmp(self)
        calls = os.path.join(cfg, "calls")
        os.makedirs(patches.patch_dir(cfg, "r9demo"))
        with open(os.path.join(patches.patch_dir(cfg, "r9demo"), "p.py"), "w", encoding="utf-8") as fh:
            fh.write(f"def apply(ctx):\n    return 'applied'\ndef status(ctx):\n    open({calls!r}, 'a').write('x')\n    return 'pending'\n")
        patches._PY_STATUS.clear()
        self.addCleanup(patches._PY_STATUS.clear)

        def count():
            patches.status(cfg, "r9demo", site, comp, "1.0")
            with open(calls, encoding="utf-8") as fh:
                return len(fh.read())

        self.assertEqual((count(), count()), (1, 1))  # served from the cache
        os.makedirs(os.path.join(site, "somelib-2.0.dist-info"))  # what pip leaves in site-packages
        future = time.time() + 5
        os.utime(site, (future, future))
        self.assertEqual(count(), 2)


class LegacyCookieMaxAgeTest(unittest.TestCase):
    def test_a_long_request_does_not_turn_the_moved_cookie_into_a_deletion(self):
        tmp = _tmp(self)

        async def job(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(async_add_executor_job=job, data={}, config=SimpleNamespace(path=lambda *p: os.path.join(tmp, *p)),
                               http=SimpleNamespace(app=SimpleNamespace(middlewares=[])))
        env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD_FILE", "HRI_COOKIE_SECURE")}
        with mock.patch.dict(os.environ, {**env, "HRI_PASSWORD": "pw"}, clear=True):
            auth = asyncio.run(auth_mod.async_setup_auth(hass))
        guard = hass.http.app.middlewares[0]
        legacy = auth.new_session()
        real = time.time()

        async def slow_handler(request):
            clock.return_value = real + auth_mod.SESSION_S + 5  # the session ran out while this request was handled
            return web.Response(text="ok")

        request = SimpleNamespace(headers={}, query={}, cookies={auth_mod.LEGACY_COOKIE: legacy}, path="/", path_qs="/", secure=False, remote="10.0.0.9")
        with mock.patch.object(auth_mod.time, "time", return_value=real) as clock:
            resp = asyncio.run(guard(request, slow_handler))
        self.assertGreater(int(resp.cookies[auth_mod.COOKIE]["max-age"]), 0)


class ImportUploadHeaderBeforeLockTest(unittest.TestCase):
    def test_a_request_without_the_header_is_refused_for_the_header(self):
        view = import_views.ImportUploadView(SimpleNamespace(config=SimpleNamespace(config_dir=_tmp(self))))

        async def run():
            async with import_views._IMPORT_LOCK:  # an upload in progress
                return await view.post(SimpleNamespace(headers={}))

        resp = asyncio.run(run())
        self.assertEqual(resp.status, 400)
        self.assertIn(b"X-Requested-With", resp.body)

    def test_it_does_not_take_the_lock(self):
        view = import_views.ImportUploadView(SimpleNamespace(config=SimpleNamespace(config_dir=_tmp(self))))
        with mock.patch.object(import_views, "_IMPORT_LOCK") as lock:
            lock.locked.return_value = False
            asyncio.run(view.post(SimpleNamespace(headers={})))
        lock.__aenter__.assert_not_called()


class RenderKeepsTheHtmlModuleTest(unittest.TestCase):
    def test_the_page_argument_does_not_shadow_the_module(self):
        self.assertNotIn("html", inspect.signature(ui.render).parameters)
        out = ui.render("<head><!--css--></head><body><!--js-->", "/logs")
        self.assertIn('/static/logs.js?v=', out)
        self.assertIn('<nav class="topbar">', out)


class ParentUrlRuleTest(unittest.TestCase):
    """The (:\\d+)? after a host part that already takes a port never matched anything.  A cleanup, not a fix:
    this passes before and after, and is here to show the rule did not change."""

    def test_accepted_and_refused(self):
        for url, ok in (("http://ha.local:8123", True), ("https://10.0.0.2", True), ("http://ha.local:8123/", True),
                        ("http://[fd00::2]:8123", True), ("http://ha.local:8123/api", False), ("ftp://ha.local", False),
                        ("http://ha local:8123", False), ("http://", False)):
            with self.subTest(url=url):
                settings = SimpleNamespace(data={}, async_save=mock.AsyncMock(), public=dict)
                view = manage_views.SettingsView(SimpleNamespace(settings=settings, _releases_cache={}))
                resp = json.loads(asyncio.run(view.post(_json_request({"parent_ha_url": url}))).body)
                self.assertEqual(resp["ok"], ok, resp)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
