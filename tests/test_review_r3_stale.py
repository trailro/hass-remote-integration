"""The documents of entities a new version dropped were never cleared, by any caller.

R3-11's wider half: `async_after_start` reconnects (connect_async + loop_start), and `_connected` only
becomes True in paho's `_on_connect` callback, on its own thread.  `async_clear_stale_docs` read the flag
immediately and answered 0 - so a version switch from the UI, from an MQTT manager action, from the
environment builder and from the boot hand-over all left the old documents on the main Home Assistant.
"""

import asyncio
import unittest
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r3_mqtt import _publisher


class ClearsAfterTheConnackTest(unittest.IsolatedAsyncioTestCase):
    def publisher(self):
        pub = _publisher()
        pub._connected = False
        pub.async_republish_all = mock.AsyncMock(return_value=None)
        pub._clear_stale_docs = mock.Mock(return_value=7)
        pub.hass.async_add_executor_job = mock.AsyncMock(side_effect=lambda f, *a: f(*a))
        return pub

    async def test_it_waits_for_the_connection_the_reconnect_is_still_making(self):
        pub = self.publisher()

        async def connack():
            await asyncio.sleep(0.1)  # paho's thread, a moment after connect_async returned
            pub._connected = True

        task = asyncio.ensure_future(connack())
        self.assertEqual(await pub.async_clear_stale_docs(), 7)
        await task

    async def test_a_broker_that_never_answers_still_gives_up(self):
        pub = self.publisher()
        with mock.patch.object(mp.MqttPublisher, "CONNECT_GRACE_S", 0.2):
            with self.assertLogs(mp._LOGGER, "WARNING"):
                self.assertEqual(await pub.async_clear_stale_docs(), 0)
        pub._clear_stale_docs.assert_not_called()

    async def test_a_connection_that_is_already_up_does_not_wait(self):
        pub = self.publisher()
        pub._connected = True
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.assertEqual(await pub.async_clear_stale_docs(), 7)
        self.assertLess(loop.time() - started, 0.05)
