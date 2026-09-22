"""External review, web: a session key that cannot be written (WEB-1), deleting a device of several
config entries (WEB-2), and the cross-site gate of every mutating view."""

import asyncio
import errno
import importlib
import pkgutil
import re
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

from custom_components.integration_manager import auth as auth_mod
from custom_components.integration_manager import devices_page


def _tmp(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    return tmp


def _request(path="/api/status", cookies=None, body=None):
    async def json():
        return body

    return SimpleNamespace(headers={}, query={}, cookies=cookies or {}, path=path, path_qs=path, secure=False,
                           remote="10.0.0.9", content_type="application/json", json=json)


class KeyWriteFailsTest(unittest.TestCase):
    """HRI_PASSWORD set, no auth_key yet (first boot, or a restore: the key is not in backups) and a full
    volume: the manager comes up with a key held in memory, and the login stays exactly as closed."""

    def _setup(self, fail_with=errno.ENOSPC):
        tmp = _tmp(self)
        real_open = os.open

        def full_disk(path, *args, **kwargs):
            if str(path).endswith("auth_key.tmp"):
                raise OSError(fail_with, os.strerror(fail_with), path)
            return real_open(path, *args, **kwargs)

        async def job(fn, *args):
            return fn(*args)

        hass = SimpleNamespace(async_add_executor_job=job, data={}, config=SimpleNamespace(path=lambda *p: os.path.join(tmp, *p)),
                               http=SimpleNamespace(app=SimpleNamespace(middlewares=[])))
        env = {k: v for k, v in os.environ.items() if k not in ("HRI_PASSWORD_FILE", "HRI_COOKIE_SECURE")}
        with mock.patch.dict(os.environ, {**env, "HRI_PASSWORD": "pw"}, clear=True), \
                mock.patch.object(auth_mod.os, "open", side_effect=full_disk), \
                mock.patch.object(auth_mod.events, "emit") as emit:
            auth = asyncio.run(auth_mod.async_setup_auth(hass))  # raised OSError before the fix
        return tmp, auth, hass.http.app.middlewares[0], emit

    def test_setup_succeeds_and_says_why(self):
        tmp, auth, _guard, emit = self._setup()
        self.assertTrue(auth.enabled)
        self.assertEqual(len(auth._key), 32)  # a fresh random key: never empty, never a constant
        self.assertFalse(os.path.exists(os.path.join(tmp, "integration_manager", "auth_key")))
        self.assertFalse(os.path.exists(os.path.join(tmp, "integration_manager", "auth_key.tmp")))
        self.assertTrue(any(c.args[0] == "auth" and "auth_key" in c.args[1] for c in emit.call_args_list), emit.call_args_list)

    def test_two_failed_boots_do_not_share_a_key(self):
        self.assertNotEqual(self._setup()[1]._key, self._setup()[1]._key)

    def test_login_stays_closed(self):
        _tmp_dir, auth, guard, _emit = self._setup(errno.EROFS)
        login = auth_mod.LoginView(auth)

        async def handler(request):
            return web.Response(text="ok")

        async def run():
            with mock.patch.object(auth_mod.asyncio, "sleep", mock.AsyncMock()):
                wrong = await login.post(_request("/api/login", body={"password": "nope"}))
                right = await login.post(_request("/api/login", body={"password": "pw"}))
            cookie = right.cookies[auth_mod.COOKIE].value
            with_cookie = await guard(_request(cookies={auth_mod.COOKIE: cookie}), handler)
            without = await guard(_request(), handler)
            forged = await guard(_request(cookies={auth_mod.COOKIE: cookie[:-1] + ("0" if cookie[-1] != "0" else "1")}), handler)
            return wrong, right, with_cookie, without, forged

        wrong, right, with_cookie, without, forged = asyncio.run(run())
        self.assertEqual(wrong.status, 401)
        self.assertNotIn(auth_mod.COOKIE, wrong.cookies)
        self.assertEqual(right.status, 200)
        self.assertEqual(with_cookie.status, 200)
        self.assertEqual(without.status, 401)
        self.assertEqual(forged.status, 401)

    def test_a_readable_key_is_still_used(self):
        path = os.path.join(_tmp(self), "integration_manager", "auth_key")
        first, err = auth_mod._load_key(path)
        self.assertIsNone(err)
        self.assertEqual(auth_mod._load_key(path), (first, None))

    def test_a_write_cut_short_leaves_no_partial_file(self):
        path = os.path.join(_tmp(self), "integration_manager", "auth_key")
        real_fdopen = os.fdopen

        class Full:
            def __init__(self, fd, *args):
                self.fh = real_fdopen(fd, *args)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.fh.close()

            def write(self, data):
                self.fh.write(data[:5])
                self.fh.flush()
                raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(auth_mod.os, "fdopen", Full):
            key, err = auth_mod._load_key(path)
        self.assertEqual(err.errno, errno.ENOSPC)
        self.assertEqual(len(key), 32)
        self.assertEqual(os.listdir(os.path.dirname(path)), [])


class _Registry:
    """A registry before HA 2026.9: one device, several config entries."""

    def __init__(self, entries):
        self.dev = SimpleNamespace(id="dev1", config_entries=list(entries))  # ordered: which entry is asked first is fixed
        self.calls = []

    def async_get(self, device_id):
        return self.dev if device_id == "dev1" and self.dev is not None else None

    def async_update_device(self, device_id, remove_config_entry_id=None):
        self.calls.append(("detach", remove_config_entry_id))
        self.dev.config_entries.remove(remove_config_entry_id)
        if not self.dev.config_entries:
            self.dev = None

    def async_remove_device(self, device_id):
        self.calls.append(("remove", device_id))
        self.dev = None


class DeleteDeviceOfSeveralEntriesTest(unittest.TestCase):
    def _delete(self, hooks, entries=("ea", "eb")):
        """hooks: domain -> None (no removal hook) or an async hook."""
        registry = _Registry(entries)
        domains = {"ea": "alpha", "eb": "beta"}
        config_entries = SimpleNamespace(async_get_entry=lambda eid: SimpleNamespace(entry_id=eid, domain=domains[eid]))
        hass = SimpleNamespace(config_entries=config_entries)

        async def get_integration(_hass, domain):
            component = SimpleNamespace() if hooks[domain] is None else SimpleNamespace(async_remove_config_entry_device=hooks[domain])

            async def get_component():
                return component

            return SimpleNamespace(async_get_component=get_component)

        async def run():
            view = devices_page.DeviceActionView(hass)
            return await view.post(_request("/api/devices/dev1/delete", body={}), device_id="dev1", action="delete")

        with mock.patch.object(devices_page.dr, "async_get", return_value=registry), \
                mock.patch.object(devices_page.loader, "async_get_integration", get_integration), \
                mock.patch.object(devices_page.disc, "is_child_device", return_value=False):
            resp = asyncio.run(run())
        import json
        return json.loads(resp.body), registry

    @staticmethod
    def _hook(answer, seen=None):
        async def hook(hass, entry, dev):
            if seen is not None:
                seen.append(entry.domain)
            return answer
        return hook

    def test_an_entry_without_the_hook_is_found_before_anything_changes(self):
        for order in (("ea", "eb"), ("eb", "ea")):
            seen = []
            with self.subTest(order=order):
                body, registry = self._delete({"alpha": self._hook(True, seen), "beta": None}, order)
                self.assertFalse(body["ok"])
                self.assertIn("beta", body["error"])
                self.assertEqual(registry.calls, [])  # alpha was detached before the fix
                self.assertEqual(seen, [])  # and its hook was not even asked
                self.assertEqual(sorted(registry.dev.config_entries), ["ea", "eb"])

    def test_a_refusal_after_a_detach_says_what_was_detached(self):
        body, registry = self._delete({"alpha": self._hook(True), "beta": self._hook(False)})
        self.assertFalse(body["ok"])
        self.assertEqual(registry.calls, [("detach", "ea")])
        self.assertIn("beta refused", body["error"])
        self.assertIn("alpha", body["error"])  # the detach already made is reported
        self.assertEqual(body["config_entries_detached"], 1)

    def test_a_hook_that_raises_after_a_detach_says_what_was_detached(self):
        async def boom(hass, entry, dev):
            raise RuntimeError("cloud down")

        body, registry = self._delete({"alpha": self._hook(True), "beta": boom})
        self.assertFalse(body["ok"])
        self.assertIn("cloud down", body["error"])
        self.assertIn("alpha", body["error"])
        self.assertEqual(body["config_entries_detached"], 1)

    def test_every_entry_allows_it(self):
        body, registry = self._delete({"alpha": self._hook(True), "beta": self._hook(True)})
        self.assertEqual(body, {"ok": True, "config_entries_detached": 2})
        self.assertIsNone(registry.dev)


class EveryMutatingViewRefusesASimpleRequestTest(unittest.TestCase):
    """What stops a cross-site page from driving the API is that a browser sends a POST it may send without
    asking (a "simple request": one of these content types, no custom header) only after a CORS preflight,
    which nobody answers.  Every mutating handler must therefore refuse such a request before it does
    anything: through with_body, its own content-type check, or X-Requested-With for a multipart upload.
    The handlers are found by walking every ManagerView subclass, so a new view that forgets the gate fails
    here.  The views are built without __init__: a handler that gets past its gate touches an attribute it
    does not have, and that fails the test too."""

    SIMPLE_TYPES = ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data")

    @staticmethod
    def _views():
        import custom_components.integration_manager as pkg
        from custom_components.integration_manager.http_util import ManagerView

        for mod in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(f"{pkg.__name__}.{mod.name}")
        seen, todo = set(), [ManagerView]
        while todo:
            for sub in todo.pop().__subclasses__():
                if sub not in seen:
                    seen.add(sub)
                    todo.append(sub)
        return sorted(seen, key=lambda c: (c.__module__, c.__name__))

    def test_each_one(self):
        checked = 0
        for cls in self._views():
            for method in ("post", "put", "delete", "patch"):
                if method not in cls.__dict__:
                    continue
                params = {name: "x" for name in re.findall(r"{(\w+)", cls.url)}
                for ctype in self.SIMPLE_TYPES:
                    async def boom(*a, **k):
                        raise AssertionError("body read before the gate")

                    request = SimpleNamespace(content_type=ctype, headers={"Content-Type": ctype}, query={}, cookies={},
                                              path=cls.url, path_qs=cls.url, remote="10.0.0.9", secure=False,
                                              json=boom, text=boom, read=boom, post=boom, multipart=boom,
                                              match_info=params, app={})
                    with self.subTest(view=f"{cls.__module__.rsplit('.', 1)[1]}.{cls.__name__}.{method}", ctype=ctype):
                        view = cls.__new__(cls)
                        resp = asyncio.run(getattr(view, method)(request, **params))
                        # refused and said why: 400, or (MqttConfigView, which answers every failure so) ok:false
                        self.assertTrue(resp.status == 400 or b"Content-Type must be application/json" in resp.body, resp.body)
                checked += 1
        self.assertGreater(checked, 40)  # the walk found the views (44 when this was written)


if __name__ == "__main__":
    unittest.main()
