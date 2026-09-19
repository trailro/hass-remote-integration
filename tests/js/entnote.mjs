// Runs the Entities page's row painter (static/entities.js) for real over a handful of entities, and prints the
// MQTT column of each row: its text and every tag on it, with the class and the title the operator hovers.  What
// it is here for is the marker on date, time and datetime -- domains whose MQTT platforms exist only from Home
// Assistant 2026.5.  Then the same painter, and the Devices page's (static/devices.js), over what has the focus
// when the table is repainted.  tests/test_entnote_js.py reads the JSON (and skips when node is missing, as the
// container the unit tests run in is).
//   node tests/js/entnote.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, document, pageEsc } from './dom.mjs';

const STATIC = process.argv[2];
const painter = file => {                                // the row painter of a page, up to its chip bar
  const src = fs.readFileSync(path.join(STATIC, file), 'utf8');
  return src.slice(src.indexOf('let rows=['), src.indexOf('function chips(){'));
};
const code = painter('entities.js');
const esc = pageEsc(path.join(STATIC, 'entities.js'));

// the page's own filters, left at what a freshly loaded page has: no search, every integration, any state.
// contains() walks the parents, as a browser does, and activeElement is writable: what has the focus when the
// page repaints is the whole point of the second half of this harness.
const CHECKBOXES = ['#onlyDisc', '#tree'], FIELDS = ['#q', ...CHECKBOXES];
function page(src = code) {
  const els = {};
  const $ = s => (els[s] ??= Object.assign(new El(FIELDS.includes(s) ? 'input' : 'div'), { id: s.slice(1) }));
  for (const s of CHECKBOXES) $(s).type = 'checkbox';
  const tb = $('#tb');
  tb.contains = n => { for (let e = n; e; e = e.parentNode) if (e === tb) return true; return false; };
  const doc = { ...document, activeElement: null };
  const api = new Function('$', 'esc', 'document',
    src + '\nreturn {render, load:r=>{rows=r}};')($, esc, doc);
  return { $, doc, ...api };
}

// the MQTT cell is the last one of a row: its plain text, then the tags it carries
const cell = tr => {
  const td = tr.elementChildren[tr.elementChildren.length - 1];
  const tags = td.querySelectorAll('.tag');
  return {
    text: td.textContent.trim(),
    tags: tags.map(t => ({ class: t.className, text: t.textContent, title: t.title })),
  };
};

const row = (entity_id, extra = {}) => ({
  entity_id, domain: entity_id.split('.')[0], name: entity_id, state: 'on', attributes: {},
  integration: 'demo', mqtt_topic: `hri/${entity_id.replace('.', '/')}/state`, discovery: 'native',
  last_updated: '2026-09-19T10:00:00+00:00', last_reported: '2026-09-19T10:00:00+00:00', mqtt_rule: {}, ...extra,
});

const ROWS = [
  row('date.oven_service'),
  row('time.wake_up'),
  row('datetime.next_clean'),
  row('sensor.kitchen_humidity'),
  row('button.restart'),
  row('sensor.update_date'),                                        // the word, not the domain
  row('weather.home', { discovery: 'mirror' }),                     // no MQTT platform at all: mirrored
  row('date.holiday', { mqtt_topic: null, discovery: false, mqtt_rule: { exclude: true } }),  // kept off MQTT
];

const p = page();
p.load(ROWS);
p.render();
const out = {};
for (const tr of p.$('#tb').querySelectorAll('tr')) out[tr.dataset.id] = cell(tr);

// ----- F14: what still has the focus when the table is repainted -------------------------------
// The operator opens a row and clicks one of its buttons.  In Chrome and Edge that button keeps the focus, and
// it lives inside #tb: the load() the successful action asks for, and every poll after it, used to find
// "something inside #tb is focused" and return -- leaving the old row on screen under an "ok", so a second
// rename posted to an id that no longer existed and a deleted device stayed in the list.
const entity = (id, extra = {}) => row(id, { unique_id: 'ureg-' + id, mqtt_topic: null, discovery: false, ...extra });
const device = (id, name) => ({ id, name, original_name: name, name_by_user: '', manufacturer: 'ACME', model: 'M42',
  identifier: id, integrations: ['demo'], entities: [], via_device_id: null, via_name: '', discovery_id: 'disc-' + id,
  unavailable: 0, identifiers: [], connections: [], config_entries: [] });

// the rows the table shows, by the key each page puts on them
const shown = (tb, cls) => tb.querySelectorAll('tr.' + cls).map(tr => tr.dataset.id || tr.textContent.trim().split('\n')[0]);

function repaint({ src, cls, first, second, focus }) {
  const p = page(src);
  p.load(first);
  p.render();
  p.$('#tb').querySelectorAll('tr.' + cls)[0].click();   // open the row: its edit boxes and buttons are in there
  const before = shown(p.$('#tb'), cls);
  p.doc.activeElement = focus ? (focus.startsWith('#') ? p.$(focus) : p.$('#tb').querySelector(focus)) : null;
  p.load(second);                                        // what the action answered, and the reload it asks for
  p.render();
  return { before, after: shown(p.$('#tb'), cls) };
}

const ENTITIES = { src: code, cls: 'e', first: [entity('sensor.kitchen_humidity')], second: [entity('sensor.humidity_kitchen')] };
const DEVICES = { src: painter('devices.js'), cls: 'd', first: [device('d1', 'Old oven')], second: [] };

const focus = {};
for (const [name, base] of Object.entries({ entities: ENTITIES, devices: DEVICES })) {
  focus[name] = {
    nothing_focused: repaint({ ...base, focus: null }),
    button_focused: repaint({ ...base, focus: 'button.act-delete' }),   // the button the operator just clicked
    input_focused: repaint({ ...base, focus: 'input.act-name' }),       // a name actually being typed: still protected
    search_focused: repaint({ ...base, focus: '#q' }),                  // a field outside the table stops nothing
  };
}
console.log(JSON.stringify({ rows: out, painted: Object.keys(out).length, focus }));
