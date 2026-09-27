// Runs the MQTT page's broker line (static/mqtt.js) for each source of the base topic -- default, HRI_INSTANCE, the
// identity this volume remembered (with its Move button), an invalid HRI_INSTANCE -- clicks Move, and prints one JSON
// object with what each showed and what Move sent.  tests/test_mqtt_identity_js.py reads it; it skips when node is
// missing (the container the unit tests run in has none).
//   node tests/js/mqtt_identity.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, pageEsc } from './dom.mjs';

const STATIC = process.argv[2];
const src = fs.readFileSync(path.join(STATIC, 'mqtt.js'), 'utf8');
const lines = src.split('\n');
const broker = lines.filter(l => ["$('#mqbroker').innerHTML=", "if($('#mqmove'))", "if($('#mqidfmt1'))"].some(p => l.trimStart().startsWith(p))).join('\n');
const helpers = src.slice(src.indexOf('function mqIdentitySource('), src.indexOf('async function mqttConfigLoad('));
const base = { host: 'core-mosquitto', port: 1883, tls: false, identity_problem: null, identity_warning: null, identity_move_to: null, identity_instance: null };
const out = {};
for (const [name, s] of Object.entries({
  default: { ...base, has_identity: true, wanted_base_topic: 'hass_demo', identity_source: 'default' },
  instance: { ...base, has_identity: true, wanted_base_topic: 'hass_demo-garage', identity_source: 'instance', identity_instance: 'garage' },
  remembered: { ...base, has_identity: true, wanted_base_topic: 'hass_demo', identity_source: 'remembered', identity_instance: 'garage',
                identity_move_to: 'hass_demo-garage' },
  remembered_clear: { ...base, has_identity: true, wanted_base_topic: 'hass_demo', identity_source: 'remembered', identity_instance: 'garage',
                      identity_move_to: 'hass_demo-garage' },
  warned: { ...base, has_identity: true, wanted_base_topic: 'hass_demo', identity_source: 'remembered',
            identity_warning: "HRI_INSTANCE='<i>x</i>' is not an instance name (not used: demo keeps hass_demo, the identity this volume published it under)" },
  undecided: { ...base, has_identity: true, wanted_base_topic: 'hass_demo', identity_source: 'default', id_format_choosable: true,
               identity_warning: 'discovery waits: whether hass_demo keeps the ids it published with 0.26.0 or older is decided by ...' },
  undecided_new: { ...base, has_identity: true, wanted_base_topic: 'hass_demo', identity_source: 'default', id_format_choosable: true },
  invalid: { ...base, has_identity: false, wanted_base_topic: null, identity_source: 'invalid',
             identity_problem: "HRI_INSTANCE='<b>x</b>' is not an instance name: MQTT stays disconnected until it is corrected or removed" },
})) {
  const el = new El('td'), sent = [], confirms = [];
  const $ = sel => sel === '#mqbroker' ? el : el.querySelector(sel);
  const post = async (url, body) => { sent.push([url, body]); return { ok: true, from: 'hass_demo', to: body.to, identity: 'hass_demo', prefix: 'hass_demo_' }; };
  const run = new Function('$', 'esc', 's', 'post', 'log', 'confirm', 'mqtt', 'mqttConfigLoad',
    `${helpers}\n${broker}`);
  run($, pageEsc(path.join(STATIC, 'mqtt.js')), s, post, () => {}, m => { confirms.push(m); return true; }, async () => {}, () => {});
  const button = el.querySelector('#mqmove'), box = el.querySelector('#mqmoveclear');
  if (box && name.endsWith('_clear')) box.checked = true;
  if (button) { button.onclick(); await new Promise(r => setTimeout(r, 0)); }
  const formats = [el.querySelector('#mqidfmt1'), el.querySelector('#mqidfmt2')].filter(Boolean);
  if (formats.length) { formats[name.endsWith('_new') ? 1 : 0].onclick(); await new Promise(r => setTimeout(r, 0)); }
  out[name] = { text: el.textContent, bold: el.querySelectorAll('b').length, italic: el.querySelectorAll('i').length, button: button ? button.textContent : null,
                formats: formats.map(b => b.textContent), box: box ? box.checked : null, sent, confirms };
}
console.log(JSON.stringify(out));
