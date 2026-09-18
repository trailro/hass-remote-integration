"""A text entity whose source state is `unknown` showed the literal word "None" on the main Home Assistant: its MQTT
text platform takes every payload as the value and has no PAYLOAD_NONE.  Checked against the consuming Home
Assistant's own platform code, together with the other platforms whose component renders the raw state into a value."""

import json
import unittest
from types import SimpleNamespace

from homeassistant.core import State

from tests.test_e2e_disc import Consumer, _HassCase, document


def availability(hass, consumer, state: State) -> bool:
    """What the consuming entity's availability is once the document of `state` has reached every availability topic
    of its component (the instance's status topic says this instance is up)."""
    from homeassistant.components.mqtt.models import DATA_MQTT, ReceiveMessage

    entity = consumer.entity
    if not hasattr(entity, "_avail_topics"):
        entity._availability_setup_from_config(consumer.config)
        entity._available = {topic: False for topic in entity._avail_topics}
        entity._available_latest = False
    hass.data[DATA_MQTT] = SimpleNamespace(client=SimpleNamespace(connected=True))
    payload = json.dumps(document(state))
    for topic in entity._avail_topics:
        message = payload if topic == consumer.doc_topic else "online"
        entity._availability_message_received(ReceiveMessage(topic, message, 0, False, topic, 0.0))
    return entity.available


def availability_template(component: dict) -> str:
    """The availability template the component puts on its own document topic."""
    return next(a["value_template"] for a in component["availability"] if "value_template" in a)


class TextWithoutAValueTest(_HassCase):
    """An `unknown` source state must not become the word "None" on the main HA, while a value that really is
    "None" (or empty) must stay what it is."""

    def text(self, state: str) -> State:
        return State("text.note", state, {"min": 0, "max": 100, "mode": "text"})

    async def test_a_value_reaches_the_main_ha(self):
        consumer = Consumer(self.hass, self.text("hello"))
        self.assertQuiet(consumer, self.text("hello"))
        self.assertEqual(consumer.entity.native_value, "hello")
        self.assertTrue(availability(self.hass, consumer, self.text("hello")))

    async def test_unknown_and_unavailable_make_the_entity_unavailable(self):
        consumer = Consumer(self.hass, self.text("hello"))
        for state in ("unknown", "unavailable"):
            with self.subTest(state=state):
                self.assertQuiet(consumer, self.text(state))
                self.assertFalse(availability(self.hass, consumer, self.text(state)))

    async def test_the_word_none_typed_by_a_user_stays_that_word(self):
        consumer = Consumer(self.hass, self.text("None"))
        self.assertQuiet(consumer, self.text("None"))
        self.assertEqual(consumer.entity.native_value, "None")
        self.assertTrue(availability(self.hass, consumer, self.text("None")))

    async def test_an_empty_text_is_still_an_empty_text(self):
        consumer = Consumer(self.hass, self.text(""))
        self.assertQuiet(consumer, self.text(""))
        self.assertEqual(consumer.entity.native_value, "")
        self.assertTrue(availability(self.hass, consumer, self.text("")))


class NoValuePayloadTest(_HassCase):
    """The other platforms whose component renders the raw source state into a value: each of them has a payload for
    "no value" and reads the 'None' this container sends as one, so only text needed the availability route."""

    CASES = {
        "select": (State("select.s", "eco", {"options": ["eco", "boost"]}), "current_option"),
        "number": (State("number.n", "5", {"min": 0, "max": 10, "step": 1}), "native_value"),
        "lock": (State("lock.l", "locked", {}), "is_locked"),
        "device_tracker": (State("device_tracker.d", "home", {"source_type": "gps"}), "location_name"),
        "lawn_mower": (State("lawn_mower.m", "mowing", {}), "activity"),
    }

    async def test_none_is_read_as_no_value(self):
        for domain, (state, attribute) in self.CASES.items():
            with self.subTest(domain=domain):
                consumer = Consumer(self.hass, state)
                self.assertQuiet(consumer, state)
                self.assertIsNotNone(getattr(consumer.entity, attribute))
                for unset in ("unknown", "unavailable"):
                    self.assertQuiet(consumer, State(state.entity_id, unset, state.attributes))
                    self.assertIsNone(getattr(consumer.entity, attribute))

    async def test_only_text_goes_unavailable_on_unknown(self):
        self.assertNotIn("unknown", availability_template(Consumer(self.hass, self.CASES["select"][0]).component))
        self.assertIn("unknown", availability_template(Consumer(self.hass, State("text.note", "hi", {})).component))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
