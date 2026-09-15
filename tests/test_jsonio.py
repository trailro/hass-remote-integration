"""jsonio.vkey and jsonio.ha_vkey."""

import unittest

from jsonio import ha_vkey, vkey


class VkeyTest(unittest.TestCase):
    def test_v_prefix_is_ignored(self):
        self.assertEqual(vkey("v1.2.3"), vkey("1.2.3"))
        self.assertEqual(vkey("V1.2.3"), (1, 2, 3, 0))
        self.assertEqual(vkey("1.2.0"), vkey("v1.2"))

    def test_numeric_order(self):
        self.assertGreater(vkey("1.10.0"), vkey("1.9.0"))
        self.assertGreater(vkey("v2.0.0"), vkey("1.99.99"))

    def test_four_parts(self):
        self.assertEqual(vkey("1.2.3.4"), (1, 2, 3, 4))
        self.assertGreater(vkey("1.2.3.4"), vkey("1.2.3"))
        self.assertEqual(vkey("1.2.3.4.5"), (1, 2, 3, 4))

    def test_non_versions(self):
        self.assertEqual(vkey("local"), ())
        self.assertEqual(vkey("feature/x"), ())
        self.assertEqual(vkey("release-2"), (2, 0, 0, 0))
        self.assertLess(vkey("local"), vkey("0.0.1"))

    def test_empty(self):
        self.assertEqual(vkey(""), (0, 0, 0, 0))
        self.assertEqual(vkey(None), (0, 0, 0, 0))


class HaVkeyTest(unittest.TestCase):
    def test_beta_before_release(self):
        self.assertLess(ha_vkey("2026.9.0b2"), ha_vkey("2026.9.0"))
        self.assertLess(ha_vkey("2026.9.0b2"), ha_vkey("2026.9.0b10"))
        self.assertGreater(ha_vkey("2026.9.1b0"), ha_vkey("2026.9.0"))
        self.assertGreater(vkey("2026.9.0b2"), vkey("2026.9.0"))  # why ha_vkey exists

    def test_releases(self):
        self.assertLess(ha_vkey("2026.9.3"), ha_vkey("2026.10.0"))
        self.assertEqual(ha_vkey(" 2026.9.0 "), ha_vkey("2026.9.0"))

    def test_fallback(self):
        self.assertEqual(ha_vkey("v2026.9.0"), ha_vkey("2026.9.0"))
        self.assertEqual(ha_vkey("local"), (1, 0))
        self.assertEqual(ha_vkey(""), (0, 0, 0, 1, 0))
        self.assertEqual(ha_vkey(None), (0, 0, 0, 1, 0))
        self.assertLess(ha_vkey(None), ha_vkey("2026.1.0"))


if __name__ == "__main__":
    unittest.main()
