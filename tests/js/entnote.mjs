// Runs the Entities page's row painter (static/entities.js) for real over a handful of entities, and prints the
// MQTT column of each row: its text and every tag on it, with the class and the title the operator hovers.  What
// it is here for is the marker on date, time and datetime -- domains whose MQTT platforms exist only from Home
// Assistant 2026.5.  tests/test_entnote_js.py reads the JSON (and skips when node is missing, as the container
// the unit tests run in is).
//   node tests/js/entnote.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, document, pageEsc } from './dom.mjs';

const STATIC = process.argv[2];
const src = fs.readFileSync(path.join(STATIC, 'entities.js'), 'utf8');
const code = src.slice(src.indexOf('let rows=['), src.indexOf('function chips(){'));
const esc = pageEsc(path.join(STATIC, 'entities.js'));

// the page's own filters, left at what a freshly loaded page has: no search, every integration, any state
function page() {
  const els = {};
  const $ = s => (els[s] ??= Object.assign(new El(s === '#onlyDisc' ? 'input' : 'div'), { id: s.slice(1) }));
  $('#onlyDisc').type = 'checkbox';
  $('#tb').contains = () => false;  // no inline edit is in progress
  const api = new Function('$', 'esc', 'document',
    code + '\nreturn {render, load:r=>{rows=r}};')($, esc, { ...document, activeElement: null });
  return { $, ...api };
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
console.log(JSON.stringify({ rows: out, painted: Object.keys(out).length }));
