"""Full external review (b433d06): event replay, suspended entries, text masking, check tokens."""

import asyncio
import unittest
from types import SimpleNamespace

from custom_components.integration_manager import build_views
from custom_components.integration_manager.diagnostics import scrub
from custom_components.integration_manager.installer import Installer
from custom_components.integration_manager.mqtt_publisher import MqttConfig, MqttPublisher


def _state(entity_id, state):
    return SimpleNamespace(entity_id=entity_id, domain=entity_id.split(".")[0], state=state)


class EventReplayTest(unittest.TestCase):
    def test_availability_flap_does_not_replay(self):
        pub = object.__new__(MqttPublisher)
        pub._last_event = {}
        pub.config, pub._connected = MqttConfig(), False  # only the replay logic is under test: no discovery here
        emitted = []
        pub._publish_state = lambda st, is_event=False, force=True: emitted.append((st.state, is_event))
        seq = [(None, "T1"), ("T1", "unavailable"), ("unavailable", "T1"), ("T1", "T2"), ("T2", "unavailable"),
               ("unavailable", "T2"), ("T2", "unavailable"), ("unavailable", "T2"), ("T2", "T3")]
        for old, new in seq:
            pub._on_state(SimpleNamespace(data={"old_state": _state("event.button", old) if old else None,
                                                "new_state": _state("event.button", new)}))
        self.assertEqual([s for s, e in emitted if e], ["T2", "T3"])


class _Entries:
    def __init__(self, entries):
        self.entries = entries

    async def async_set_disabled_by(self, entry_id, disabler):
        next(e for e in self.entries if e.entry_id == entry_id).disabled_by = disabler


class SuspendedEntriesTest(unittest.TestCase):
    def _installer(self, entries, suspended):
        inst = object.__new__(Installer)
        inst.state = SimpleNamespace(installed={"demo": {}}, suspended_entries=suspended)
        inst.hass = SimpleNamespace(config_entries=_Entries(entries))
        inst._entries_of = lambda d: entries
        inst._save_state = lambda: None
        return inst

    def test_user_disabled_entry_stays_disabled(self):
        entries = [SimpleNamespace(entry_id="mine", disabled_by="user"), SimpleNamespace(entry_id="stopped", disabled_by="user")]
        inst = self._installer(entries, ["stopped"])
        self.assertEqual(asyncio.run(inst._enable_entries("demo")), ["stopped"])
        self.assertEqual(entries[0].disabled_by, "user")
        self.assertIsNone(entries[1].disabled_by)
        self.assertEqual(inst.state.suspended_entries, [])

    def test_volume_from_before_resumes_everything_once(self):
        entries = [SimpleNamespace(entry_id="a", disabled_by="user")]
        inst = self._installer(entries, None)
        self.assertEqual(asyncio.run(inst._enable_entries("demo")), ["a"])


class TextMaskingTest(unittest.TestCase):
    def test_pins_keys_and_quoted_values(self):
        self.assertEqual(scrub('password="synthetic alpha beta" ok'), 'password="***" ok')
        self.assertEqual(scrub('{"pin": 1234}'), '{"pin": ***}')
        self.assertNotIn("SYNTHETIC", scrub("encryption_key=SYNTHETIC_KEY_123"))
        self.assertEqual(scrub("spinning: 5"), "spinning: 5")


class CheckTokenTest(unittest.TestCase):
    def test_token_changes_with_the_commit(self):
        a = build_views._check_token("x", "main", "", "aaa")
        self.assertNotEqual(a, build_views._check_token("x", "main", "", "bbb"))
        self.assertEqual(a, build_views._check_token("x", "main", "", "aaa"))


if __name__ == "__main__":
    unittest.main()
