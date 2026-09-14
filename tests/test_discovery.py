"""discovery.manager_device."""

import unittest

from custom_components.integration_manager import discovery as disc

KEY = "hass_demo"
TOPICS = {"status": f"{KEY}/status", "health": f"{KEY}/health", "manager": f"{KEY}/manager", "cmd": f"{KEY}/manager/cmd"}


def components(integration="demo", commands=True):
    return disc.manager_device(KEY, KEY + "_", TOPICS, integration, "1.0.0", commands)[2]


def commands_of(comps):
    out = {}
    for comp in comps.values():
        if "command_topic" in comp:
            payload = comp.get("payload_install", comp.get("payload_press"))
            out[comp["command_topic"].removeprefix(TOPICS["cmd"] + "/")] = payload
    return out


class ManagerDeviceTest(unittest.TestCase):
    def test_with_commands(self):
        comps = components()
        self.assertEqual(commands_of(comps), disc.MANAGER_ACTIONS)
        for part in ("integration", "home_assistant"):
            comp = comps[f"update.{KEY}_{part}_update"]
            self.assertEqual(comp["command_topic"], f"{TOPICS['cmd']}/install_{part}")
            self.assertEqual(comp["payload_install"], "install")
        self.assertEqual({e for e, c in comps.items() if c["platform"] == "button"},
                         {f"button.{KEY}_{s}" for s in ("restart", "backup", "check_updates")})

    def test_without_commands(self):
        comps = components(commands=False)
        self.assertEqual(commands_of(comps), {})
        self.assertFalse(any("payload_install" in c for c in comps.values()))
        self.assertFalse(any(c["platform"] == "button" for c in comps.values()))
        self.assertIn(f"update.{KEY}_integration_update", comps)

    def test_manager_update_has_no_command(self):
        for commands in (True, False):
            comp = components(commands=commands)[f"update.{KEY}_manager_update"]
            self.assertNotIn("command_topic", comp)
            self.assertNotIn("payload_install", comp)

    def test_without_integration(self):
        comps = components(integration=None)
        self.assertNotIn(f"update.{KEY}_integration_update", comps)
        self.assertEqual(set(commands_of(comps)), set(disc.MANAGER_ACTIONS) - {"install_integration"})
        self.assertEqual(comps[f"sensor.{KEY}_health"]["name"], "none health")

    def test_unique_ids(self):
        comps = components()
        ids = [c["unique_id"] for c in comps.values()]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
