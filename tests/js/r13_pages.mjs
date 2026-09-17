// Runs page code the thirteenth review changed -- the radio groups of a config flow form (static/config.js) -- and
// prints one JSON object with what each did.
// tests/test_r13_pages_js.py reads it; it skips when node is missing (the container the unit tests run in has none).
//   node tests/js/r13_pages.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, Option, document, pageEsc } from './dom.mjs';

const STATIC = process.argv[2];
const read = name => fs.readFileSync(path.join(STATIC, name), 'utf8');
const between = (src, a, b) => { const i = src.indexOf(a), j = src.indexOf(b, i); return i < 0 || j < 0 ? null : src.slice(i, j); };
const out = {};

// what a browser does with radios the stub leaves alone: in one form, a checked radio unchecks every other radio of
// its name -- when the markup is inserted (the later one wins) and when the user picks one
const radios = root => root.descendants().filter(e => e.tag === 'input' && e.type === 'radio');
const settle = root => { const seen = new Map(); for (const r of radios(root)) if (r.checked) { if (seen.has(r.name)) seen.get(r.name).checked = false; seen.set(r.name, r); } };
const pick = (root, input) => { for (const r of radios(root)) if (r !== input && r.name === input.name) r.checked = false; input.checked = true; };

{  // F5: two sections each holding a single-select list of the same name
  const code = between(read('config.js'), 'function optionsOf(', 'function clearErrors(');
  const { field, collect } = new Function('document', 'esc', 'Option', code + '\nreturn {field, collect};')(
    document, pageEsc(path.join(STATIC, 'config.js')), Option);
  const list = dflt => ({ name: 'mode', selector: { select: { mode: 'list', options: ['x', 'y'] } }, default: dflt });
  const form = new El('form');
  for (const [name, dflt] of [['first', 'x'], ['second', 'y']]) form.appendChild(field({ type: 'expandable', name, schema: [list(dflt)] }, {}, null));
  form.appendChild(field(list('y'), {}, null));
  settle(form);
  const groups = [...form.querySelectorAll(':scope > [data-name]')].map(w => [...new Set(radios(w).map(r => r.name))]);
  const untouched = collect(form);
  const second = form.querySelectorAll(':scope > [data-name]')[1];
  pick(form, radios(second).find(r => r.value === 'x'));
  out.sections = { groups, untouched, picked: collect(form) };
}
console.log(JSON.stringify(out));
