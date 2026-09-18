"""A source cover that can open and close its tilt but cannot set a tilt position got tilt open/close as position 100
and 0: the main Home Assistant's MQTT cover sends tilt_opened_value / tilt_closed_value to the tilt command topic, and
that is what such a cover refuses.  The commands are what the consuming HA's own cover entity publishes."""

import unittest

from homeassistant.core import State

from custom_components.integration_manager import discovery as disc
from tests.test_e2e_disc import Consumer, _HassCase


class TiltWithoutAPositionTest(_HassCase):
    SERVICES = ("cover",)
    TILT_ONLY = 16 | 32 | 64  # OPEN_TILT, CLOSE_TILT, STOP_TILT; no SET_TILT_POSITION
    FULL_TILT = 16 | 32 | 64 | 128

    def cover(self, features: int) -> State:
        return State("cover.slats", "closed", {"current_tilt_position": 0, "supported_features": features})

    async def test_open_and_close_tilt_become_the_tilt_services(self):
        consumer = Consumer(self.hass, self.cover(self.TILT_ONLY))
        self.assertQuiet(consumer, self.cover(self.TILT_ONLY))
        for action in ("async_open_cover_tilt", "async_close_cover_tilt", "async_stop_cover_tilt"):
            await getattr(consumer.entity, action)()
        self.assertEqual(await self.run_here(consumer), [
            ("cover", "open_cover_tilt", {"entity_id": "cover.slats"}),
            ("cover", "close_cover_tilt", {"entity_id": "cover.slats"}),
            ("cover", "stop_cover_tilt", {"entity_id": "cover.slats"})])

    async def test_the_boundary_positions_become_the_tilt_services_too(self):
        consumer = Consumer(self.hass, self.cover(self.TILT_ONLY))
        for position in (100, 0):
            await consumer.entity.async_set_cover_tilt_position(tilt_position=position)
        self.assertEqual([service for _, service, _ in await self.run_here(consumer)],
                         ["open_cover_tilt", "close_cover_tilt"])  # the only tilt this cover has

    async def test_a_position_in_between_is_still_a_position(self):
        consumer = Consumer(self.hass, self.cover(self.TILT_ONLY))
        await consumer.entity.async_set_cover_tilt_position(tilt_position=40)
        self.assertEqual(await self.run_here(consumer), [
            ("cover", "set_cover_tilt_position", {"entity_id": "cover.slats", "tilt_position": 40})])

    async def test_a_cover_that_can_set_a_tilt_position_keeps_sending_positions(self):
        consumer = Consumer(self.hass, self.cover(self.FULL_TILT))
        self.assertNotIn("tilt_command_template", consumer.component)
        for action in ("async_open_cover_tilt", "async_close_cover_tilt"):
            await getattr(consumer.entity, action)()
        await consumer.entity.async_set_cover_tilt_position(tilt_position=40)
        self.assertEqual(await self.run_here(consumer), [
            ("cover", "set_cover_tilt_position", {"entity_id": "cover.slats", "tilt_position": 100}),
            ("cover", "set_cover_tilt_position", {"entity_id": "cover.slats", "tilt_position": 0}),
            ("cover", "set_cover_tilt_position", {"entity_id": "cover.slats", "tilt_position": 40})])

    async def test_a_source_whose_features_are_unknown_is_left_alone(self):
        consumer = Consumer(self.hass, State("cover.slats", "closed", {"current_tilt_position": 0}))
        self.assertNotIn("tilt_command_template", consumer.component)


class TiltTokenTest(unittest.TestCase):
    def test_the_tilt_topic_takes_the_three_tokens_in_any_case(self):
        for payload, service in ((" Open ", "open_cover_tilt"), ("close", "close_cover_tilt"), ("STOP", "stop_cover_tilt")):
            self.assertEqual(disc.command_to_service("cover", "slats", "tilt", payload)[1], service)

    def test_a_number_is_still_a_position_and_a_word_is_refused(self):
        self.assertEqual(disc.command_to_service("cover", "slats", "tilt", "40"),
                         ("cover", "set_cover_tilt_position", {"entity_id": "cover.slats", "tilt_position": 40}))
        with self.assertRaises(ValueError):
            disc.command_to_service("cover", "slats", "tilt", "OPENISH")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
