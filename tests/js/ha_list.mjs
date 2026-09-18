// Runs the System page's Home Assistant version list (static/system.js) for real: what the selector holds by
// default, the mark each version carries, that "show all versions" asks for the rest, that a selection
// survives the 60 s refresh, and that painting the page never starts a check.  Prints one JSON object;
// tests/test_ha_list_js.py reads it (and skips when node is missing, as the unit test container is).
//   node tests/js/ha_list.mjs <path to static/>
import fs from 'fs';
import path from 'path';
import { El, pageEsc } from './dom.mjs';

const STATIC = process.argv[2];
const src = fs.readFileSync(path.join(STATIC, 'system.js'), 'utf8');
const between = (from, to) => { const at = src.indexOf(from); return src.slice(at, src.indexOf(to, at)); };
const esc = pageEsc(path.join(STATIC, 'system.js'));

const BLOCKED = {
  version: '2026.9.1', ok: false, checked: true, missing: ['lru-dict==1.3.0'], warnings: [], notes: [],
  blockers: ['Home Assistant 2026.9.1 needs lru-dict==1.3.0, with no wheel for Python 3.14.7 on aarch64'],
};
const CLEAN = { version: '2026.9.0', ok: true, checked: true, missing: [], blockers: [], warnings: [], notes: ['all 48 pinned requirements have a wheel'] };

// what GET /api/ha answers: the newest few releases plus everything this box has (2025.3.1 is an old venv on
// the volume, far outside the newest ten), and the verdicts the server already had cached
const ANSWER = {
  current: '2026.9.2', python: '3.14.7', in_venv: true, latest_stable: '2026.9.2', latest_published: '2026-09-01',
  update_available: false, checked_at: '2026-09-18T10:00:00', apt: null, last_error: '', pending: false,
  previous: '2026.8.3', desired: '2026.9.2', baseline: '2026.8.3', recent_n: 10, versions_total: 1420,
  installed_venvs: ['2025.3.1', '2026.8.3', '2026.9.2'], config_backups: {},
  versions: ['2025.3.1', '2026.8.3', '2026.8.4', '2026.9.0', '2026.9.1', '2026.9.2'],
  verdicts: { '2026.9.0': CLEAN, '2026.9.1': BLOCKED },
};
const EVERYTHING = { ...ANSWER, all_versions: ['2014.1.0', '2021.6.0', ...ANSWER.versions] };

// the version-list block with its own $, post and fetch; HA lives inside it, as it does on the page
function page(answers) {
  const els = {}, sent = [], fetched = [];
  const tag = s => (s === '#haver' ? 'select' : s === '#haall' ? 'input' : 'div');
  const $ = s => (els[s] ??= Object.assign(new El(tag(s)), { id: s.slice(1) }));
  $('#haall').type = 'checkbox';
  const post = async (url, body) => { sent.push([url, body]); return answers.shift(); };
  const fetch = async (url) => { fetched.push(url); return { json: async () => answers.shift() }; };
  const code = 'let HA=null;\n' + between('const vparts=', 'function haPlan(');
  const api = new Function('$', 'post', 'esc', 'fetch', 'haPlan',
    code + '\nreturn {ha, haOptions, haMark, haCheck, HACHK};')($, post, esc, fetch, () => {});
  const shown = () => $('#haver').options.map(o => ({ value: o.value, text: o.textContent, selected: o.selected }));
  return { $, sent, fetched, shown, ...api };
}

const out = {};

{  // the default list, painted from one GET
  const p = page([ANSWER]);
  await p.ha();
  out.default = { options: p.shown(), note: p.$('#halistnote').textContent, fetched: p.fetched, checks: p.sent.length };
}
{  // show all versions: the same page asks for the rest and keeps everything it had
  const p = page([ANSWER, EVERYTHING]);
  await p.ha();
  p.$('#haall').checked = true;
  await p.ha();
  out.all = { options: p.shown().map(o => o.value), note: p.$('#halistnote').textContent, fetched: p.fetched, checks: p.sent.length };
}
{  // a refresh forces PyPI and keeps "show all"; the operator's selection survives the repaint
  const p = page([ANSWER, EVERYTHING]);
  await p.ha();
  p.$('#haver').value = '2026.8.4';
  p.$('#haall').checked = true;
  await p.ha(true);
  out.refresh = { url: p.fetched[1], picked: p.$('#haver').value, selected: p.shown().filter(o => o.selected).map(o => o.value) };
}
{  // a version the server has no verdict for is only checked when the operator asks, and the answer becomes its mark
  const p = page([ANSWER, { ok: true, check: { ...CLEAN, version: '2026.8.4', notes: ['all 51 pinned requirements have a wheel'] } }]);
  await p.ha();
  const before = p.shown().find(o => o.value === '2026.8.4').text;
  await p.haCheck('2026.8.4');
  out.checked = { before, after: p.shown().find(o => o.value === '2026.8.4').text, sent: p.sent.map(([u, b]) => [u, b.version]) };
}
{  // a version below the image's baseline is refused without a pip run, however it is reached
  const p = page([ANSWER]);
  await p.ha();
  const verdict = await p.haCheck('2025.3.1');
  out.baseline = { verdict, sent: p.sent.length, painted: p.$('#hacheck').textContent };
}
console.log(JSON.stringify(out));
