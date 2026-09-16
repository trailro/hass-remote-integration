// Runs the callers of post()/del() that took any answer for a success -- mqtt.js Republish and Reconnect,
// parity.js cstatus(), config.js Abort, a patch's Delete and a config entry's Delete and Reload, system.js
// Clear of an imported backup -- against the error object post() and del() hand back for a failed request, and
// prints one JSON object with what each page showed.  tests/test_r11_pages_js.py reads it; it skips when node
// is missing (the container the unit tests run in has none).
//   node tests/js/r11_pages.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, document, pageEsc } from './dom.mjs';

const STATIC = process.argv[2];
const read = name => fs.readFileSync(path.join(STATIC, name), 'utf8');
const between = (src, from, to) => { const at = src.indexOf(from); return src.slice(at, src.indexOf(to, at)); };
const esc = pageEsc(path.join(STATIC, 'hri.js'));
const FAILED = { ok: false, error: 'HTTP 500: 500 Internal Server Error', message: 'HTTP 500: 500 Internal Server Error' };
const settle = () => new Promise(r => setTimeout(r, 0));

// a table whose header row the page keeps: the stub has no :not()
class Table extends El {
  querySelectorAll(sel) { return sel.startsWith('tr:not') ? [] : super.querySelectorAll(sel); }
}

function page(tables = []) {
  const els = {}, logs = [], sent = [];
  const $ = s => (els[s] ??= Object.assign(tables.includes(s) ? new Table('table') : new El('div'), { id: s.slice(1) }));
  const failed = async url => { sent.push(url); return FAILED; };
  return { $, logs, sent, log: m => logs.push(m), post: failed, del: failed };
}
const out = {};

{
  const p = page();
  const code = between(read('mqtt.js'), "$('#mqrepub').onclick", 'setInterval(mqtt');
  new Function('$', 'post', 'log', 'mqtt', code)(p.$, p.post, p.log, async () => {});
  await p.$('#mqrepub').onclick();
  await p.$('#mqreconn').onclick();
  out.mqtt = { logs: p.logs };
}
{
  const p = page();
  const cstatus = new Function('$', 'post', 'esc', between(read('parity.js'), 'async function cstatus()', "$('#cenable')") + '\nreturn cstatus;')(p.$, p.post, esc);
  await cstatus();
  out.cutover_status = { shown: p.$('#c1').textContent };
}
{
  const p = page();
  const code = between(read('config.js'), "$('#abort').onclick", 'async function entries()');
  const flowNow = new Function('$', 'del', 'log', 'clearTimeout', 'let flow={id:"F1",kind:"config"}, PROGRESS_T=0;\n' + code + '\nreturn () => flow;')(
    p.$, p.del, p.log, () => {});
  p.$('#stepcard').hidden = false;
  await p.$('#abort').onclick();
  out.abort = { logs: p.logs, sent: p.sent, flow: flowNow(), stepcard_hidden: p.$('#stepcard').hidden };
}
{
  const p = page(['#entries']);
  const fetch = async url => ({ json: async () => url === 'api/entries'
    ? [{ domain: 'demo', title: 'Demo', state: 'loaded', version: 1, entry_id: 'e1', supports_options: false }] : [] });
  const entries = new Function('$', 'post', 'log', 'fetch', 'document', 'esc', 'confirm', 'render', 'DOM',
    'let flow=null;\n' + between(read('config.js'), 'async function entries()', 'load().catch') + '\nreturn entries;')(
    p.$, p.post, p.log, fetch, document, esc, () => true, () => {}, 'demo');
  await entries();
  const [del] = p.$('#entries').querySelectorAll('button[data-a=delete]'), [reload] = p.$('#entries').querySelectorAll('button[data-a=reload]');
  await del.onclick(); await settle();
  const afterDelete = [...p.logs];
  await reload.onclick(); await settle();
  out.entries = { delete_logs: afterDelete, reload_logs: p.logs.slice(afterDelete.length), sent: p.sent };
}
{
  const p = page(['#plist']);
  const fetch = async () => ({ json: async () => ({ ok: true, patches: [{ name: 'fix.py', status: 'applied' }] }) });
  const loadPatches = new Function('$', 'post', 'fetch', 'document', 'esc', 'confirm', 'pedEdit', 'DOM', 'ST',
    between(read('config.js'), 'async function loadPatches()', "$('#pupload')") + '\nreturn loadPatches;')(
    p.$, p.post, fetch, document, esc, () => true, () => {}, 'demo', { running: null });
  await loadPatches();
  const [button] = p.$('#plist').querySelectorAll('button[data-n]');
  await button.onclick(); await settle();
  out.patch_delete = { shown: p.$('#pmsg').textContent, sent: p.sent };
}
{
  const p = page();
  const code = between(read('system.js'), "$('#imclear').onclick", "$('#imapplyall')");
  const kept = new Function('$', 'post', 'confirm', 'imRender', 'let IMS={domains:{}}, IMSEL={dom:"demo"};\n' + code + '\nreturn () => ({IMS, IMSEL});')(
    p.$, p.post, () => true, () => {});
  await p.$('#imclear').onclick();
  const state = kept();
  out.import_clear = { shown: p.$('#immsg').textContent, inspected_kept: state.IMS !== null && state.IMSEL !== null };
}
console.log(JSON.stringify(out));
