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
{  // C3: the target's entity domains come from an integration's services.yaml
  const src = read('services.js');
  const code = src.slice(src.indexOf('function selKind('), src.indexOf('function chips('));
  const { callForm } = new Function('document', 'esc', 'CSS', 'fetch', 'confirm', code + '\nreturn {callForm};')(
    document, pageEsc(path.join(STATIC, 'services.js')), { escape: s => s }, async () => ({}), () => true);
  const x = new El('td');
  x.innerHTML = callForm('demo', { name: 'probe', fields: {}, target: { entity: [{ domain: ['<img src=x onerror=alert(1)>', 'light'] }, { domain: 'switch' }] } });
  const label = x.querySelectorAll('label')[0];
  out.target_domains = { images: x.querySelectorAll('img').length, label: label ? label.textContent : null };
}
{  // m3: a subscription the broker refused shows while connected (state keeps flowing, commands do not)
  const line = read('mqtt.js').split('\n').find(l => l.trimStart().startsWith("$('#mqconn').innerHTML="));
  out.mqconn = {};
  for (const [name, s] of Object.entries({
    refused: { enabled: true, connected: true, protocol: 'MQTT 5', subscribe_error: 'subscription refused: <b>cmd/#</b> (Not authorized)' },
    fine: { enabled: true, connected: true, protocol: 'MQTT 5', subscribe_error: '' },
  })) {
    const el = new El('div');
    new Function('$', 'esc', 's', 'skipped', line)(() => el, pageEsc(path.join(STATIC, 'mqtt.js')), s, '');
    out.mqconn[name] = { text: el.textContent, bold: el.querySelectorAll('b').length };
  }
}
console.log(JSON.stringify(out));
