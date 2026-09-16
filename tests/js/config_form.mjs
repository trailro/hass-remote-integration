// Runs field() and collect() out of static/config.js against a DOM small enough
// to keep here, and prints one JSON object per scenario.  The unit tests read
// that output; they skip when node is missing (the container has none).
//   node tests/js/config_form.mjs <path to config.js>
import fs from 'fs';
import { El, Option, document, pageEsc } from './dom.mjs';  // .value is a string there, as in a browser: that is what hid F15

const src = fs.readFileSync(process.argv[2], 'utf8');
const code = src.slice(src.indexOf('function optionsOf('), src.indexOf('function clearErrors('));
const esc = pageEsc(process.argv[2]);
const { field, collect } = new Function('document', 'esc', 'Option', code + '\nreturn {field, collect};')(
  document, esc, Option);

function submit(schemaField, touch) {  // what the page would send for a form the user did not touch (or touched so)
  const form = new El('div');
  const wrap = form.appendChild(field(schemaField, {}, null));
  if (touch) touch(wrap);
  return collect(form);
}
const type = text => wrap => { wrap._custom.value = text; };  // what the user puts in the custom_value box
const items = wrap => wrap._el.querySelectorAll('[data-item]');  // the inputs of a list of strings
const multiText = extra => ({ name: 'words', selector: { text: { multiple: true, ...extra } } });

const scenarios = {
  duration_ms: () => submit({ name: 'delay', selector: { duration: { enable_millisecond: true } }, default: { milliseconds: 500 } }),
  duration_ms_not_offered: () => submit({ name: 'delay', selector: { duration: {} }, default: { milliseconds: 500 } }),
  duration_plain: () => submit({ name: 'delay', selector: { duration: {} }, default: { hours: 1, minutes: 30 } }),
  list_multiple: () => submit({ name: 'choices', selector: { select: { mode: 'list', multiple: true, options: ['a', 'b', 'c'] } }, default: ['a', 'b'] }),
  list_multiple_scalar_default: () => submit({ name: 'choices', selector: { select: { mode: 'list', multiple: true, options: ['a', 'b'] } }, default: 'a' }),
  list_single: () => submit({ name: 'choice', selector: { select: { mode: 'list', options: ['a', 'b'] } }, default: 'b' }),
  dropdown_multiple: () => submit({ name: 'choices', selector: { select: { multiple: true, options: ['a', 'b'] } }, default: ['b'] }),
  // an option value that is not a string goes back as the schema offered it, not as the HTML string
  dropdown_single_number: () => submit({ name: 'choice', selector: { select: { options: [{ value: 1, label: 'one' }, { value: 2, label: 'two' }] } }, default: 2 }),
  text_number_default: () => submit({ name: 'word', selector: { text: {} }, default: 5 }),
  list_single_quotes: () => submit({ name: 'choice', selector: { select: { mode: 'list', options: ['say "hi" & <bye>', 'b'] } }, default: 'say "hi" & <bye>' }),
  // the stub itself: .value is what a browser would hold, or a scenario can pass on a value no browser sends
  stub_coerces_values: () => {
    const input = document.createElement('input'), area = document.createElement('textarea'), num = document.createElement('input');
    const sel = document.createElement('select'); sel.appendChild(new Option('one', 1)); sel.appendChild(new Option('two', 2));
    input.value = ['one', 'two']; area.value = null; num.type = 'number'; num.value = '1.'; sel.value = 2;
    const blank = document.createElement('select'); blank.appendChild(new Option('first', 'f'));
    return { input: input.value, textarea: area.value, number: num.value, select: sel.value, option: sel.options[0].value, select_first: blank.value };
  },

  // F10: HA's cv.time_period_dict takes floats, so a fractional default has to be submittable
  duration_fractional_seconds: () => submit({ name: 'delay', selector: { duration: {} }, default: { seconds: 0.5 } }),
  duration_fractional_minutes: () => submit({ name: 'delay', selector: { duration: {} }, default: { minutes: 1.5 } }),
  duration_part_step: () => { const w = field({ name: 'delay', selector: { duration: {} } }, {}, null); return { seconds: String(w._parts.seconds.step) }; },

  // F11: custom_value -- a default the options do not list is kept, and a new one can be typed
  custom_multi_dropdown: () => submit({ name: 'choices', selector: { select: { options: ['a', 'b'], multiple: true, custom_value: true } }, default: ['a', 'custom'] }),
  custom_multi_list: () => submit({ name: 'choices', selector: { select: { options: ['a', 'b'], multiple: true, custom_value: true, mode: 'list' } }, default: ['a', 'custom'] }),
  custom_multi_typed: () => submit({ name: 'choices', selector: { select: { options: ['a', 'b'], multiple: true, custom_value: true } }, default: ['a'] }, type('x, y')),
  custom_single_default: () => submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true } }, default: 'custom' }),
  custom_single_list_default: () => submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true, mode: 'list' } }, default: 'custom' }),
  custom_single_typed: () => submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true } }, default: 'a' }, type('typed')),
  no_custom_box_without_custom_value: () => ({ has: !!field({ name: 'choice', selector: { select: { options: ['a', 'b'] } } }, {}, null)._custom }),

  // F14: a single custom value is the whole box -- a comma is part of it
  custom_single_comma: () => submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true } }, default: 'a' }, type('Smith, John')),
  custom_single_list_comma: () => submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true, mode: 'list' } }, default: 'a' }, type('Smith, John')),
  custom_single_blank_box: () => submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true } }, default: 'b' }, type('   ')),
  custom_multi_comma: () => submit({ name: 'choices', selector: { select: { options: ['a', 'b'], multiple: true, custom_value: true } }, default: [] }, type('Smith, John')),

  // F15: a multiple text selector is a list of strings, whatever the strings hold
  text_multiple_empty: () => submit({ ...multiText(), default: [] }),
  text_multiple_none: () => submit(multiText()),
  text_multiple_one: () => submit({ ...multiText(), default: ['one'] }),
  text_multiple_several: () => submit({ ...multiText(), default: ['one', 'two', 'three'] }),
  text_multiple_comma: () => submit({ ...multiText(), default: ['Smith, John', 'two'] }),
  text_multiple_scalar_default: () => submit({ ...multiText(), default: 'one' }),
  text_multiple_typed: () => submit({ ...multiText(), default: [] }, w => {  // the empty row, then "add" twice
    items(w)[0].value = 'a, b'; w._after.onclick(); items(w)[1].value = 'c'; w._after.onclick(); }),
  text_multiple_removed: () => submit({ ...multiText(), default: ['one', 'two', 'three'] }, w => {
    const row = items(w)[1].parentNode; row.children.find(c => c.tag === 'button').onclick(); }),
  text_multiple_blank_rows_dropped: () => submit({ ...multiText(), default: ['one', '', '  '] }),
  text_multiple_multiline: () => { const w = field({ ...multiText({ multiline: true }), default: ['x'] }, {}, null); return { tag: items(w)[0].tag }; },
  text_multiple_password: () => { const w = field({ ...multiText({ type: 'password' }), default: ['x'] }, {}, null); return { type: items(w)[0].type }; },
  text_single_unchanged: () => submit({ name: 'word', selector: { text: {} }, default: 'Smith, John' }),
};
// one scenario that throws is reported under its own name instead of taking the others down with it
const out = {};
for (const [key, run] of Object.entries(scenarios)) {
  try { out[key] = run(); } catch (e) { out[key] = { error: String(e && e.stack || e) }; }
}
console.log(JSON.stringify(out));
