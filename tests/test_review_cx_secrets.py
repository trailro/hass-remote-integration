"""Third external review, secret disclosure.

F2: a permitted service that failed with its own words - ValueError("authentication failed: password=...") -
had that text assigned straight to the command record's "error".  _remember masks what the CALLER sent;
nothing masked what the SERVICE said back, and recent_commands, which drops "result", returned "error" as it
was.  So the secret reached the Commands page, /api/mqtt/commands, the MQTT status document and the
diagnostics zip - the pages an operator screenshots into a support thread.  Both failure paths were affected:
a service call on <base>/call/... and an entity command on <base>/cmd/....

F3: an entity EXCLUDED from MQTT was shown with more, not less.  A published entity's document runs its
attributes through _published_attributes, which drops access_token and any value carrying a ?token= URL; an
excluded entity has no document, so entity_rows took its fallback branch and put dict(state.attributes) - the
raw attributes - into the row /api/entities serialises.

Every test fails on the tree before the fix unless its docstring says it pins behaviour that already held.
Secrets are synthetic."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er

from custom_components.integration_manager import entities_page
from custom_components.integration_manager import mqtt_publisher as mp
from tests.test_r3_mqtt import BASE, _message, _publisher
from tests.test_r4_mqtt import _running_publisher, _settle

SECRET = "SNTL-7f3a9c41"  # synthetic
# the three shapes a third-party client library puts a credential in when it raises
LEAKY_ERRORS = {
    "password": f"authentication failed: password={SECRET}",
    "token_url": f"401 Unauthorized for https://hub.invalid/api/v1/state?token={SECRET}",
    "bearer": f"rejected header Authorization: Bearer {SECRET}",
}


def _raiser(text):
    async def service(*_a, **_k):
        raise ValueError(text)

    return service


class ServiceCallErrorTest(unittest.IsolatedAsyncioTestCase):
    """F2, the <base>/call/<domain>/<service> path."""

    async def _call(self, text):
        pub = _running_publisher()
        pub.hass.services.async_call = _raiser(text)
        pub._on_call("hri_probe/tick", json.dumps({"entity_id": "light.a"}))
        await _settle()
        return pub

    async def test_the_history_row_hides_what_the_service_said(self):
        for name, text in LEAKY_ERRORS.items():
            with self.subTest(shape=name):
                pub = await self._call(text)
                row = pub.recent_commands()[0]
                self.assertEqual(row["state"], "error")
                self.assertNotIn(SECRET, row["error"])
                self.assertIn("ValueError", row["error"])

    async def test_nothing_the_commands_api_serialises_carries_it(self):
        for name, text in LEAKY_ERRORS.items():
            with self.subTest(shape=name):
                pub = await self._call(text)
                self.assertNotIn(SECRET, json.dumps(pub.recent_commands(200)))

    async def test_a_late_error_after_a_timeout_is_hidden_too(self):
        pub = _running_publisher()

        async def service(*_a, **_k):
            await pub.release.wait()
            raise ValueError(LEAKY_ERRORS["password"])

        pub.hass.services.async_call = service
        with mock.patch.object(mp, "CALL_TIMEOUT_S", 0):
            pub._on_call("hri_probe/tick", json.dumps({"entity_id": "light.a"}))
            await _settle()
            pub.release.set()
            await _settle()
        self.assertEqual(pub.recent_commands()[0]["state"], "late-error")
        self.assertNotIn(SECRET, json.dumps(pub.recent_commands()))

    async def test_an_internal_error_path_is_hidden_too(self):
        """Pins behaviour that already held: _internal_error never repeats the exception's message.  Here so
        the three error paths of a call - the service's own failure, a refusal and a crash of our own handling
        - are asserted together."""
        pub = _running_publisher()
        pub._call_target_problem = mock.Mock(side_effect=ValueError(LEAKY_ERRORS["password"]))
        pub._on_call("hri_probe/tick", json.dumps({"entity_id": "light.a"}))
        await _settle()
        self.assertNotIn(SECRET, json.dumps(pub.recent_commands()))

    async def test_an_ordinary_failure_stays_readable(self):
        """Pins that the scrubber does not eat the text an operator needs."""
        pub = _running_publisher()
        pub.hass.services.has_service = lambda d, s: False
        pub._on_call("hri_probe/tick", "{}")
        await _settle()
        self.assertEqual(pub.recent_commands()[0]["error"], "unknown service hri_probe.tick")


class EntityCommandErrorTest(unittest.IsolatedAsyncioTestCase):
    """F2, the <base>/cmd/<domain>/<object_id>/<field> path: the review named the service call, the entity
    command records its error the same way."""

    async def _command(self, text):
        pub = _running_publisher()
        pub.stats.update(commands=0, last_command=None)
        pub._topics = {"switch.a": f"{BASE}/demo/switch/a"}
        pub.hass.services.async_call = _raiser(text)
        pub._handle_message(_message(f"{BASE}/cmd/switch/a/state", "ON"))
        await _settle()
        return pub

    async def test_the_history_row_hides_what_the_service_said(self):
        for name, text in LEAKY_ERRORS.items():
            with self.subTest(shape=name):
                pub = await self._command(text)
                row = pub.recent_commands()[0]
                self.assertEqual(row["state"], "error")
                self.assertNotIn(SECRET, row["error"])
                self.assertNotIn(SECRET, json.dumps(pub.recent_commands(200)))

    async def test_a_rejected_command_still_says_why(self):
        """Pins that the scrubber does not eat the text an operator needs."""
        pub = await self._command("never reached")
        pub._handle_message(_message(f"{BASE}/cmd/switch/a/state", "SIDEWAYS"))
        await _settle()
        self.assertIn("neither an on nor an off payload", pub.recent_commands()[0]["error"])


class HistoryResultTest(unittest.IsolatedAsyncioTestCase):

    async def test_the_result_is_still_dropped(self):
        """Pins behaviour that already held: the answer published to the caller keeps the service's words,
        and the public history never carries it."""
        pub = _running_publisher()
        pub.hass.services.async_call = _raiser(LEAKY_ERRORS["password"])
        pub._on_call("hri_probe/tick", json.dumps({"entity_id": "light.a"}))
        await _settle()
        self.assertIsNone(pub.recent_commands()[0]["result"])
        self.assertTrue(any(SECRET in json.dumps(r) for _d, _s, r in pub.results))


TOKEN_ATTRS = {
    "friendly_name": "Hall camera",
    "access_token": SECRET,
    "entity_picture": f"/api/camera_proxy/camera.hall?token={SECRET}",
}


class ExcludedEntityAttributesTest(unittest.TestCase):
    """F3: published, excluded by a rule and excluded through exclude_integrations must agree."""

    def _rows(self, *, rule=None, exclude=()):
        state = State("camera.hall", "idle", dict(TOKEN_ATTRS))
        pub = _publisher(exclude=exclude)
        pub.config = SimpleNamespace(exclude_integrations=list(exclude), main_ha_version="")
        pub.rules = SimpleNamespace(for_entity=lambda _e: dict(rule or {}))
        hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda: [state]), data={},
                               config=SimpleNamespace(config_dir="/tmp"))
        registry = SimpleNamespace(async_get=lambda _e: None, entities={})
        with mock.patch.object(er, "async_get", return_value=registry), \
                mock.patch.object(mp, "platform_of", return_value="demo"), \
                mock.patch.object(entities_page, "platform_of", return_value="demo"):
            return entities_page.entity_rows(hass, pub)

    def test_the_three_rows_carry_the_same_attributes(self):
        published = self._rows()[0]
        by_rule = self._rows(rule={"exclude": True})[0]
        by_integration = self._rows(exclude=("demo",))[0]
        self.assertIsNotNone(published["mqtt_topic"])
        self.assertIsNone(by_rule["mqtt_topic"])
        self.assertIsNone(by_integration["mqtt_topic"])
        self.assertEqual(published["attributes"], by_rule["attributes"])
        self.assertEqual(published["attributes"], by_integration["attributes"])

    def test_no_excluded_row_carries_the_token(self):
        for name, kwargs in (("rule", {"rule": {"exclude": True}}), ("integration", {"exclude": ("demo",)})):
            with self.subTest(excluded_by=name):
                row = self._rows(**kwargs)[0]
                self.assertNotIn("access_token", row["attributes"])
                self.assertNotIn(SECRET, json.dumps(row, default=str))

    def test_the_rest_of_the_attributes_are_still_there(self):
        """The row is sanitised, not emptied: the page still needs it to be readable."""
        for name, kwargs in (("published", {}), ("rule", {"rule": {"exclude": True}}), ("integration", {"exclude": ("demo",)})):
            with self.subTest(row=name):
                self.assertEqual(self._rows(**kwargs)[0]["attributes"], {"friendly_name": "Hall camera"})


class EntitiesApiTest(unittest.TestCase):
    """What /api/entities serialises is what entity_rows built: the view adds nothing back."""

    def test_the_api_serialises_the_sanitised_rows(self):
        rows = [{"entity_id": "camera.hall", "attributes": {"friendly_name": "Hall camera"}}]
        view = entities_page.EntitiesApiView.__new__(entities_page.EntitiesApiView)
        view.hass, view.publisher = mock.Mock(), mock.Mock()
        with mock.patch.object(entities_page, "entity_rows", return_value=rows):
            body = asyncio.run(view.get(mock.Mock())).body
        self.assertNotIn(SECRET.encode(), body)
        self.assertEqual(json.loads(body), rows)


if __name__ == "__main__":
    unittest.main()
