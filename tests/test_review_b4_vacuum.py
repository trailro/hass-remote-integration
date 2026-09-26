"""End-to-end run on b4cd1a1: an unavailable vacuum showed `unknown` on the main Home Assistant, not unavailable.  Its
document replaces `state` with the activity, null for anything else, and the shared availability template reads
`state == 'unavailable'`, which then never matched.  Checked against the consuming Home Assistant's own platform code."""

from homeassistant.core import State

from custom_components.integration_manager import discovery as disc
from tests.test_e2e_disc import Consumer, _HassCase, document
from tests.test_polish_disc_text import availability

ATTRS = {"fan_speed_list": ["quiet", "standard", "turbo"], "fan_speed": "standard", "supported_features": 14140}


class UnavailableVacuumTest(_HassCase):
    def bot(self, state):
        return State("vacuum.bot", state, ATTRS)

    async def test_unavailable_at_the_source_is_unavailable_there(self):
        consumer = Consumer(self.hass, self.bot("docked"))
        self.assertTrue(availability(self.hass, consumer, self.bot("docked")))
        self.assertQuiet(consumer, self.bot("unavailable"))
        self.assertFalse(availability(self.hass, consumer, self.bot("unavailable")))
        self.assertIsNone(consumer.entity.activity)  # still never read as an activity
        self.assertTrue(availability(self.hass, consumer, self.bot("cleaning")))

    async def test_unknown_is_no_activity_and_stays_available(self):
        consumer = Consumer(self.hass, self.bot("cleaning"))
        self.assertQuiet(consumer, self.bot("cleaning"))
        self.assertQuiet(consumer, self.bot("unknown"))
        self.assertIsNone(consumer.entity.activity)
        self.assertTrue(availability(self.hass, consumer, self.bot("unknown")))

    async def test_a_document_from_before_the_field_counts_as_online(self):
        doc = document(self.bot("docked"))
        doc.pop("availability")
        consumer = Consumer(self.hass, self.bot("docked"))
        tpl = next(a["value_template"] for a in consumer.component["availability"] if "value_template" in a)
        from homeassistant.helpers.template import Template

        self.assertEqual(Template(tpl, self.hass).async_render({"value_json": doc}, parse_result=False), "online")

    async def test_other_platforms_keep_the_shared_template(self):
        light = Consumer(self.hass, State("light.l", "on", {"supported_color_modes": ["onoff"]}))
        self.assertIn(disc._AVAILABILITY_TPL, [a.get("value_template") for a in light.component["availability"]])
