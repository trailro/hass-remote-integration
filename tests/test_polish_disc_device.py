"""In the five minutes after a start (the orphan-sweep window), removing a device's last *live* entity cleared the
whole device discovery config, which also took off the main Home Assistant the device's other entities: the ones an
earlier process had announced and that have not finished setting up here.  They come back only once they do."""

import unittest
from types import SimpleNamespace

from tests.test_e2e_pub_discovery import StartWindowCase


class LastLiveEntityRemovedTest(StartWindowCase):
    """sensor.b was announced by the previous process and is still setting up here; sensor.a, the only entity this
    process has, is deleted.  Clearing the device config would take sensor.b off the main HA with it."""

    LIVE, SETTING_UP = ("sensor.a",), ("sensor.b",)

    def remove_the_live_one(self):
        self.registry.entities.pop("sensor.a")
        self.states.pop("sensor.a")
        self.pub._on_registry(SimpleNamespace(data={"action": "remove", "entity_id": "sensor.a"}))

    async def test_the_device_and_the_entity_still_setting_up_are_kept(self):
        self.remove_the_live_one()
        self.assertIsNotNone(self.raw_config(), "the device config was cleared with sensor.b still on it")
        components = self.config()["components"]
        self.assertIn("unique_id", components.get("sensor_b", {}))
        self.assertEqual(components["sensor_a"], {"platform": "sensor"})  # the removal form for the one that went

    async def test_a_full_republish_keeps_carrying_it(self):
        self.remove_the_live_one()
        await self.pub.async_republish_all()
        self.assertIsNotNone(self.raw_config(), "the full republish cleared the device config")
        self.assertIn("unique_id", self.config()["components"].get("sensor_b", {}))

    async def test_the_orphan_sweep_still_clears_the_device_afterwards(self):
        self.remove_the_live_one()
        self.pub._boot_components, self.pub._orphan_sweep_due = {}, False  # what the sweep leaves behind
        await self.pub.async_republish_all()
        self.assertIsNone(self.raw_config())


class CarriedEntityExcludedTest(StartWindowCase):
    """Found end to end: an entity only the previous process announced (excluded by a rule at this start, or still
    setting up) was excluded now.  Nothing of this process held it, so the carry went on and the main Home Assistant
    kept the entity in full until the sweep five minutes later."""

    LIVE, SETTING_UP = ("sensor.a",), ("sensor.b",)

    async def test_the_carry_ends_and_the_removal_form_goes_out(self):
        self.pub.rules.set("sensor.b", exclude=True)
        self.pub._remove_component("sensor.b")
        components = self.config()["components"]
        self.assertEqual(components.get("sensor_b"), {"platform": "sensor"}, "sensor.b was still announced in full")
        self.assertIn("unique_id", components.get("sensor_a", {}))  # the live one stays
        await self.pub.async_republish_all()
        self.assert_removed_for_good("sensor_b")

    async def test_the_device_goes_when_its_last_live_entity_follows(self):
        self.pub.rules.set("sensor.b", exclude=True)
        self.pub._remove_component("sensor.b")
        self.registry.entities.pop("sensor.a")
        self.states.pop("sensor.a")
        self.pub._on_registry(SimpleNamespace(data={"action": "remove", "entity_id": "sensor.a"}))
        self.assertIsNone(self.raw_config(), "nothing left on the device: its config should be cleared")


class GenuinelyLastEntityRemovedTest(StartWindowCase):
    """Nothing carried from an earlier process: the last entity of the device really is the last one."""

    LIVE, SETTING_UP = ("sensor.a",), ()

    async def test_the_device_config_is_cleared(self):
        self.registry.entities.pop("sensor.a")
        self.states.pop("sensor.a")
        self.pub._on_registry(SimpleNamespace(data={"action": "remove", "entity_id": "sensor.a"}))
        self.assertIsNone(self.raw_config())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
