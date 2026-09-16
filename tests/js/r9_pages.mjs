// Runs post()/del() out of static/hri.js and pollProgress() out of static/config.js against canned
// answers, and prints one JSON object with what they sent and drew.  tests/test_r9_web.py reads it;
// it skips when node is missing (the container the unit tests run in has none).
//   node tests/js/r9_pages.mjs <path to static/>
import fs from 'fs';
import path from 'path';

const STATIC = process.argv[2];
const read = name => fs.readFileSync(path.join(STATIC, name), 'utf8');
const out = {};

// ----- hri.js: the shared request helpers
const hri = read('hri.js');
const helpers = hri.slice(hri.indexOf('async function', hri.indexOf('const esc=')), hri.indexOf('const log='));
function helpersWith(answer) {
  const seen = [];
  const fetch = async (url, opts) => { seen.push(opts || {}); const { status, text } = answer(url);
    return { status, statusText: '', ok: status < 400, text: async () => text, json: async () => JSON.parse(text) }; };
  return { seen, ...new Function('fetch', helpers + '\nreturn {post, del};')(fetch) };
}
{
  const h = helpersWith(() => ({ status: 200, text: '{"ok": true, "n": 1}' }));
  out.json_answer = await h.post('api/x', {});
  await h.del('api/x');
  out.post_headers = h.seen[0].headers; out.del_headers = h.seen[1].headers;
  let plain;
  try { plain = await helpersWith(() => ({ status: 500, text: '500 Internal Server Error' })).post('api/x', {}); }
  catch (e) { plain = { threw: String(e) }; }
  out.plain_500 = plain;
}

// ----- config.js: the progress poll of a config flow
const cfg = read('config.js');
const pollCode = cfg.slice(cfg.indexOf('let PROGRESS_T'), cfg.indexOf("$('#start').onclick"));
function progressPage() {
  const timers = [], posts = [], state = { renders: 0, answer: null };
  const el = () => ({ textContent: '' });
  const els = {};
  const $ = s => (els[s] ??= el());
  const post = async url => { posts.push(url); return state.answer ? state.answer : { type: 'form' }; };
  const api = new Function('$', 'post', 'render', 'progressTitle', 'setTimeout', 'clearTimeout',
    'let flow=null;\n' + pollCode + '\nreturn {pollProgress, setFlow: f => { flow = f; }};')(
    $, post, () => { state.renders++; }, r => `In progress: ${r.step_id}`,
    (fn) => { timers.push(fn); return timers.length; }, () => {});
  const fire = async () => { const t = timers.splice(0); for (const fn of t) await fn(); };
  return { api, posts, state, fire };
}
{
  // a timer of flow A fires after flow B started
  const p = progressPage();
  p.api.setFlow({ id: 'A', kind: 'config' }); p.api.pollProgress();
  p.api.setFlow({ id: 'B', kind: 'config' });
  await p.fire();
  out.timer_after_new_flow = { posts: p.posts, renders: p.state.renders };
}
{
  // flow A's answer arrives after flow B started
  const p = progressPage();
  const a = { id: 'A', kind: 'config' };
  p.api.setFlow(a); p.api.pollProgress();
  let release; p.state.answer = new Promise(r => { release = r; });
  const firing = p.fire();
  await new Promise(r => setImmediate(r));
  p.api.setFlow({ id: 'B', kind: 'config' });
  release({ type: 'form' });
  await firing;
  out.answer_after_new_flow = { posts: p.posts, renders: p.state.renders };
}
{
  // the flow that asked: polled and drawn
  const p = progressPage();
  p.api.setFlow({ id: 'A', kind: 'config' }); p.api.pollProgress();
  await p.fire();
  out.same_flow = { posts: p.posts, renders: p.state.renders };
}
console.log(JSON.stringify(out));
