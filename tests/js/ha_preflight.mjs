// Runs the System page's Home Assistant dependency check (static/system.js) for real: the verdict it paints for
// each kind of answer, that a repeated selection does not ask again, that the 60 s refresh never starts a check,
// and what the Install button does with a refusal that carries needs_force.  Prints one JSON object;
// tests/test_ha_preflight_js.py reads it (and skips when node is missing, as the unit test container is).
//   node tests/js/ha_preflight.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, document, pageEsc } from './dom.mjs';

// the stub's innerHTML getter is empty by design: what the page painted is read off the nodes it built
const painted = el => ({ text: el.textContent, classes: el.children.map(c => c.className) });

const STATIC = process.argv[2];
const read = name => fs.readFileSync(path.join(STATIC, name), 'utf8');
const src = read('system.js');
const between = (from, to) => { const at = src.indexOf(from); return src.slice(at, src.indexOf(to, at)); };
const esc = pageEsc(path.join(STATIC, 'system.js'));

const BLOCKED = {
  version: '2026.1.0', ok: false, checked: true, missing: ['lru-dict==1.3.0'], warnings: [], notes: [],
  blockers: ['Home Assistant 2026.1.0 needs lru-dict==1.3.0, with no wheel for Python 3.14.7 on aarch64; this image has no compiler to build it'],
};
const CLEAN = { version: '2026.9.2', ok: true, checked: true, missing: [], blockers: [], warnings: [], notes: ['all 48 pinned requirements have a wheel'] };
const UNCHECKED = { version: '2026.6.0', ok: true, checked: false, missing: [], blockers: [], warnings: [], notes: ['could not check Home Assistant 2026.6.0: error: resolution-too-deep'] };

// the check block, with its own $ and post.  HA is what /api/ha last answered; null here, so no baseline
// arithmetic and no list repaint interferes with what the check itself paints (tests/js/ha_list.mjs has those)
function checker(answers, HA = null) {
  const els = {}, sent = [];
  const $ = s => (els[s] ??= Object.assign(new El(s === '#haver' ? 'select' : 'div'), { id: s.slice(1) }));
  const post = async (url, body) => { sent.push([url, body]); return answers.shift(); };
  const code = between('const HACHK={};', 'async function ha(force)');
  const api = new Function('$', 'post', 'esc', 'HA', 'vcmp',
    code + '\nreturn {haCheck, haCheckShow, HACHK};')($, post, esc, HA, (a, b) => (a === b ? 0 : a < b ? -1 : 1));
  return { $, sent, ...api };
}

const out = {};

{  // each kind of answer, painted
  const c = checker([{ ok: true, check: BLOCKED }, { ok: true, check: CLEAN }, { ok: true, check: UNCHECKED }, { ok: false, error: 'boom' }]);
  const shown = [];
  for (const v of ['2026.1.0', '2026.9.2', '2026.6.0', '2026.5.0']) {
    c.$('#haver').innerHTML = `<option selected>${v}</option>`;
    await c.haCheck(v);
    shown.push(painted(c.$('#hacheck')));
  }
  out.verdicts = { shown, sent: c.sent.map(([u, b]) => [u, b.version]) };
}
{  // the same version twice asks once; the periodic refresh only repaints what is already known
  const c = checker([{ ok: true, check: BLOCKED }]);
  c.$('#haver').innerHTML = '<option selected>2026.1.0</option>';
  await c.haCheck('2026.1.0');
  await c.haCheck('2026.1.0');
  c.$('#hacheck').innerHTML = '';
  c.haCheckShow('2026.1.0');       // what ha() calls on every refresh
  const repainted = painted(c.$('#hacheck'));
  c.haCheckShow('2025.1.0');       // a version nobody checked: nothing claimed about it
  out.cache = { calls: c.sent.length, repainted, unknown: painted(c.$('#hacheck')) };
}
{  // a check that finishes after the operator moved on does not paint over the new selection
  const c = checker([{ ok: true, check: BLOCKED }]);
  c.$('#haver').innerHTML = '<option selected>2026.9.2</option>';
  await c.haCheck('2026.1.0');
  out.stale = { shown: painted(c.$('#hacheck')), remembered: !!c.HACHK['2026.1.0'] };
}
{  // Install selected version: a refusal with needs_force asks, then repeats the request with force
  const els = {}, logs = [], sent = [], asked = [];
  const $ = s => (els[s] ??= Object.assign(new El(s === '#haver' ? 'select' : 'div'), { id: s.slice(1) }));
  $('#haplan').dataset.v = '';
  const answers = [{ ok: false, needs_force: true, check: BLOCKED, error: BLOCKED.blockers[0] }, { ok: true, backup: 'b.zip', warnings: [] }, { ok: true }];
  const post = async (url, body) => { sent.push([url, body]); return answers.shift(); };
  const code = between('async function haSet(', 'let HA=null;');
  const haSet = new Function('$', 'post', 'log', 'confirm', 'esc', 'HA', 'HACHK', 'haPlan', 'haCheckShow', 'setTimeout', 'vcmp', 'ha',
    code + '\nreturn haSet;')(
    $, post, m => logs.push(m), m => (asked.push(m), true), esc, { current: '2026.9.2', config_backups: {} }, {},
    () => 'keep', () => {}, () => {}, (a, b) => (a === b ? 0 : a < b ? -1 : 1), async () => {});
  await haSet('2026.1.0', 'update');
  out.force = { sent, forced: sent.length > 1 && sent[1][1].force === true, asked, logs };
}
{  // the same refusal, declined: nothing is forced and nothing restarts
  const els = {}, logs = [], sent = [];
  const $ = s => (els[s] ??= Object.assign(new El('div'), { id: s.slice(1) }));
  $('#haplan').dataset.v = '';
  const post = async (url, body) => { sent.push([url, body]); return { ok: false, needs_force: true, check: BLOCKED, error: BLOCKED.blockers[0] }; };
  const code = between('async function haSet(', 'let HA=null;');
  const answers = [true, false];
  const haSet = new Function('$', 'post', 'log', 'confirm', 'esc', 'HA', 'HACHK', 'haPlan', 'haCheckShow', 'setTimeout', 'vcmp', 'ha',
    code + '\nreturn haSet;')(
    $, post, m => logs.push(m), () => answers.shift(), esc, { current: '2026.9.2', config_backups: {} }, {},
    () => 'keep', () => {}, () => {}, (a, b) => (a === b ? 0 : a < b ? -1 : 1), async () => {});
  await haSet('2026.1.0', 'update');
  out.declined = { calls: sent.length, logs };
}
console.log(JSON.stringify(out));
