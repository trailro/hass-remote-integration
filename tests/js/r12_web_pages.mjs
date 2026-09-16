// Runs page code the twelfth review changed -- the top bar's log out (static/hri.js) and the call form's target
// line (static/services.js) -- and prints one JSON object with what each showed.  tests/test_r12_web_js.py reads
// it; it skips when node is missing (the container the unit tests run in has none).
//   node tests/js/r12_web_pages.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, document, pageEsc } from './dom.mjs';

const STATIC = process.argv[2];
const read = name => fs.readFileSync(path.join(STATIC, name), 'utf8');
const out = {};

{  // m15: a logout the volume could not record
  const line = /^async function logout\(e\)\{.*\}$/m.exec(read('hri.js'))[0];
  out.logout = {};
  for (const [name, answer] of Object.entries({
    refused: { ok: false, error: 'logged out, but the logout could not be recorded on the volume (No space left on device)' },
    recorded: { ok: true },
  })) {
    const alerts = [], sent = [], location = { href: '/config' };
    const post = async url => { sent.push(url); return answer; };
    const logout = new Function('post', 'alert', 'location', `${line}\nreturn logout;`)(post, m => alerts.push(m), location);
    await logout({ preventDefault() {} });
    out.logout[name] = { alerts, sent, href: location.href };
  }
}
console.log(JSON.stringify(out));
