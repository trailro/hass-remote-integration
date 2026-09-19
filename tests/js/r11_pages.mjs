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
  const flowNow = new Function('$', 'del', 'log', 'clearTimeout', 'entries', 'let flow={id:"F1",kind:"config"}, PROGRESS_T=0;\n' + code + '\nreturn () => flow;')(
    p.$, p.del, p.log, () => {}, async () => {});
  p.$('#stepcard').hidden = false;
  await p.$('#abort').onclick();
  out.abort = { logs: p.logs, sent: p.sent, flow: flowNow(), stepcard_hidden: p.$('#stepcard').hidden };
}
{
  const p = page(['#entries']);
  const fetch = async url => ({ json: async () => url === 'api/entries'
    ? [{ domain: 'demo', title: 'Demo', state: 'loaded', version: 1, entry_id: 'e1', supports_options: false }] : [] });
  const entries = new Function('$', 'post', 'del', 'log', 'fetch', 'document', 'esc', 'confirm', 'render', 'releaseFlow', 'DOM',
    'let flow=null;\n' + between(read('config.js'), 'async function entries()', 'load().catch') + '\nreturn entries;')(
    p.$, p.post, p.del, p.log, fetch, document, esc, () => true, () => {}, async () => {}, 'demo');
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
// ----- F13: a config flow Home Assistant is still holding ---------------------------------------
// HA keeps a flow in progress until someone ends it, and refuses the next one for the same device with
// already_in_progress.  Start overwrote the page's flow without aborting it (the probe reached 51 in
// progress), and the list below hid every user-source flow, so an abandoned one could be neither
// continued nor aborted: only a restart, or a DELETE typed by hand, cleared it.
{
  // the Start button, against a server that keeps every flow it is asked for
  const flows = [];
  const els = {}, logs = [], sent = [];
  let opened = 0;
  const $ = s => (els[s] ??= Object.assign(new El('div'), { id: s.slice(1) }));
  const post = async (url) => { sent.push('POST ' + url); const id = 'F' + (++opened); flows.push(id); return { flow_id: id, type: 'form', step_id: 'user', data_schema: [] }; };
  const del = async (url) => { sent.push('DELETE ' + url); const i = flows.findIndex(f => url.endsWith(f)); if (i < 0) return { ok: false, error: 'unknown flow' }; flows.splice(i, 1); return { ok: true }; };
  const code = between(read('config.js'), "$('#start').onclick", 'async function entries()');
  const held = new Function('$', 'post', 'del', 'log', 'render', 'clearErrors', 'collect', 'clearTimeout', 'entries', 'DOM',
    'let flow=null, PROGRESS_T=0;\n' + code + '\nreturn () => flow;')(
    $, post, del, m => logs.push(String(m)), () => {}, () => {}, () => ({}), () => {}, async () => {}, 'demo');
  for (let i = 0; i < 3; i++) await $('#start').onclick();
  out.flow_start = { sent, opened, in_progress: flows, held: held(), logs };
}
{
  // the list under Config entries: what the page offers for each flow the server still holds
  const p = page(['#entries']);
  const progress = [
    { flow_id: 'F1', handler: 'demo', step_id: 'user', source: 'user', entry_id: null },              // abandoned: the page was reloaded
    { flow_id: 'F2', handler: 'demo', step_id: 'reauth_confirm', source: 'reauth', entry_id: 'e1' },
    { flow_id: 'F3', handler: 'other', step_id: 'user', source: 'user', entry_id: null },             // another integration's
  ];
  const sent = [];
  const fetch = async url => ({ json: async () => url === 'api/entries'
    ? [{ domain: 'demo', title: 'Demo', state: 'loaded', version: 1, entry_id: 'e1', supports_options: false }] : progress });
  const del = async url => { sent.push(url); const i = progress.findIndex(f => url.endsWith(f.flow_id)); if (i >= 0) progress.splice(i, 1); return { ok: true }; };
  const post = async url => { sent.push(url); return { type: 'form', flow_id: 'F1', step_id: 'user', data_schema: [] }; };
  const entries = new Function('$', 'post', 'del', 'log', 'fetch', 'document', 'esc', 'confirm', 'render', 'releaseFlow', 'DOM',
    'let flow=null;\n' + between(read('config.js'), 'async function entries()', 'load().catch') + '\nreturn entries;')(
    p.$, post, del, p.log, fetch, document, esc, () => true, () => {}, async () => {}, 'demo');
  const offered = () => p.$('#flowsprogress').elementChildren.map(b => b.textContent);
  await entries();
  const listed = offered();
  const abort = p.$('#flowsprogress').elementChildren.find(b => b.textContent.startsWith('Abort user'));
  if (abort) { await abort.onclick(); await settle(); }   // nothing to click when user flows are not listed at all
  out.flows_in_progress = { listed, sent, left: offered(), logs: p.logs };
}

console.log(JSON.stringify(out));
