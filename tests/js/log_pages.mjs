// Runs static/logs.js (fetchLogs) and static/logfiles.js (loadFiles, load) against canned
// answers of the API and a DOM small enough to keep here, and prints one JSON object with
// what each page asked for and showed.  tests/test_log_pages_js.py reads it; it skips
// when node is missing (the container the unit tests run in has none).
//   node tests/js/log_pages.mjs <path to static/>
import fs from 'fs';
import path from 'path';

const STATIC = process.argv[2];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

class El {
  constructor() {
    this.value = ''; this.textContent = ''; this.checked = false; this.rows = [];
    this.scrollTop = 0; this.clientHeight = 100; this.scrollHeight = 100; this.options = [];
  }
  addEventListener() {}
  querySelectorAll() { return []; }
  appendChild() {}
  get children() { return this.rows; }
  set innerHTML(html) {
    this.html = String(html);
    this.rows = [];
    this.options = [...this.html.matchAll(/<option value="([^"]*)"\s*(selected)?>/g)].map(m => ({ value: m[1], selected: !!m[2] }));
    if (this.options.length) this._value = (this.options.find(o => o.selected) || this.options[0]).value;
    this.addRows(this.html);
  }
  get innerHTML() { return this.html || ''; }
  addRows(html) {
    for (const m of html.matchAll(/<span class="l [^"]*">([\s\S]*?)<\/span>(?=<span class="l |$)/g)) {
      const row = { text: m[1].replace(/<[^>]*>/g, ''), remove: () => { this.rows.splice(this.rows.indexOf(row), 1); } };
      this.rows.push(row);
    }
  }
  insertAdjacentHTML(_where, html) { this.addRows(html); }
  querySelector(sel) { return sel === 'select' && /<select/.test(this.html || '') ? (this._sel ??= new Select()) : null; }
}
class Select extends El {
  get value() { return this._value ?? ''; }
  set value(v) { this._value = v; }
}

function page(file, ids, answer) {
  const els = {};
  const $ = s => (els[s] ??= s === '#file' ? new Select() : new El());
  const requests = [], logs = [], rows = [];
  const document = { querySelector: $, querySelectorAll: () => [], createElement: () => { const e = new El(); rows.push(e); return e; } };
  const fetch = async (url) => {
    requests.push(url);
    const { status = 200, body } = await answer(new URL(url, 'http://hri'));
    // no body at all: an answer that is not JSON, which r.json() rejects on, as a browser's does
    return { ok: status < 400, status, json: async () => { if (body === undefined) throw new SyntaxError('Unexpected token < in JSON at position 0'); return body; } };
  };
  const src = fs.readFileSync(path.join(STATIC, file), 'utf8');
  const api = new Function('$', 'document', 'esc', 'fetch', 'setInterval', 'log', 'post',
    src + `\nreturn {${ids.join(',')}};`)($, document, esc, fetch, () => 0, m => logs.push(String(m)), async () => ({}));
  return { api, $, requests, logs, rows };
}

const settle = () => new Promise(r => setTimeout(r, 0));
const out = {};

// ----- logs.js: a follow past a page the search emptied ------------------------------------------
{
  const rec = (id, message) => ({ id, ts: '2026-09-16T10:00:00.000', level: 'INFO', logger: 'custom_components.probe', message, exc: null });
  const base = { capacity: '2 MB', path: '/config/integration_manager/process.log' };
  // what the server answers for each since_id: page 1..201 is 200 records that only matched inside a
  // password, all dropped by the search on the masked text; the cursor says they were examined
  const answers = {
    0: { records: [rec(1, 'needle: first')], truncated: false, cursor: 1 },
    1: { records: [], truncated: true, cursor: 201 },
    201: { records: [rec(202, 'needle: actual failure')], truncated: false, cursor: 202 },
  };
  const p = page('logs.js', ['fetchLogs'], url => {
    if (url.pathname === '/api/logs/loggers') return { body: [] };
    const since = Number(url.searchParams.get('since_id'));
    return { body: { ...base, ...(answers[since] || { records: [], truncated: false, cursor: since }) } };
  });
  p.$('#level').value = 'DEBUG'; p.$('#q').value = 'needle';
  await settle(); await settle();  // the page's own first load (loadGroups().then(fetchLogs(true)))
  p.requests.length = 0;
  await p.api.fetchLogs(true);
  for (let i = 0; i < 4; i++) await p.api.fetchLogs(false);
  out.logs_follow = {
    since_ids: p.requests.map(u => Number(new URL(u, 'http://hri').searchParams.get('since_id'))),
    shown: p.$('#out').rows.map(r => r.text),
  };
}

// ----- logfiles.js: two files that share a masked name -------------------------------------------
{
  let listing = [
    { id: 'id-alpha', name: 'logs/session-token=***', bytes: 1024, active: true, source: 'registry log_dir' },
    { id: 'id-beta', name: 'logs/session-token=***', bytes: 2048, active: true, source: 'registry log_dir' },
  ];
  const contents = { 'id-alpha': 'contents of alpha', 'id-beta': 'contents of beta' };
  const p = page('logfiles.js', ['loadFiles', 'load'], url => {
    if (url.pathname === '/api/log_files') return { body: listing };
    if (url.pathname === '/api/settings') return { body: {} };
    // the server resolves what the page sends against its listing, like LogFileTailView
    const id = url.searchParams.get('id'), name = url.searchParams.get('file');
    const hits = id ? listing.filter(f => f.id === id) : name ? listing.filter(f => f.name === name) : listing.slice(0, 1);
    if (hits.length !== 1) return { status: hits.length ? 409 : 404, body: { message: 'unknown file' } };
    return { body: { path: '/config/' + hits[0].name, bytes: hits[0].bytes, total_lines_scanned: 1, columns: [], format_error: null,
      lines: [{ raw: contents[hits[0].id], cells: null, color: null }] } };
  });
  const tb = () => (p.$('#tb').innerHTML.match(/<td[^>]*>([^<]*)<\/td>/) || [])[1];
  await settle(); await settle(); await settle();
  const opened = [];
  for (const o of p.$('#file').options) {
    p.$('#file').value = o.value;
    await p.api.load();
    opened.push({ option: o.value, shown: tb() });
  }
  // a restart gives every file a new id: the selection the page still holds is unknown to the server
  listing = listing.map(f => ({ ...f, id: f.id + '-restarted' }));
  contents['id-alpha-restarted'] = contents['id-alpha']; contents['id-beta-restarted'] = contents['id-beta'];
  p.$('#file').value = 'id-beta';
  await p.api.load();
  out.logfiles = { labels: p.$('#file').options.length, opened, after_restart: { selected: p.$('#file').value, shown: tb() } };
}

// ----- logs.js: a follower far behind reads on while the answers say truncated --------------------
{
  const rec = (id) => ({ id, ts: '2026-09-16T10:00:00.000', level: 'INFO', logger: 'custom_components.probe', message: 'line ' + id, exc: null });
  const base = { capacity: '2 MB', path: '/config/integration_manager/process.log' };
  const run = async (lastPage) => {
    // pages of 200 from since_id=1; every page up to lastPage is truncated (lastPage=Infinity: the server never catches up)
    const p = page('logs.js', ['fetchLogs'], url => {
      if (url.pathname === '/api/logs/loggers') return { body: [] };
      const since = Number(url.searchParams.get('since_id')), n = since ? Math.floor(since / 200) : 0;
      if (!since) return { body: { ...base, records: [rec(1)], truncated: false, cursor: 1 } };
      return { body: { ...base, records: [rec(since + 200)], truncated: n < lastPage, cursor: since + 200 } };
    });
    p.$('#level').value = 'DEBUG'; p.$('#q').value = ''; p.$('#follow').checked = true;
    await settle(); await settle();
    await p.api.fetchLogs(true);
    p.requests.length = 0;
    await p.api.fetchLogs(false);  // one tick of the 3 s timer
    return { reads: p.requests.length, last_since: Number(new URL(p.requests.at(-1), 'http://hri').searchParams.get('since_id')),
      notice: p.$('#out').rows.some(r => r.text.includes('more new lines than fit')) };
  };
  out.logs_catch_up = { behind: await run(3), never: await run(Infinity) };
}

// ----- logfiles.js: the selection after a restart, found again by its label ----------------------
{
  const run = async (listing, choose) => {
    const contents = Object.fromEntries(listing.map(f => [f.id, 'contents of ' + f.id]));
    const p = page('logfiles.js', ['loadFiles', 'load'], url => {
      if (url.pathname === '/api/log_files') return { body: listing };
      if (url.pathname === '/api/settings') return { body: {} };
      const hits = listing.filter(f => f.id === url.searchParams.get('id'));
      if (hits.length !== 1) return { status: 404, body: { message: 'unknown file' } };
      return { body: { path: '/config/' + hits[0].name, bytes: 1, total_lines_scanned: 1, columns: [], format_error: null,
        lines: [{ raw: contents[hits[0].id], cells: null, color: null }] } };
    });
    await settle(); await settle(); await settle();
    p.$('#file').value = choose;
    await p.api.load();
    listing = listing.map(f => ({ ...f, id: f.id + '-restarted' }));
    for (const f of listing) contents[f.id] = contents[f.id.replace('-restarted', '')];
    await p.api.load();
    return { selected: p.$('#file').value, shown: (p.$('#tb').innerHTML.match(/<td[^>]*>([^<]*)<\/td>/) || [])[1], note: p.$('#fmterr').textContent };
  };
  const file = (id, name) => ({ id, name, bytes: 1024, active: true, source: 'config root' });
  out.logfiles_restart = {
    unique: await run([file('a', 'first.log'), file('b', 'second.log')], 'b'),
    shared: await run([file('a', 'logs/session-token=***'), file('b', 'logs/session-token=***'), file('c', 'other.log')], 'b'),
  };
}

// ----- logs.js: a level the server refuses ------------------------------------------------------
// LogLevelView answers through json_message, which sends {"message": ...}; the page read only .error, so
// every refusal -- the root logger, an unknown level, the 50-logger cap -- reached the operator as "error: 400"
{
  const GROUPS = [{ name: 'custom_components', loggers: ['custom_components.demo'], levels: { 'custom_components.demo': 'INFO' }, counts: {} }];
  const run = async (refusal) => {
    const p = page('logs.js', ['loadGroups'], url => {
      if (url.pathname === '/api/logs/loggers') return { body: GROUPS };
      if (url.pathname === '/api/logs/level') return refusal;
      return { body: { capacity: '2 MB', path: '', records: [], truncated: false, cursor: 0 } };
    });
    await p.api.loadGroups();
    p.logs.length = 0;
    await p.rows.at(-1).querySelector('select').onchange({ target: { value: 'DEBUG' } });
    return p.logs;
  };
  out.log_level_refused = {
    message: await run({ status: 400, body: { message: 'the root logger is not yours to change: pick a logger below it' } }),
    error: await run({ status: 400, body: { error: 'unknown level FINE' } }),
    cap: await run({ status: 400, body: { message: 'already 50 loggers with a level of their own: reset one first' } }),
    not_json: await run({ status: 502 }),
    accepted: await run({ status: 200, body: { ok: true } }),
  };
}

console.log(JSON.stringify(out));
