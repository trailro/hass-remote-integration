"""Thirteenth review, MQTT side: an escaped value in the text rule ended at the first backslash-quote of any length, so
a secret written with an escaped quote inside a value that is itself escaped (`code=\\"a\\\\\\"SECRET\\"`, the form a
JSON string value takes in the dumped call data) kept everything after that quote in the history, the status and the
log; a single-quoted value ended at `\\'`, a double-quoted one at a backslash before a line break, and a value never
closed was masked to its first space only.  The rule must stay linear (it runs on paho's network thread).  Every test fails on the tree before the fix."""

import json
import unittest

from custom_components.integration_manager import mqtt_publisher as mp
from tests import test_camp_publish as camp
from tests.test_r11_mqtt import FAST_S, _seconds


class EscapedQuoteMaskingTest(unittest.TestCase):
    def assertMasked(self, text, limit=None):
        for masker in (mp._mask_codes, mp._mask_text):
            with self.subTest(masker=masker.__name__, text=text, limit=limit):
                masked = masker(text, limit)
                self.assertNotIn("SECRET", masked)
                self.assertNotIn("MORE", masked)

    def test_the_documented_json_forms(self):
        for text in ('{"password":"a\\"SECRET MORE"}', '{"code":"\\"SECRET MORE"}'):
            self.assertMasked(text)
            self.assertMasked(text, mp.MASK_SCAN_CHARS)

    def test_an_escaped_quote_inside_an_escaped_value(self):
        for text in ('x code=\\"a\\\\\\"SECRET MORE\\" y',
                     '{"message": "lock.x code=\\"a\\\\\\"SECRET MORE\\""}',
                     '{"data": "{\\"code\\": \\"a\\\\\\"SECRET MORE\\"}"',  # a nested document that does not parse
                     'x {\\\\\\"pin\\\\\\": \\\\\\"1\\\\\\\\\\\\\\"SECRET MORE\\\\\\"}'):
            self.assertMasked(text)
            self.assertMasked(text, mp.MASK_SCAN_CHARS)

    def test_the_value_still_ends_at_its_own_quote(self):
        self.assertEqual(mp._mask_text('x code=\\"a\\\\\\"b\\" mode=\\"away\\"'), 'x code=\\"***\\" mode=\\"away\\"')
        # a plain quote in an escaped value is the end of the string around it
        self.assertEqual(mp._mask_text('{"m": "code=\\"12 34", "n": 1}'), '{"m": "code=\\"***\\"", "n": 1}')

    def test_single_quotes_and_line_breaks(self):
        self.assertMasked("lock.unlock {'code': 'a\\'SECRET MORE'}")
        self.assertMasked("{'code': '12\\'SECRET'}")
        self.assertMasked('{"code": "a\\\nSECRET MORE"}')
        self.assertMasked("x {\\'code\\': \\'1\\\\\\'SECRET MORE\\'}")
        self.assertMasked('x code=\\"a\nSECRET MORE')

    def test_the_reported_forms(self):
        self.assertEqual(mp._mask_text("{'code': '12\\'SYNTHQ'}"), "{'code': '***'}")  # its own quotes (b4cd1a1 review)
        self.assertEqual(mp._mask_text('{\\"code\\": \\"12\\\\\\"SYNTHD\\"}'), '{\\"code\\": \\"***\\"}')

    def test_a_value_never_closed_is_masked_to_the_end(self):
        self.assertMasked('lock.unlock code="12 SECRET MORE')
        self.assertMasked("lock.unlock code='12 SECRET MORE")

    def test_the_history_row_of_a_call(self):
        pub = camp._publisher()
        for payload in ('{"entity_id": "script.x", "note": "code=\\"a\\\\\\"SECRET MORE\\""}',
                        '{"entity_id": "script.x", "data": "{\\"code\\": \\"a\\\\\\"SECRET MORE\\"}"'):
            for unparsable in (False, True):
                with self.subTest(payload=payload, unparsable=unparsable):
                    rec = pub._remember("call", "script.turn_on", payload, unparsable=unparsable)
                    self.assertNotIn("SECRET", json.dumps(rec))


class EscapedQuoteMaskingTimeTest(unittest.TestCase):
    """Adversarial escaping over a whole call payload (256 KB), without a limit (the debug log line)."""

    def test_openers_of_every_length(self):
        grow = 'parts, b = [], 1\nwhile sum(map(len, parts)) < mp.CALL_MAX_BYTES - 32:\n    parts.append("pin=" + "\\\\" * b + chr(34) + "x "); b += 1\n'
        self.assertLess(_seconds(grow + 'mp._mask_codes("".join(parts))'), FAST_S)
        self.assertLess(_seconds(grow + 'mp._mask_codes("".join(reversed(parts)))'), FAST_S)

    def test_runs_and_quotes(self):
        n = "mp.CALL_MAX_BYTES // 8"
        for body in ("\"code=\\\\'\" + \"\\\\\\\\\\\\'a\\\"b\" * N", '"code=\\\\\\"" + "\\\\\\\\\\"a" * N', '"code=\\"" + "\\\\\\"a code=" * N', "\"code='\" + \"\\\\'a code=\" * N",
                     '"code=\\\\\\\\\\\\\\"" + "\\\\\\"code=\\\\\\"" * N', 'json.dumps({"code": "\\\\" + "\\"" * (2 * N)})'):
            with self.subTest(body=body):
                self.assertLess(_seconds(f"N = {n}\nmp._mask_codes({body})"), FAST_S)


if __name__ == "__main__":
    unittest.main()
