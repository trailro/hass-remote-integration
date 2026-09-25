"""The web UI uses only relative URLs and redirects: Home Assistant's ingress serves it under a prefix the request never
shows (the Supervisor strips it), and a <base> is ruled out by the policy's base-uri 'none'.  Every page is one level
deep, so a relative URL means the same on the app's port and under the prefix."""

import asyncio
import os
import pathlib
import re
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import ui

IM_DIR = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "integration_manager"


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    return tmp


def _env(**values):
    env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD", "HRI_PASSWORD_FILE", "HRI_COOKIE_SECURE")}
    return mock.patch.dict(os.environ, {**env, **values}, clear=True)


def _hass(tmp, app):
    async def job(fn, *args):
        return fn(*args)

    return SimpleNamespace(async_add_executor_job=job, data={}, config=SimpleNamespace(path=lambda *p: os.path.join(tmp, *p)),
                           http=SimpleNamespace(app=app))


class RelativeRedirectTest(unittest.TestCase):
    def _guard(self):
        tmp = _tmp(self)
        hass = _hass(tmp, SimpleNamespace(middlewares=[]))
        with _env(HRI_PASSWORD="pw"):
            asyncio.run(auth_mod.async_setup_auth(hass))
        return hass.http.app.middlewares[0]

    def _location(self, guard, path_qs):
        path = path_qs.split("?", 1)[0]
        req = SimpleNamespace(headers={}, query={}, cookies={}, path=path, path_qs=path_qs, secure=False, remote="10.0.0.9")
        with self.assertRaises(web.HTTPFound) as ctx:
            asyncio.run(guard(req, None))
        return ctx.exception.location

    def test_the_login_redirect_is_relative(self):
        guard = self._guard()
        self.assertEqual(self._location(guard, "/"), "login?next=")
        self.assertEqual(self._location(guard, "/config?domain=x"), "login?next=config%3Fdomain%3Dx")
        self.assertEqual(self._location(guard, "/static/app.js"), "../login?next=static%2Fapp.js")
        self.assertEqual(self._location(guard, "/a/b/"), "../../login?next=a%2Fb%2F")

    def test_the_login_page_without_a_password_goes_to_the_overview(self):
        req = SimpleNamespace(headers={}, query={}, cookies={}, path="/login", path_qs="/login")
        with self.assertRaises(web.HTTPFound) as ctx:
            asyncio.run(auth_mod.LoginPageView(auth_mod.Auth("")).get(req))
        self.assertEqual(ctx.exception.location, "./")

    def test_no_root_absolute_redirect_is_left(self):
        for path in IM_DIR.glob("*.py"):
            src = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"HTTP(?:Found|SeeOther|MovedPermanently|TemporaryRedirect|PermanentRedirect)\(\s*f?[\"']/", src))
                self.assertIsNone(re.search(r"[\"']Location[\"']\s*:\s*f?[\"']/", src))

    def test_every_page_is_one_level_deep(self):
        """The relative URLs hold because every page route is /<name>: a deeper page would need its own base."""
        routes = re.findall(r'^\s+url = "(/[^"]*)"', "\n".join(p.read_text(encoding="utf-8") for p in IM_DIR.glob("*.py")), re.M)
        pages = [r for r in routes if not r.startswith("/api/") and r != "/static/{name}"]
        self.assertIn("/", pages)
        for route in pages:
            self.assertRegex(route, r"^/[a-z_]*$")


# root-absolute URLs a page could use: under ingress each one leaves the prefix and reaches Home Assistant itself
ROOT_ABSOLUTE = [
    re.compile(r"""\b(?:href|src|action|formaction)\s*=\s*\\?["']/(?!/)"""),  # an attribute, also inside a JS string
    re.compile(r"""(?:fetch|post|del|get|fetchDownload|open|assign|replace)\((?:[^()'"`]*,)?\s*["'`]/(?!/)"""),
    re.compile(r"""\blocation(?:\.href)?\s*=\s*["'`]/(?!/)"""),
    re.compile(r"""["'`]/(?:api|static|login|config|install|mqtt|parity|entities|devices|services|logs|logfiles|system|flow)\b"""),
    re.compile(r"""url\(\s*["']?/(?!/)"""),
    re.compile(r"""<base\b""", re.I),
]


class RelativeUrlGuardTest(unittest.TestCase):
    def _check(self, name, text):
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue  # a comment in a script
            for pattern in ROOT_ABSOLUTE:
                self.assertIsNone(pattern.search(line), f"{name}:{n}: root-absolute URL ({pattern.pattern}): {line.strip()[:160]}")

    def test_templates_and_static_files(self):
        files = sorted([*(IM_DIR / "templates").glob("*.html"), *(IM_DIR / "static").glob("*.js"), *(IM_DIR / "static").glob("*.css")])
        self.assertGreater(len(files), 20)
        for path in files:
            with self.subTest(file=path.name):
                self._check(path.name, path.read_text(encoding="utf-8"))

    def test_the_rendered_pages(self):
        for route, _, page in ui.PAGES:
            html = ui.render(ui.load_template(page), route)
            with self.subTest(page=page):
                self._check(page, html)
                self.assertIn('href="static/hri.css?v=', html)
                self.assertIn('<a class="brand" href="./">', html)
        self.assertIn('href="config"', ui.topbar("/"))

    def test_the_guard_catches_what_it_is_for(self):
        for bad in ('<a href="/config">', "fetch('/api/x')", "post(`/api/x`)", "location.href='/login'", '<script src="/static/a.js">',
                    "fetchDownload(b,'/api/log_files/download?id=1')", "x.innerHTML='<a href=\"/install\">'", "url(/static/a.png)"):
            with self.subTest(bad=bad), self.assertRaises(AssertionError):
                self._check("sample", bad)
        for good in ('<a href="config">', "fetch('api/x')", "location.href='login'", "https://github.com/a/b", "a.split('/')",
                     "/^https?:\\/\\//i.test(x)", '<a href="./">', "// fetch('/api/x') in a comment"):
            with self.subTest(good=good):
                self._check("sample", good)


if __name__ == "__main__":
    unittest.main()
