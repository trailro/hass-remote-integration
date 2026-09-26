"""The header's status chips (integration, health, MQTT, notifications, log out) were hidden below 1150 px, so inside
the Home Assistant panel (an iframe about 1144 px wide at a 1400 px window) they never showed, and on the direct port
at 1400 px the last one was cut off at the right edge.  The bar now wraps: the page links stay on one row (scrolling
sideways when narrow), and the chips move to a row of their own under them when both do not fit."""

import os
import re
import unittest

from custom_components.integration_manager import ui

CSS = os.path.join(os.path.dirname(ui.__file__), "static", "hri.css")


def _rules(css: str) -> list[tuple[str, str, str]]:
    """(media query or "", selector, declarations) of every rule, one level of @media deep."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, i = [], 0
    while (m := re.compile(r"\s*([^{}]+)\{").match(css, i)):
        head, i = m.group(1).strip(), m.end()
        if head.startswith("@media"):
            while (r := re.compile(r"\s*([^{}]+)\{([^{}]*)\}").match(css, i)):
                out.append((head, r.group(1).strip(), r.group(2)))
                i = r.end()
            i = css.index("}", i) + 1
        else:
            end = css.index("}", i)
            out.append(("", head, css[i:end]))
            i = end + 1
    return out


def _decls(text: str) -> dict[str, str]:
    return {k.strip(): v.strip() for k, _, v in (d.partition(":") for d in text.split(";")) if k.strip()}


class TopbarCssTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CSS, encoding="utf-8") as fh:
            cls.rules = _rules(fh.read())

    def rule(self, selector: str) -> dict[str, str]:
        [decls] = [_decls(d) for media, sel, d in self.rules if not media and sel == selector]
        return decls

    def test_no_width_hides_the_chips(self):
        for media, sel, decls in self.rules:
            if "chip" in sel:
                with self.subTest(media=media, selector=sel):
                    self.assertNotEqual(_decls(decls).get("display"), "none")

    def test_the_bar_wraps_instead_of_cutting_off(self):
        bar = self.rule(".topbar")
        self.assertEqual(bar.get("flex-wrap"), "wrap")
        self.assertNotIn("height", bar)  # a fixed height would clip the second row
        self.assertNotIn("overflow-x", bar)  # scrolling the whole bar hid the chips past the right edge
        chips = self.rule(".topbar #tb-chips")
        self.assertEqual(chips.get("flex-wrap"), "wrap")  # several rows of chips on a phone, never wider than the bar
        self.assertEqual(chips.get("min-width"), "0")
        self.assertEqual(chips.get("max-width"), "100%")
        links = self.rule(".topbar .tb-nav")
        self.assertEqual(links.get("overflow-x"), "auto")  # the page links scroll sideways on a narrow screen
        self.assertEqual(links.get("min-width"), "0")
        self.assertEqual(links.get("flex"), "1 1 auto")  # on a wide screen: one row, the chips on the right as before


class TopbarMarkupTest(unittest.TestCase):
    def test_links_and_chips_are_siblings_in_the_bar(self):
        bar = ui.topbar("/config")
        m = re.fullmatch(r'<nav class="topbar"><div class="tb-nav">(.*)</div><span id="tb-chips"></span></nav>', bar)
        self.assertIsNotNone(m, bar)
        self.assertIn('<a class="brand" href="./">', m.group(1))
        self.assertIn('<a class="nav active" href="config">Integration</a>', m.group(1))
        self.assertNotIn("style=", bar)  # CSP: no inline styles added


if __name__ == "__main__":
    unittest.main()
