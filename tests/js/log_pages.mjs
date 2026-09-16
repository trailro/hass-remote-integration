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
}
class Select extends El {
  get value() { return this._value ?? ''; }
  set value(v) { this._value = v; }
}

function page(file, ids, answer) {
  const els = {};
  const $ = s => (els[s] ??= s === '#file' ? new Select() : new El());
  const requests = [];
  const document = { querySelector: $, querySelectorAll: () => [], createElement: () => new El() };
  const fetch = async (url) => {
    requests.push(url);
    const { status = 200, body } = await answer(new URL(url, 'http://hri'));
    return { ok: status < 400, status, json: async () => body };
  };
  const src = fs.readFileSync(path.join(STATIC, file), 'utf8');
  const api = new Function('$', 'document', 'esc', 'fetch', 'setInterval', 'log', 'post',
    src + `\nreturn {${ids.join(',')}};`)($, document, esc, fetch, () => 0, () => {}, async () => ({}));
  return { api, $, requests };
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

console.log(JSON.stringify(out));
