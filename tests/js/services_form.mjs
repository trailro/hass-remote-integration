// Runs the call form of static/services.js -- callForm() draws it, wireCall() wires and reads it -- against
// the DOM in dom.mjs, and prints what "Call service" would POST for each scenario.  Read by
// tests/test_camp_config_js.py, which skips when node is missing.
//   node tests/js/services_form.mjs <path to services.js>
import fs from 'fs';
import { El, document, pageEsc, pageConst } from './dom.mjs';
import { selector as customMulti, roundTrips, typeItems } from './select_roundtrip.mjs';

const src = fs.readFileSync(process.argv[2], 'utf8');
const code = src.slice(src.indexOf('function selKind('), src.indexOf('function chips('));
let posted = null;
const fetch = async (url, init) => { posted = JSON.parse(init.body); return { json: async () => ({ ok: true, ms: 1 }) }; };
const CSS = { escape: s => String(s).replace(/[^\w-]/g, c => '\\' + c) };
const { callForm, wireCall } = new Function('document', 'esc', 'CSS', 'fetch', 'confirm', 'valueList', code + '\nreturn {callForm, wireCall};')(
  document, pageEsc(process.argv[2]), CSS, fetch, () => true, pageConst(process.argv[2], 'valueList'));  // as on the page: static/hri.js is loaded first

async function call(fields, touch) {  // what the page POSTs for a form the user did not touch (or touched so)
  const svc = { name: 'probe', fields }, x = new El('td');
  x.innerHTML = callForm('demo', svc);
  wireCall(x, 'demo', svc);
  if (touch) touch(x);
  posted = null;
  await x.querySelector('#ct_go').onclick();
  return posted ? posted.data : { refused: x.querySelector('#ct_msg').textContent };
}
const typeInto = (name, text) => x => { x.querySelector(`#cf_${name}`).value = text; };
const items = (x, name) => x.querySelector(`#cf_${name}`).querySelectorAll('[data-item]');
const button = (x, name, sel) => x.querySelector(`#cf_${name}`).querySelectorAll(sel);
const words = (extra, more) => ({ words: { selector: { text: { multiple: true, ...extra } }, ...more } });

const scenarios = {
  // F14 on this page: a single select with custom_value is one text box, read whole
  custom_single_comma: () => call({ who: { selector: { select: { options: ['a'], custom_value: true } } } }, typeInto('who', 'Smith, John')),
  custom_multi_comma: () => call({ who: { selector: { select: { options: ['a'], multiple: true, custom_value: true } } } },
    x => typeItems(() => x.querySelector('#cf_who').querySelectorAll('[data-item]'), () => x.querySelector('#cf_who').querySelector('button[data-add]'), ['Smith, John'])),
  text_single_comma: () => call({ word: { selector: { text: {} } } }, typeInto('word', 'Smith, John')),

  // F15 on this page: a list of strings, one input per item
  text_multiple_empty: () => call(words({}, { default: [] })),
  text_multiple_empty_required: () => call(words({}, { required: true })),
  text_multiple_one: () => call(words({}, { default: ['one'] })),
  text_multiple_several: () => call(words({}, { example: ['one', 'two', 'three'] })),
  text_multiple_comma: () => call(words({}, { example: ['Smith, John', 'say "hi" & <bye>'] })),
  text_multiple_scalar_example: () => call(words({}, { example: 'one' })),
  text_multiple_typed: () => call(words({}), x => {  // the empty row, then "add" twice
    items(x, 'words')[0].value = 'a, b'; button(x, 'words', 'button[data-add]')[0].click();
    items(x, 'words')[1].value = 'c'; button(x, 'words', 'button[data-add]')[0].click(); }),
  text_multiple_removed: () => call(words({}, { example: ['one', 'two', 'three'] }), x => button(x, 'words', 'button[data-del]')[1].click()),
  text_multiple_multiline_added: () => call(words({ multiline: true }, { example: ['x'] }), x => {
    button(x, 'words', 'button[data-add]')[0].click(); items(x, 'words')[1].value = 'line 1\nline 2'; }),
  text_multiple_password: async () => ({ type: (() => { const x = new El('td'); x.innerHTML = callForm('demo', { name: 'probe', fields: words({ type: 'password' }) }); return items(x, 'words')[0].type; })() }),

  // F4 on this page: the same two words, and the mirror of what the config flow page did with them.  This page
  // kept "null" and "undefined" but stringified an example that was not there, so a field whose example (or
  // default) is null was drawn -- and sent -- as the word "null".
  word_single_custom_null: () => call({ who: { selector: { select: { options: ['a'], custom_value: true } }, example: 'null' } }),
  word_single_custom_undefined: () => call({ who: { selector: { select: { options: ['a'], custom_value: true } }, example: 'undefined' } }),
  word_multi_custom: () => call({ who: { selector: { select: { options: ['a'], multiple: true, custom_value: true } }, example: ['a', 'null', 'undefined'] } }),
  word_single_listed_no_custom: () => call({ who: { selector: { select: { options: ['null', 'undefined', 'b'] } }, example: 'null' } }),
  word_multi_listed_no_custom: () => call({ who: { selector: { select: { options: ['null', 'undefined', 'b'], multiple: true } }, example: ['null', 'undefined'] } }),
  word_single_unlisted_no_custom: () => call({ who: { selector: { select: { options: ['a', 'b'] } }, example: 'null' } }),

  // an example that is not there is no value at all, whatever the options are called
  missing_single_custom: () => call({ who: { selector: { select: { options: ['a'], custom_value: true } } } }),
  missing_multi_custom: () => call({ who: { selector: { select: { options: ['a'], multiple: true, custom_value: true } } } }),
  missing_multi_custom_null_example: () => call({ who: { selector: { select: { options: ['a'], multiple: true, custom_value: true } }, example: null } }),
  missing_multi_custom_null_default: () => call({ who: { selector: { select: { options: ['a'], multiple: true, custom_value: true } }, default: null } }),
  missing_multi_custom_null_in_list: () => call({ who: { selector: { select: { options: ['a'], multiple: true, custom_value: true } }, example: [null, 'a'] } }),
  missing_single_word_option: () => call({ who: { selector: { select: { options: ['null', 'b'] } }, example: null } }),
  missing_multi_word_option: () => call({ who: { selector: { select: { options: ['null', 'b'], multiple: true } }, example: null } }),
};

// F18: the round trips config_form.mjs runs too (an example here, as a service's catalog gives one; nothing sent is [])
for (const [key, fx] of Object.entries(roundTrips)) {
  scenarios[`roundtrip.${key}`] = async () => {
    const sent = await call({ names: { selector: { select: { ...customMulti, ...(fx.mode ? { mode: fx.mode } : {}) } }, example: fx.value } },
      fx.type && (x => typeItems(() => x.querySelector('#cf_names').querySelectorAll('[data-item]'), () => x.querySelector('#cf_names').querySelector('button[data-add]'), fx.type)));
    return sent.names ?? [];
  };
}
scenarios.custom_multi_default_not_example = async () => call({ names: { selector: { select: customMulti }, default: ['Smith, John', '  padded  '] } });

const out = {};
for (const [key, run] of Object.entries(scenarios)) {
  try { out[key] = await run(); } catch (e) { out[key] = { error: String(e && e.stack || e) }; }
}
console.log(JSON.stringify(out));
