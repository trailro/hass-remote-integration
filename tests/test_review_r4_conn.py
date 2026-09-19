"""Three connection-bookkeeping findings of the fourth review.

R3-13: only the SUBACK callback checked which client called it, so a paho thread we had replaced - one
stuck on a TLS listener that never answers keeps its thread for minutes - could report itself
disconnected over a healthy connection.
R3-14: a refused CONNACK wrote an ERROR at every retry; paho retries for ever, so a wrong password was
about 1440 identical lines a day, while a disconnection has said "one per reason" since it was written.
R3-16: changing the MQTT user, or turning TLS on, counted as a different broker, so the identity sweep
deferred a removal to a broker nobody would ever name again.
"""

import unittest
from unittest import mock

from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r3_mqtt import _publisher


def _connected_publisher():
    """The fixture plus what the connection callbacks touch (they are paho-thread code, not the API)."""
    pub = _publisher()
    pub.stats.update(connected=False, connect_error="", subscribe_error="")
    pub._connected = pub._stopping = False
    pub._connected_at = pub._tls_checked_at = 0.0
    pub._tls_error = pub._last_disconnect = pub._last_refusal = ""
    pub._broker_max_packet = 0
    pub._session_v5 = False
    pub._last_hash = {}
    pub._subscribing = None
    pub.config.host, pub.config.port, pub.config.tls, pub.config.username = "broker.lan", 1883, False, "hri"
    return pub


class StaleClientTest(unittest.TestCase):
    def setUp(self):
        self.pub = _connected_publisher()
        self.pub._client = mock.Mock(name="current")
        self.old = mock.Mock(name="replaced")

    def test_a_replaced_client_cannot_report_a_disconnection(self):
        self.pub._connected = True
        self.pub._on_disconnect(self.old, None, None, 0)
        self.assertTrue(self.pub._connected, "the healthy connection is still up")

    def test_a_replaced_client_cannot_report_a_refusal(self):
        self.pub._connected = True
        self.pub._on_connect(self.old, None, None, 5)
        self.assertTrue(self.pub._connected)
        self.assertEqual(self.pub.stats["connect_error"], "")

    def test_a_replaced_client_cannot_report_it_failed_to_reach_the_broker(self):
        self.pub.stats["connect_error"] = ""
        self.pub._on_connect_fail(self.old, None)
        self.assertEqual(self.pub.stats["connect_error"], "")

    def test_the_current_client_is_heard(self):
        self.pub._connected = True
        self.pub._on_disconnect(self.pub._client, None, None, 0)
        self.assertFalse(self.pub._connected)

    def test_before_a_client_exists_nothing_is_stale(self):
        self.pub._client = None
        self.assertFalse(self.pub._stale_client(self.old))


class RefusalLoggingTest(unittest.TestCase):
    def setUp(self):
        self.pub = _connected_publisher()
        self.pub._client = mock.Mock()

    def refuse(self, code=5):
        with self.assertLogs(mp._LOGGER, "ERROR") as logs:
            self.pub._on_connect(self.pub._client, None, None, code)
        return logs.output

    def test_the_reason_is_logged_once_not_once_per_retry(self):
        self.assertEqual(len(self.refuse()), 1)
        with mock.patch.object(mp._LOGGER, "error") as err:
            for _ in range(20):
                self.pub._on_connect(self.pub._client, None, None, 5)
            err.assert_not_called()

    def test_a_different_reason_is_news(self):
        self.refuse(5)
        self.assertEqual(len(self.refuse(4)), 1)

    def test_the_status_still_carries_every_refusal(self):
        self.refuse(5)
        self.pub._on_connect(self.pub._client, None, None, 5)
        self.assertIn("reason_code=5", self.pub.stats["connect_error"])

    def test_a_deliberate_reconnect_makes_the_reason_news_again(self):
        self.refuse(5)
        self.pub._forget_errors()
        self.assertEqual(len(self.refuse(5)), 1)


class SameBrokerTest(unittest.TestCase):
    def setUp(self):
        self.pub = _connected_publisher()
        self.pub.config.host, self.pub.config.port = "broker.lan", 1883
        self.pub.config.username, self.pub.config.tls = "hri", False

    def last(self, **broker):
        return {"base": "hass_demo", "prefix": "homeassistant",
                "broker": {"host": "broker.lan", "port": 1883, "tls": False, "username": "hri", **broker}}

    def test_a_new_user_on_the_same_broker_is_the_same_broker(self):
        self.pub.config.username = "someone-else"
        self.assertFalse(self.pub._recorded_elsewhere(self.last()))

    def test_turning_tls_on_is_the_same_broker(self):
        self.pub.config.tls = True
        self.pub.config.port = 1883  # the same listener, now spoken to over TLS
        self.assertFalse(self.pub._recorded_elsewhere(self.last()))

    def test_another_host_is_another_broker(self):
        self.assertTrue(self.pub._recorded_elsewhere(self.last(host="10.9.9.9")))

    def test_another_port_is_another_broker(self):
        self.assertTrue(self.pub._recorded_elsewhere(self.last(port=8883)))

    def test_a_record_written_before_brokers_were_named_is_this_one(self):
        self.assertFalse(self.pub._recorded_elsewhere({"base": "hass_demo", "prefix": "homeassistant"}))
