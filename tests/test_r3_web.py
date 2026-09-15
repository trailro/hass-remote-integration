"""Review round 3, web: secret masking (29), the login lockout (30), the per-port session cookie (31),
expensive GETs behind X-Requested-With (C1), release links (C2/D4), the Content-Security-Policy (C3),
multi-line log records (C4), the view-module import check (C12) and the service call form (D6)."""

import asyncio
import os
import re
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import hostguard, logs_page, manage_views, memdiag, views
from custom_components.integration_manager.diagnostics import scrub

SECRET = "SYNTHSECRET42"
IM_DIR = os.path.dirname(auth_mod.__file__)


def _read(*parts):
    with open(os.path.join(IM_DIR, *parts), encoding="utf-8") as fh:
        return fh.read()


def _request(headers=None, query=None, cookies=None, path="/", secure=False, remote="10.0.0.9"):
    return SimpleNamespace(headers=headers or {}, query=query or {}, cookies=cookies or {}, path=path, path_qs=path,
                           secure=secure, remote=remote)


# ----- 29 -----------------------------------------------------------------------------------------

class ScrubCasesTest(unittest.TestCase):
    KEYS = ("lr_s2_access_control_key", "lr_s2_authenticated_key", "irk", "ltk", "security_key", "pwd", "sessionid",
            "session_id", "mqtt_pw", "signature", "sig", "wifi_key", "api-key", "Set-Cookie")
    TEXTS = (
        f"lr_s2_access_control_key: {SECRET}", f"lr_s2_authenticated_key={SECRET}", f"irk={SECRET}", f"ltk: {SECRET}",
        f"security_key={SECRET}", f"pwd={SECRET}", f"sessionid={SECRET}", f"mqtt_pw: {SECRET}", f"'irk': '{SECRET}'",
        f"-----BEGIN PRIVATE KEY-----\nMIIEv{SECRET}QIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----",
        f"-----BEGIN RSA PRIVATE KEY-----\\nMIIEv{SECRET}\\n-----END RSA PRIVATE KEY-----",  # escaped in a repr
        f"cut tail -----BEGIN EC PRIVATE KEY-----\nMIIEv{SECRET}",
        f"GET https://x.example/y?key={SECRET}&a=1", f"GET https://x.example/y?a=1&sig={SECRET}",
        f"GET https://x.example/y?signature={SECRET}", f"GET https://x.example/y?token={SECRET}",
        f"Cookie: a={SECRET}; b=c", f"Cookie: a=b; session={SECRET}", f"Set-Cookie: hri_session={SECRET}; Path=/",
        f"mqtt://user:pa/{SECRET}@broker:1883", f"http://user:p@{SECRET}@host/path", f"redis://:{SECRET}/x@cache:6379/0",
    )

    def test_dict_keys(self):
        for name in self.KEYS:
            with self.subTest(name=name):
                self.assertEqual(scrub({name: SECRET})[name], "***")

    def test_texts(self):
        for text in self.TEXTS:
            with self.subTest(text=text):
                self.assertNotIn(SECRET, scrub(text))

    def test_what_stays_readable(self):
        self.assertEqual(scrub("mqtt://user:pa/ss@broker:1883"), "mqtt://user:***@broker:1883")
        self.assertEqual(scrub("http://user:p@ss@host/path"), "http://user:***@host/path")
        self.assertEqual(scrub("http://u:p@host/users/@me"), "http://u:***@host/users/@me")
        self.assertEqual(scrub("http://host:8123/api/states"), "http://host:8123/api/states")
        self.assertEqual(scrub({"translation_key": "t", "sort_key": 1, "keep": 2}), {"translation_key": "t", "sort_key": 1, "keep": 2})
        out = scrub("a -----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE----- b")
        self.assertEqual(out, "a -----BEGIN CERTIFICATE-----***-----END CERTIFICATE----- b")


# ----- 30 -----------------------------------------------------------------------------------------

class LockoutTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(auth_mod.events, "emit")
        self.emit = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_locked_address_stays_locked_whatever_fills_the_table(self):
        auth = auth_mod.Auth("pw")
        with mock.patch.object(auth_mod, "GLOBAL_MAX_FAILURES", 10 ** 9):
            for _ in range(auth_mod.MAX_FAILURES):
                auth.failed("victim")
            before = auth.locked_for("victim")
            for i in range(600):  # the probe of the review: more than 500 locked entries, then fresh addresses
                for _ in range(auth_mod.MAX_FAILURES):
                    auth.failed(f"10.0.{i // 256}.{i % 256}")
            for i in range(1200):
                auth.failed(f"2001:db8:{i:x}::/64")
            self.assertGreater(before, 800)
            self.assertGreater(auth.locked_for("victim"), 800)
            self.assertLessEqual(len(auth._failures), auth_mod.MAX_KEYS)

    def test_a_table_full_of_locked_addresses_treats_a_new_one_as_locked(self):
        auth = auth_mod.Auth("pw")
        with mock.patch.object(auth_mod, "GLOBAL_MAX_FAILURES", 10 ** 9), mock.patch.object(auth_mod, "MAX_KEYS", 10):
            for i in range(10):
                for _ in range(auth_mod.MAX_FAILURES):
                    auth.failed(f"10.0.0.{i}")
            auth.failed("10.0.1.1")
            self.assertNotIn("10.0.1.1", auth._failures)
            self.assertGreater(auth.locked_for("10.0.1.1"), 0)
            self.assertTrue(all(auth.locked_for(f"10.0.0.{i}") for i in range(10)))

    def test_many_addresses_hit_the_global_budget(self):
        clock = [1000.0]
        auth = auth_mod.Auth("pw")
        with mock.patch.object(auth_mod.time, "monotonic", lambda: clock[0]), self.assertLogs(auth_mod._LOGGER, "WARNING"):
            keys = {auth_mod.client_key(f"2001:db8:1:{i:x}::1") for i in range(auth_mod.GLOBAL_MAX_FAILURES)}
            self.assertEqual(len(keys), auth_mod.GLOBAL_MAX_FAILURES)  # a /48 gives a fresh /64 per attempt
            for k in keys:
                self.assertEqual(auth.locked_for(k), 0)
                auth.failed(k)
            self.assertGreater(auth.locked_for("2001:db8:1:ffff::/64"), 0)
            self.assertGreater(auth.locked_for("192.0.2.1"), 0)  # everyone, while the budget is spent
            self.assertTrue(any("many addresses" in str(c) for c in self.emit.call_args_list))
            clock[0] += auth_mod.GLOBAL_WINDOW_S + 1
            self.assertEqual(auth.locked_for("192.0.2.1"), 0)


# ----- 31 -----------------------------------------------------------------------------------------

class SessionCookieTest(unittest.TestCase):
    def test_name_carries_the_port(self):
        self.assertEqual(auth_mod.COOKIE, f"hri_session_{os.environ.get('HRI_PORT', '8087')}")
        self.assertNotEqual(auth_mod.COOKIE, auth_mod.LEGACY_COOKIE)

    def _guard(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))

        async def job(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(async_add_executor_job=job, data={}, config=SimpleNamespace(path=lambda *p: os.path.join(tmp, *p)),
                               http=SimpleNamespace(app=SimpleNamespace(middlewares=[])))
        env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD_FILE", "HRI_COOKIE_SECURE")}
        with mock.patch.dict(os.environ, {**env, "HRI_PASSWORD": "pw"}, clear=True):
            auth = asyncio.run(auth_mod.async_setup_auth(hass))
        return auth, hass.http.app.middlewares[0]

    def test_a_legacy_session_is_moved_to_the_port_name(self):
        auth, guard = self._guard()
        legacy = auth.new_session()

        async def handler(request):
            return web.Response(text="ok")

        resp = asyncio.run(guard(_request(cookies={auth_mod.LEGACY_COOKIE: legacy}), handler))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.cookies[auth_mod.COOKIE].value, legacy)
        self.assertEqual(resp.cookies[auth_mod.LEGACY_COOKIE]["max-age"], "0")
        resp = asyncio.run(guard(_request(cookies={auth_mod.COOKIE: legacy}), handler))
        self.assertNotIn(auth_mod.COOKIE, resp.cookies)
        resp = asyncio.run(guard(_request(cookies={auth_mod.LEGACY_COOKIE: "1.0.forged"}, path="/api/status"), handler))
        self.assertEqual(resp.status, 401)

    def test_login_sets_the_port_name_and_drops_the_old_one(self):
        resp = web.Response()
        auth_mod._set_session_cookie(resp, _request(), "v", 60)
        self.assertEqual(resp.cookies[auth_mod.COOKIE].value, "v")
        self.assertEqual(resp.cookies[auth_mod.COOKIE]["samesite"], "Strict")
        self.assertEqual(resp.cookies[auth_mod.LEGACY_COOKIE]["max-age"], "0")

    def test_login_script_is_reachable_without_a_session(self):
        self.assertIn("/static/login.js", auth_mod.OPEN_PATHS)


# ----- C1 -----------------------------------------------------------------------------------------

class ExpensiveGetsTest(unittest.TestCase):
    def test_refused_without_the_header(self):
        cases = [
            (memdiag.MemoryDiagView(None).get, ()),
            (memdiag.MemoryDiagView(None).get, ()),
            (manage_views.PatchesView(None, None).get, ("demo",)),
            (logs_page.LogsApiView(None).get, ()),
        ]
        queries = [{}, {"refs": "dict"}, {}, {"limit": "2000"}]
        for (fn, args), query in zip(cases, queries):
            with self.subTest(fn=fn.__qualname__, query=query):
                resp = asyncio.run(fn(_request(query=query), *args))
                self.assertEqual(resp.status, 400)
                self.assertIn(b"X-Requested-With", resp.body)

    def test_status_without_the_header_is_served_from_a_short_cache(self):
        installer = mock.Mock()
        installer.status = mock.AsyncMock(side_effect=lambda: {"n": installer.status.await_count})
        installer.hass.config.components = {"b", "a"}
        view = views.StatusView(installer)

        async def run():
            plain = await asyncio.gather(*(view.get(_request()) for _ in range(5)))
            fresh = await view.get(_request(headers={"X-Requested-With": "fetch"}))
            return plain, fresh

        plain, fresh = asyncio.run(run())
        self.assertEqual(installer.status.await_count, 2)  # five plain requests: one build; the UI's request: another
        self.assertTrue(all(r.status == 200 for r in plain))
        self.assertEqual(__import__("json").loads(fresh.body)["components"], ["a", "b"])
        with mock.patch.object(views.time, "monotonic", return_value=time.monotonic() + views.STATUS_CACHE_S + 1):
            asyncio.run(view.get(_request()))
        self.assertEqual(installer.status.await_count, 3)

    def test_the_ui_sends_the_header(self):
        static = os.path.join(IM_DIR, "static")
        found = 0
        for name in sorted(os.listdir(static)):
            if not name.endswith(".js"):
                continue
            src = _read("static", name)
            for m in re.finditer(r"fetch\((['`])/?api/(?:status|patches/\$\{[^}`]*\}|logs\?)[^'`]*\1([^;]*)", src):
                found += 1
                with self.subTest(file=name, call=m.group(0)[:60]):
                    self.assertIn("X-Requested-With", m.group(2).split(".json()")[0])
        self.assertGreaterEqual(found, 8)


# ----- C2 / D4 ------------------------------------------------------------------------------------

class ReleaseLinkTest(unittest.TestCase):
    def test_banner_links_only_http(self):
        src = _read("static", "hri.js")
        line = next(ln for ln in src.splitlines() if "const notes=rel.map" in ln)
        self.assertIn("/^https?:\\/\\//i.test(r.url||'')?r.url:'#'", line)
        self.assertNotIn('href="${esc(r.url)}"', src)


# ----- C3 -----------------------------------------------------------------------------------------

class ContentSecurityPolicyTest(unittest.TestCase):
    def test_header_on_every_answer(self):
        app = SimpleNamespace(middlewares=[])
        hostguard.install_host_guard(SimpleNamespace(http=SimpleNamespace(app=app)), SimpleNamespace(settings=SimpleNamespace(data={})))

        async def handler(request):
            return web.Response(text="<html>", content_type="text/html")

        resp = asyncio.run(app.middlewares[0](_request(headers={"Host": "10.0.0.2:8087"}), handler))
        csp = resp.headers["Content-Security-Policy"]
        for part in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "base-uri 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe-inline'; img", csp.split("script-src", 1)[1].split(";", 1)[0] + ";")

    def test_no_inline_script_or_handler_left(self):
        tdir = os.path.join(IM_DIR, "templates")
        for name in sorted(os.listdir(tdir)):
            html = _read("templates", name)
            with self.subTest(template=name):
                self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>")
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", html), "inline event handler")
                self.assertNotIn("javascript:", html)
        for name in sorted(os.listdir(os.path.join(IM_DIR, "static"))):
            if name.endswith(".js"):
                with self.subTest(script=name):
                    self.assertIsNone(re.search(r"<[a-z][^<>]*\son[a-z]+=[\"'\\]", _read("static", name)), "handler in generated markup")


# ----- C4 / D6 ------------------------------------------------------------------------------------

class PageDetailsTest(unittest.TestCase):
    def test_log_records_mark_their_continuation_lines(self):
        css = _read("static", "logs.css")
        rule = re.search(r"^\.l\{[^}]*\}", css, re.M).group(0)
        self.assertIn("border-left", rule)
        self.assertRegex(rule, r"text-indent:-\d")

    def test_required_multi_select_is_not_sent_empty_and_errors_do_not_hide_each_other(self):
        src = _read("static", "services.js")
        self.assertNotIn("vals.length||f.required", src)
        self.assertNotIn("bad.includes(", src)
        self.assertIn("badJson.has(k)", src)


# ----- C12 ----------------------------------------------------------------------------------------

class ViewModuleImportTest(unittest.TestCase):
    def test_an_import_error_is_reported(self):
        from tests import test_view_handlers as tvh

        real = tvh.importlib.import_module

        def fake(name, *a, **k):
            if name.endswith(".memdiag"):
                raise ImportError("synthetic")
            return real(name, *a, **k)

        errors: list[str] = []
        with mock.patch.object(tvh.importlib, "import_module", fake):
            list(tvh._views(errors))
        self.assertEqual(errors, ["memdiag: ImportError: synthetic"])
        with mock.patch.object(tvh.importlib, "import_module", fake), mock.patch.dict(tvh.IMPORT_FAILURES_ALLOWED, {"memdiag": "test"}):
            errors = []
            list(tvh._views(errors))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
