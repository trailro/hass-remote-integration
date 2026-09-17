// Runs page code the thirteenth review changed -- the radio groups of a config flow form (static/config.js) and
// the System page's Memory snapshot button (static/system.js) -- and prints one JSON object with what each did.
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
{  // F6: Memory snapshot fetches with the header the endpoint requires, and saves the answer
  const code = between(read('system.js'), 'async function fetchDownload(', 'async function ha(');
  const line = read('system.js').split('\n').find(l => l.startsWith("$('#memsnap').onclick="));
  out.memsnap = {};
  for (const [name, answer] of Object.entries({
    ok: { status: 200, body: '{"rss_mb": 1}', headers: {} },
    busy: { status: 429, body: '{"message": "a memory probe is already running: try again when it has finished"}', headers: {} },
    broken: { status: 500, body: 'not json', headers: {} },
  })) {
    if (!code || !line) { out.memsnap[name] = null; continue; }
    const els = { '#memsnap': new El('button') }, sent = [], alerts = [], saved = [];
    const $ = s => els[s] ??= new El('div');
    const fetch = async (url, opts) => {
      sent.push({ url, headers: (opts || {}).headers || {}, disabled: els['#memsnap'].disabled });
      return { ok: answer.status < 300, status: answer.status, headers: { get: h => answer.headers[h.toLowerCase()] ?? null },
               json: async () => JSON.parse(answer.body), blob: async () => ({ text: answer.body }) };
    };
    const body = new El('body'), doc = { createElement: t => new El(t), body };
    body.appendChild = a => { a.click = () => saved.push({ name: a.download, href: a.href }); return El.prototype.appendChild.call(body, a); };
    const URL_ = { createObjectURL: blob => `blob:${blob.text}`, revokeObjectURL() {} };
    new Function('$', 'fetch', 'document', 'alert', 'URL', 'setTimeout', `${code}\n${line}`)(
      $, fetch, doc, m => alerts.push(m), URL_, () => {});
    await els['#memsnap'].onclick();
    out.memsnap[name] = { sent, alerts, saved, disabled_after: els['#memsnap'].disabled };
  }
}
console.log(JSON.stringify(out));
