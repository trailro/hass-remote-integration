import unittest

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from custom_components.integration_manager import change_report as cr


def entity(entity_id, **kw):
    return {"entity_id": entity_id, "name": entity_id, "unit_of_measurement": None, "device_class": None,
            "state_class": None, "entity_category": None, **kw}


class SchemaKeysTest(unittest.TestCase):
    def test_plain_service_schema(self):
        self.assertEqual(cr._schema_keys(vol.Schema({vol.Required("value"): str, vol.Optional("attributes"): dict})), ["attributes", "value"])

    def test_entity_service_schema_without_target_keys(self):
        self.assertEqual(cr._schema_keys(cv.make_entity_service_schema({vol.Required("value"): str, vol.Optional("until"): str})),
                         ["until", "value"])

    def test_no_schema(self):
        self.assertEqual(cr._schema_keys(None), [])


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.before = {"at": "t0", "services": {"set": ["value"], "reset": []},
                       "entities": {"uid:a": entity("sensor.a", unit_of_measurement="°C"), "uid:b": entity("sensor.b"),
                                    "uid:c": entity("sensor.c"), "eid:sensor.x": entity("sensor.x")}}

    def report(self, after):
        return cr.build({"domain": "demo", "from_tag": "1.0", "to_tag": "2.0", "at": "t0", "before": self.before}, after)

    def test_every_kind_of_change(self):
        r = self.report({"at": "t1", "services": {"set": ["value", "attributes"], "update": []},
                         "entities": {"uid:a": entity("sensor.a", unit_of_measurement="°F"), "uid:b": entity("sensor.b_new"),
                                      "uid:d": entity("sensor.d"), "eid:sensor.x": entity("sensor.x")}})
        self.assertEqual([e["entity_id"] for e in r["entities_removed"]], ["sensor.c"])
        self.assertEqual([e["entity_id"] for e in r["entities_added"]], ["sensor.d"])
        self.assertEqual(r["entities_renamed"], [{"from": "sensor.b", "to": "sensor.b_new"}])
        self.assertEqual(r["entities_changed"], [{"entity_id": "sensor.a", "changes": {"unit_of_measurement": ["°C", "°F"]}}])
        self.assertEqual((r["services_added"], r["services_removed"]), (["update"], ["reset"]))
        self.assertEqual(r["fields_added"], [{"service": "set", "fields": ["attributes"]}])
        self.assertTrue(r["breaking"])
        self.assertIn("1 removed", cr.summary(r))

    def test_only_additions_are_not_breaking(self):
        after = {**self.before, "at": "t1", "entities": {**self.before["entities"], "uid:e": entity("sensor.e")},
                 "services": {**self.before["services"], "new": ["x"]}}
        r = self.report(after)
        self.assertFalse(r["breaking"])
        self.assertEqual(len(r["entities_added"]), 1)

    def test_removed_field_is_breaking(self):
        r = self.report({**self.before, "at": "t1", "services": {"set": [], "reset": []}})
        self.assertEqual(r["fields_removed"], [{"service": "set", "fields": ["value"]}])
        self.assertTrue(r["breaking"])
