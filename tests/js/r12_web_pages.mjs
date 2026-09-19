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
{  // C1: two lines of the MQTT page escape and then assign to .textContent, which parses no HTML
  const lines = read('mqtt.js').split('\n');
  const s = { commands: 4, last_command: 'call light.turn_on {"brightness": 255} & wait', cmd_base: 'hass_demo/cmd',
              services_published: 12, base_topic: 'hass_demo', calls: 7,
              last_call: 'light.turn_on {"entity_id": "light.hall"}', call_base: 'hass_demo/call' };
  out.mqtt_text = {};
  for (const id of ['#mqcmd', '#mqcall']) {
    const line = lines.find(l => l.trimStart().startsWith(`$('${id}').textContent=`));
    const el = new El('div');
    new Function('$', 'esc', 's', line)(() => el, pageEsc(path.join(STATIC, 'mqtt.js')), s);
    out.mqtt_text[id.slice(1)] = el.textContent;
  }
}

{  // F14 again, on this page: the per-integration health rules table refused to repaint while anything inside it
   // had the focus -- and each row's own Save button is inside it, so it keeps the focus in Chrome and Edge
  const src = read('mqtt.js');
  const code = src.slice(src.indexOf('async function healthRules()'), src.indexOf("$('#hsave').onclick="));
  // the header row the page keeps: tr:not(:first-child) is every row after it
  class Table extends El {
    querySelectorAll(sel) {
      if (sel === 'tr:not(:first-child)') return this.elementChildren.filter(c => c.tag === 'tr').slice(1);
      return super.querySelectorAll(sel);
    }
  }
  const run = async (focus) => {
    const els = {};
    const $ = q => (els[q] ??= Object.assign(q === '#hrules' ? new Table('table') : new El(q === '#hmsg' ? 'div' : 'input'), { id: q.slice(1) }));
    const t = $('#hrules');
    t.appendChild(Object.assign(new El('tr'), { className: 'head' }));   // the header the page never removes
    t.contains = n => { for (let e = n; e; e = e.parentNode) if (e === t) return true; return false; };
    const doc = { ...document, activeElement: null };
    let installed = { demo: {} };
    const fetch = async url => ({ json: async () => url === 'api/settings'
      ? { health_stale_s: 900, health_unavailable_pct: 50, health: {} }
      : { installed, running: null } });
    const healthRules = new Function('$', 'document', 'esc', 'fetch', 'post',
      code + '\nreturn healthRules;')($, doc, pageEsc(path.join(STATIC, 'mqtt.js')), fetch, async () => ({ ok: true }));
    await healthRules();
    const rows = () => t.querySelectorAll('tr:not(:first-child)').map(r => r.querySelectorAll('b')[0].textContent);
    const before = rows();
    doc.activeElement = focus ? t.querySelector(focus) : null;
    installed = { demo: {}, other: {} };                                 // a second integration installed meanwhile
    await healthRules();
    return { before, after: rows() };
  };
  out.health_rules = {
    nothing_focused: await run(null),
    save_focused: await run('button[data-hs]'),      // the Save button of a row: inside the table
    input_focused: await run('input[data-h=stale_s]'),
  };
}

console.log(JSON.stringify(out));
