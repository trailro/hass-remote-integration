// Runs the Cutover page (static/parity.js) against the answers of a container whose id format is undecided, and
// against a Remove orphans answer that left orphans alone for each reason, and prints one JSON object with what the
// page showed.  tests/test_parity_ids_js.py reads it; it skips when node is missing (the container the unit tests
// run in has none).
//   node tests/js/parity_ids.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, pageEsc } from './dom.mjs';

const STATIC = process.argv[2];
const src = fs.readFileSync(path.join(STATIC, 'parity.js'), 'utf8');
const between = (from, to) => { const at = src.indexOf(from); return src.slice(at, src.indexOf(to, at)); };
const esc = pageEsc(path.join(STATIC, 'parity.js'));
const WHY = 'discovery waits: whether hass_demo keeps the ids ... (<b>x</b>)';
const out = {};

function page() {
  const els = {}, logs = [];
  return { $: s => (els[s] ??= Object.assign(new El('div'), { id: s.slice(1) })), logs, log: m => logs.push(m) };
}
{
  const p = page();
  const status = { ok: true, running: 'demo', tag: '1.0', health: 'ok', mqtt_connected: true, parent_configured: true, discovery_enabled: false,
                   discovery_devices: 0, ids_undecided: WHY };
  const cstatus = new Function('$', 'post', 'esc', between('async function cstatus()', "$('#cenable')") + '\nreturn cstatus;')(p.$, async () => status, esc);
  await cstatus();
  out.cutover_status = { shown: p.$('#c1').textContent, bold: p.$('#c1').querySelectorAll('b').length };
}
{
  const p = page();
  const answer = { ok: false, ids_undecided: true, error: `id format undecided: nothing is compared until it is decided (${WHY})` };
  const fetch = async () => ({ json: async () => answer });
  const check = new Function('$', 'fetch', 'esc', 'renderParity', 'let P=null;\n' + between('async function check()', "$('#pcheck')") + '\nreturn check;')(
    p.$, fetch, esc, () => { throw new Error('nothing is rendered'); });
  out.parity = { result: await check(), shown: p.$('#psum').textContent };
}
{
  const p = page();
  const answer = { ok: true, removed: ['sensor.mine'], skipped: ['sensor.x', 'sensor.gone', 'sensor.dark'],
                   not_ours: ['sensor.x'], not_on_broker: ['sensor.gone'], unreadable: ['sensor.dark'] };
  const code = between('const ORPH_LEFT=', 'async function cstatus()');
  const P = { orphans: ['sensor.mine', 'sensor.x', 'sensor.gone', 'sensor.dark'].map(e => ({ parent_entity_id: e })) };
  new Function('$', 'post', 'log', 'confirm', 'check', 'P', code)(p.$, async () => answer, p.log, () => true, () => {}, P);
  await p.$('#porph').onclick();
  out.remove_orphans = { logs: p.logs };
}
console.log(JSON.stringify(out));
