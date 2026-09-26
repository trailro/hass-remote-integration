"""The System page's watchdog line (static/system.js wdRender).  A restart the watchdog decided on but did not make
(its record could not be written to state.json, or the restart itself was refused) is recorded with next = "not
restarted: ...", and the line read "restarted after N min ... - not restarted: ...".

Needs node, which the container the unit tests run in does not have: it skips there and runs wherever node is
installed (a developer machine, CI)."""

import json
import os
import shutil
import subprocess
import unittest

SYSTEM_JS = os.path.join(os.path.dirname(__file__), os.pardir, "custom_components", "integration_manager", "static",
                         "system.js")

SCRIPT = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('function wdRender(w){');
const end = src.indexOf("\n$('#wd').onchange", start);
const esc = s => String(s);
const els = {};
const $ = sel => (els[sel] ??= {innerHTML: ''});
eval(src.slice(start, end));
const out = {};
for (const [name, next] of Object.entries({done: 'next restart allowed after 30 min', refused: 'not restarted: the watchdog\'s record could not be written to state.json (OSError: full)'})) {
  wdRender({last: {at: '2026-09-26T10:00:00', unhealthy_s: 1200, state: 'error', reason: 'x', attempt: 1, next}});
  out[name] = els['#wdlast'].innerHTML;
}
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not available here")
class WatchdogLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run([shutil.which("node"), "-e", SCRIPT, SYSTEM_JS], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(f"node failed: {out.stderr.strip()}")
        cls.out = json.loads(out.stdout)

    def test_a_restart_made_says_restarted(self):
        self.assertIn(": restarted after 20 min", self.out["done"])

    def test_a_restart_not_made_does_not_say_restarted(self):
        self.assertIn(": restart refused after 20 min", self.out["refused"])
        self.assertNotIn(": restarted after", self.out["refused"])


if __name__ == "__main__":
    unittest.main()
