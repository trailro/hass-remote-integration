// Runs page code the thirteenth review changed -- the radio groups of a config flow form (static/config.js) and
// the System page's Memory snapshot button (static/system.js) -- and prints one JSON object with what each did.
// tests/test_r13_pages_js.py reads it; it skips when node is missing (the container the unit tests run in has none).
//   node tests/js/r13_pages.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, Option, document, pageEsc, pageVcmp, pageConst } from './dom.mjs';

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
  const { field, collect } = new Function('document', 'esc', 'Option', 'valueList', code + '\nreturn {field, collect};')(
    document, pageEsc(path.join(STATIC, 'config.js')), Option, pageConst(path.join(STATIC, 'config.js'), 'valueList'));
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
  const code = between(read('hri.js'), 'async function fetchDownload(', 'function chipBar(');  // shared with the Log files page's Download
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
{  // N3 (follow-up): a Check answer that arrives after the selection changed, or after a newer Check, enables nothing
  const src = read('install.js');
  const code = [between(src, 'function domain()', 'async function options('), between(src, 'let CHECK=null', 'function invalidate('),
                between(src, 'function body(extra)', 'async function prepare(')].join('\n');
  const run = async (script) => {
    const els = {}, sel = { '#bdomain': '', '#bdom': 'demo', '#bref': '', '#brel': 'v1', '#bhafree': '', '#bha': '2026.8.3', '#brepo': '', '#bname': '' };
    const $ = q => els[q] || (els[q] = Object.assign(new El('div'), { value: sel[q] ?? '', disabled: false }));
    const pending = [];
    const post = (url, b) => new Promise(res => pending.push({ b, res }));
    const api = new Function('$', 'post', 'esc', 'renderPreflight', 'confirm',
      code.replace('$(\'#bcheck\').onclick=', 'const check=') + '\nreturn {check, state: () => ({CHECK, REPORT: typeof REPORT === "undefined" ? null : REPORT})};')(
      $, post, s => String(s), () => 'report', () => true);
    await script({ $, api, pending });
    return { check: api.state().CHECK, prepare_enabled: !$('#bprepare').disabled, msg: $('#bmsg').innerHTML || $('#bmsg').textContent, sent: pending.map(p => p.b.ref) };
  };
  const ok = id => ({ ok: true, check_id: id, report: { ok: true } });
  const flush = () => new Promise(r => setTimeout(r, 0));
  out.stale_check = {
    changed: await run(async ({ $, api, pending }) => { const p = api.check(); $('#brel').value = 'v2'; pending[0].res(ok('CHECK_V1')); await p; }),
    overtaken: await run(async ({ $, api, pending }) => { const p1 = api.check(); await flush(); const p2 = api.check(); pending[1].res(ok('CHECK_2')); await p2; pending[0].res(ok('CHECK_1')); await p1; }),
    current: await run(async ({ api, pending }) => { const p = api.check(); pending[0].res(ok('CHECK_V1')); await p; }),
  };
}
{  // C2: every list of versions a page shows, sorted as strings -- 2026.10.1 under 2026.8.4, v0.10.0 under v0.9.0
  const esc = pageEsc(path.join(STATIC, 'config.js'));
  const vcmp = pageVcmp(path.join(STATIC, 'config.js'));      // shared through hri.js
  class Table extends El { querySelectorAll(sel) { return sel.startsWith('tr:not') ? [] : super.querySelectorAll(sel); } }
  const TAGS = ['v0.8.4', 'v0.9.0', 'v0.10.0', 'v0.22.0'];    // the integration's git tags, in the store
  const versions = Object.fromEntries(TAGS.map(t => [t, { version: t.slice(1), installed_at: '2026-09-01T10:00' }]));
  const page = (tables = []) => { const els = {};
    return s => (els[s] ??= Object.assign(tables.includes(s) ? new Table('table') : new El('div'), { id: s.slice(1) })); };

  // the Config page's version store table
  const $c = page(['#vers']);
  const renderVersions = new Function('$', 'esc', 'document', 'vcmp', 'log', 'post', 'confirm', 'startIntegration', 'load', 'DOM',
    between(read('config.js'), 'function renderVersions(', "$('#relrefresh').onclick") + '\nreturn renderVersions;')(
    $c, esc, document, vcmp, () => {}, async () => ({ ok: true }), () => true, async () => ({ ok: true }), () => {}, 'demo');
  renderVersions({ versions, running: false, running_tag: null, previous_tag: null, pre_update_backup: '' });
  const config_page = $c('#vers').querySelectorAll('b').map(b => b.textContent);

  // the manager page's chips and its start selector
  const $i = page(['#inst']);
  const renderIntegrations = new Function('$', 'esc', 'document', 'vcmp', 'log', 'post', 'confirm', 'startIntegration',
    'status', 'mqttSummary', 'INSTALLED', 'RUN',
    between(read('index.js'), 'let LAST_FP=null;', 'async function mqttSummary(') + '\nreturn renderIntegrations;')(
    $i, esc, document, vcmp, () => {}, async () => ({ ok: true }), () => true, async () => ({ ok: true }),
    async () => {}, async () => {}, { demo: { versions, entries: [], running: false, name: 'Demo' } }, null);
  renderIntegrations({ installed: { demo: 1 }, running: null, updates: {} });
  const manager_chips = $i('#inst').querySelectorAll('.tag').map(t => t.textContent.trim());
  const manager_select = $i('#inst').querySelectorAll('option').map(o => o.textContent);

  // the Install page's Home Assistant selector
  const $b = page();
  const HA = { current: '2026.9.2', latest_stable: '2026.10.1', python: '3.14.7',
               recent: ['2026.10.1', '2026.10.0', '2026.9.2', '2026.9.0', '2026.8.4'], installed_venvs: ['2026.8.4', '2026.9.2'] };
  const options = new Function('$', 'esc', 'vcmp', 'fetch', 'domInfo', 'releases',
    between(read('install.js'), 'let OPT=null', 'let CHECK=null') + '\nreturn options;')(
    $b, esc, vcmp, async () => ({ json: async () => ({ registry: { demo: { repo: 'trailro/demo' } }, ha: HA, running: { domain: 'demo' } }) }),
    () => {}, () => {});
  await options();
  const install_page = $b('#bha').querySelectorAll('option').map(o => o.value);

  out.version_order = { config_page, manager_chips, manager_select, install_page,
                        vcmp: ['2026.10.1', '2026.8.4', 'v0.10.0', 'v0.9.0', '2026.10.0b0', '2026.10.0'].sort(vcmp) };
}

console.log(JSON.stringify(out));
