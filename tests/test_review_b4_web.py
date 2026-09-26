"""Review of b4cd1a1.  S4-1: an integration's exception text came back unmasked in the answers of the device and
entity delete (and the device rename): a token URL in it was shown on the page and returned by the API."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from custom_components.integration_manager import devices_page, entities_page
from tests import test_review_fable_web as fable
from tests.test_review_fable_web import _request

TOKEN = "s3cr3tT0kenValue42"
LEAK = f"cannot reach https://cloud.invalid/api?access_token={TOKEN}"


class DeviceAnswersMaskedTest(unittest.TestCase):
    def test_a_hook_that_raises(self):
        async def boom(hass, entry, dev):
            raise RuntimeError(LEAK)

        allow = fable.DeleteDeviceOfSeveralEntriesTest._hook(True)
        body, _registry = fable.DeleteDeviceOfSeveralEntriesTest._delete(self, {"alpha": allow, "beta": boom})
        self.assertFalse(body["ok"])
        self.assertNotIn(TOKEN, body["error"])
        self.assertIn("beta: RuntimeError: cannot reach", body["error"])

    def test_a_rename_that_raises(self):
        registry = SimpleNamespace(async_get=lambda _id: SimpleNamespace(id="dev1"),
                                   async_update_device=mock.Mock(side_effect=RuntimeError(LEAK)))

        async def run():
            view = devices_page.DeviceActionView(SimpleNamespace())
            return await view.post(_request("/api/devices/dev1/name", body={"name": "x"}), device_id="dev1", action="name")

        with mock.patch.object(devices_page.dr, "async_get", return_value=registry), \
                mock.patch.object(devices_page.disc, "is_child_device", return_value=False):
            body = json.loads(asyncio.run(run()).body)
        self.assertFalse(body["ok"])
        self.assertNotIn(TOKEN, body["error"])
        self.assertIn("RuntimeError: cannot reach", body["error"])


class EntityAnswerMaskedTest(unittest.TestCase):
    def test_a_delete_that_raises(self):
        registry = SimpleNamespace(async_get=lambda _id: SimpleNamespace(entity_id="sensor.x", domain="sensor"),
                                   async_remove=mock.Mock(side_effect=RuntimeError(LEAK)))

        async def run():
            view = entities_page.EntityActionView(SimpleNamespace(), None)
            return await view.post(_request("/api/entities/sensor.x/delete", body={}), entity_id="sensor.x", action="delete")

        with mock.patch.object(entities_page.er, "async_get", return_value=registry):
            body = json.loads(asyncio.run(run()).body)
        self.assertFalse(body["ok"])
        self.assertNotIn(TOKEN, body["error"])
        self.assertIn("RuntimeError: cannot reach", body["error"])


if __name__ == "__main__":
    unittest.main()
