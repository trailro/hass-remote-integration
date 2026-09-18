// Runs static/logfiles.js (the Download button) against canned answers and the real fetchDownload
// out of static/hri.js -- the helper the System page's Diagnostics zip and Memory snapshot share --
// and prints one JSON object with what the button asked for and what the browser saved.
// tests/test_log_download_js.py reads it; it skips when node is missing (the container the unit
// tests run in has none).
//   node tests/js/log_download.mjs <path to static/>
import fs from 'fs';
import path from 'path';

const STATIC = process.argv[2];
const read = name => fs.readFileSync(path.join(STATIC, name), 'utf8');
const between = (src, a, b) => { const i = src.indexOf(a), j = src.indexOf(b, i); return i < 0 || j < 0 ? null : src.slice(i, j); };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

class El {
  constructor() { this.value = ''; this.textContent = ''; this.checked = false; this.disabled = false; this.options = []; this.scrollTop = 0; this.clientHeight = 100; this.scrollHeight = 100; }
  addEventListener() {}
  querySelectorAll() { return []; }
  appendChild() {}
  remove() {}
  set innerHTML(html) {
    this.html = String(html);
    this.options = [...this.html.matchAll(/<option value="([^"]*)"\s*(selected)?>/g)].map(m => ({ value: m[1], selected: !!m[2] }));
    if (this.options.length) this._value = (this.options.find(o => o.selected) || this.options[0]).value;
  }
  get innerHTML() { return this.html || ''; }
}
class Select extends El {
  get value() { return this._value ?? ''; }
  set value(v) { this._value = v; }
}

// the shared helper, as the page gets it: hri.js is loaded on every page, so the Log files page
// calls the same function the System page's two download buttons call
const helper = between(read('hri.js'), 'async function fetchDownload(', 'function chipBar(');
const settle = () => new Promise(r => setTimeout(r, 0));
const out = {};

function run(listing, answer) {
  const els = {}, asked = [], alerts = [], saved = [];
  const $ = s => (els[s] ??= s === '#file' ? new Select() : new El());
  const document = { querySelector: $, querySelectorAll: () => [], createElement: () => new El(),
    // what the browser does with the <a> the helper makes: the click is the save
    body: { appendChild: a => { a.click = () => saved.push({ name: a.download, href: a.href }); } } };
  const pageFetch = async (url) => {
    const u = new URL(url, 'http://hri');
    if (u.pathname === '/api/log_files') return { ok: true, status: 200, json: async () => listing };
    if (u.pathname === '/api/settings') return { ok: true, status: 200, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => ({ path: '/config/x', bytes: 1, total_lines_scanned: 1, columns: [], lines: [], format_error: null }) };
  };
  const downloadFetch = async (url, opts) => {
    asked.push({ url, headers: (opts || {}).headers || {}, disabled: els['#download'].disabled });
    const a = answer(new URL(url, 'http://hri'));
    return { ok: a.status < 300, status: a.status,
             headers: { get: h => a.headers[h.toLowerCase()] ?? null },
             json: async () => JSON.parse(a.body), blob: async () => ({ text: a.body }) };
  };
  const fetchDownload = new Function('fetch', 'alert', 'URL', 'document', 'setTimeout',
    `${helper}\nreturn fetchDownload;`)(downloadFetch, m => alerts.push(m),
    { createObjectURL: blob => `blob:${blob.text}`, revokeObjectURL() {} }, document, () => {});
  const api = new Function('$', 'document', 'esc', 'fetch', 'setInterval', 'log', 'post', 'fetchDownload',
    read('logfiles.js') + '\nreturn {loadFiles, load};')($, document, esc, pageFetch, () => 0, () => {}, async () => ({}), fetchDownload);
  return { api, $, els, asked, alerts, saved };
}

const LISTING = [
  { id: 'id-alpha', name: 'logs/session-token=***', bytes: 1024, active: true, source: 'registry log_dir' },
  { id: 'id-beta', name: 'logs/session-token=***', bytes: 2048, active: true, source: 'registry log_dir' },
];
const OK = { status: 200, body: 'line one\nline two\n', headers: { 'content-disposition': 'attachment; filename="logs-session-token.log"' } };

{  // the selected file is downloaded, with the header a plain link cannot send
  const p = run(LISTING, () => OK);
  await settle(); await settle(); await settle();
  p.$('#file').value = 'id-beta';
  await p.$('#download').onclick();
  out.download = { asked: p.asked, saved: p.saved, alerts: p.alerts, disabled_after: p.$('#download').disabled };
}
{  // a refusal is shown, not saved
  const p = run(LISTING, () => ({ status: 404, body: '{"message": "unknown file"}', headers: {} }));
  await settle(); await settle(); await settle();
  await p.$('#download').onclick();
  out.refused = { saved: p.saved, alerts: p.alerts, disabled_after: p.$('#download').disabled };
}
{  // no log file at all: nothing to download
  const p = run([], () => OK);
  await settle(); await settle(); await settle();
  out.empty = { disabled: p.$('#download').disabled };
}
{  // a file whose name the server could not put in the header: the page's own fallback
  const p = run(LISTING, () => ({ ...OK, headers: {} }));
  await settle(); await settle(); await settle();
  await p.$('#download').onclick();
  out.fallback = { saved: p.saved };
}

console.log(JSON.stringify(out, null, 1));
