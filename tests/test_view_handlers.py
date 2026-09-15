"""Views are called the way Home Assistant calls them (handler(request, **match_info)): a handler that takes
``body`` must be wrapped by with_body, and a helper must not be (a decorator one line off broke the cutover)."""

import asyncio
import importlib
import inspect
import pkgutil
import unittest
from unittest import mock

import custom_components.integration_manager as im
from custom_components.integration_manager import http_util, parity

HANDLERS = ("get", "post", "put", "delete", "patch")
# modules allowed not to import in the test venv, with the reason; any other import error fails the test
# (a module skipped silently is a module whose views are never checked)
IMPORT_FAILURES_ALLOWED: dict[str, str] = {}


def _views(errors=None):
    for info in pkgutil.iter_modules(im.__path__):
        try:
            mod = importlib.import_module(f"{im.__name__}.{info.name}")
        except Exception as err:  # noqa: BLE001
            if info.name not in IMPORT_FAILURES_ALLOWED and errors is not None:
                errors.append(f"{info.name}: {type(err).__name__}: {err}")
            continue
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, http_util.ManagerView) and cls is not http_util.ManagerView and cls.__module__ == mod.__name__:
                yield cls


def _is_with_body(fn):
    return getattr(fn, "__wrapped__", None) is not None and "with_body" in getattr(fn, "__code__", mock.Mock(co_qualname="")).co_qualname


class HandlerWiringTest(unittest.TestCase):
    def test_every_body_handler_is_wrapped_and_no_helper_is(self):
        errors: list[str] = []
        views = list(_views(errors))
        self.assertEqual(errors, [], "modules that failed to import (add to IMPORT_FAILURES_ALLOWED with a reason if expected)")
        self.assertIn(parity.CutoverView, views)
        for cls in views:
            for name, fn in cls.__dict__.items():
                if not inspect.iscoroutinefunction(fn) and not inspect.isfunction(fn):
                    continue
                target = inspect.unwrap(fn)
                takes_body = "body" in inspect.signature(target).parameters
                with self.subTest(view=cls.__name__, method=name):
                    if name in HANDLERS and takes_body:
                        self.assertTrue(_is_with_body(fn), f"{cls.__name__}.{name} takes body but is not wrapped by with_body")
                    if name not in HANDLERS:
                        self.assertFalse(_is_with_body(fn), f"{cls.__name__}.{name} is a helper wrapped by with_body")


class FakePublisher:
    def __init__(self):
        self.config = mock.Mock(discovery_enabled=True)
        self.stats = {"connected": True, "discovery_devices": 3}
        self.async_save = mock.AsyncMock()
        self.async_reload_config = mock.AsyncMock()
        self.async_republish_all = mock.AsyncMock(return_value=7)
        self.async_clear_discovery = mock.AsyncMock(return_value=5)
        self.undiscover_done = mock.Mock()

    def build_health(self):
        return {"state": "ok"}

    def discovery_preview(self):
        return [{"components": {"a": {"default_entity_id": "sensor.zone_1"}}}]


def _view(parent=True):
    hass = mock.Mock()
    hass.async_add_executor_job = mock.AsyncMock(side_effect=lambda f, *a: f(*a))
    installer = mock.Mock(running="ramses_cc", running_tag="0.60.4", smoke={"pending": None})
    installer.settings.data = {"parent_ha_url": "http://parent", "parent_ha_token": "t"} if parent else {}
    return parity.CutoverView(hass, installer, FakePublisher())


def _call(view, action, body=None):
    request = mock.Mock()
    with mock.patch.object(http_util, "_json_object", mock.AsyncMock(return_value=body or {})), \
            mock.patch.object(parity.events, "emit"), mock.patch.object(view, "json", side_effect=lambda d, **k: d):
        return asyncio.run(view.post(request, action=action))  # exactly how aiohttp's handler factory calls it


class CutoverViewTest(unittest.TestCase):
    def test_status(self):
        res = _call(_view(), "status")
        self.assertTrue(res["ok"])
        self.assertEqual(res["running"], "ramses_cc")

    def test_enable_refuses_while_the_parent_still_holds_the_integration(self):
        view = _view()
        client = mock.Mock()
        client.commands = mock.AsyncMock(side_effect=[[{"components": ["mqtt", "ramses_cc"]}], [[{"entry_id": "x"}]],
                                                      [[{"entity_id": "sensor.zone_1", "platform": "ramses_cc"}]]])
        with mock.patch.object(parity, "_parent_client", return_value=client):
            res = _call(view, "enable")
        self.assertFalse(res["ok"])
        self.assertIn("config entry", res["error"])
        self.assertIn("sensor.zone_1", res["error"])
        view.publisher.async_republish_all.assert_not_awaited()

    def test_enable_without_parent_republishes(self):
        view = _view(parent=False)
        view.publisher.config.discovery_enabled = False
        res = _call(view, "enable")
        self.assertTrue(res["ok"])
        self.assertEqual(res["republished"], 7)

    def test_undo(self):
        view = _view()
        res = _call(view, "undo")
        self.assertTrue(res["ok"])
        self.assertEqual(res["cleared_discovery_configs"], 5)
        view.publisher.undiscover_done.assert_called_once()


if __name__ == "__main__":
    unittest.main()
