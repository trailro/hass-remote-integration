// Runs field() and collect() out of static/config.js against a DOM small enough
// to keep here, and prints one JSON object per scenario.  The unit tests read
// that output; they skip when node is missing (the container has none).
//   node tests/js/config_form.mjs <path to config.js>
import fs from 'fs';

class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.style = {}; this.className = '';
    this.textContent = ''; this.type = ''; this.value = ''; this.checked = false; this.name = '';
    this.multiple = false; this.size = 0; this.min = null; this.max = null; this.step = null; this.options = [];
  }
  appendChild(child) { this.children.push(child); return child; }
  get selectedOptions() { return this.children.filter(c => c.selected); }
  set innerHTML(html) {  // only what field() writes: one <input ...> per label
    this.children = [];
    for (const m of String(html).matchAll(/<input\b([^>]*)>/g)) {
      const input = new El('input');
      for (const a of m[1].matchAll(/(\w+)(?:="([^"]*)")?/g)) {
        if (a[1] === 'checked') input.checked = true; else if (a[2] !== undefined) input[a[1]] = a[2];
      }
      this.children.push(input);
    }
  }
  get innerHTML() { return ''; }
  descendants() { return this.children.flatMap(c => [c, ...(c.descendants ? c.descendants() : [])]); }
  matches(sel) {
    if (sel === '[data-name]') return this.dataset.name !== undefined;
    const m = /^input\[type=(\w+)\](:checked)?$/.exec(sel);
    if (m) return this.tag === 'input' && this.type === m[1] && (!m[2] || this.checked);
    return false;
  }
  querySelectorAll(sel) {
    if (sel.startsWith(':scope > ')) return this.children.filter(c => c.matches(sel.slice(9)));
    return this.descendants().filter(c => c.matches(sel));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const document = { createElement: t => new El(t), createTextNode: t => ({ tag: '#text', textContent: t }) };
class Option extends El {
  constructor(label, value) { super('option'); this.textContent = label; this.value = value; this.selected = false; }
}

const src = fs.readFileSync(process.argv[2], 'utf8');
const code = src.slice(src.indexOf('function optionsOf('), src.indexOf('function clearErrors('));
const { field, collect } = new Function('document', 'esc', 'Option', code + '\nreturn {field, collect};')(
  document, s => String(s ?? ''), Option);

function submit(schemaField, touch) {  // what the page would send for a form the user did not touch
  const form = new El('div');
  const wrap = form.appendChild(field(schemaField, {}, null));
  // a browser keeps <select>.value on the selected option; the stub has to be told
  const el = wrap._el;
  if (el && el.tag === 'select' && !el.multiple) {
    const sel = el.children.find(o => o.selected);
    if (sel) el.value = sel.value;
  }
  if (touch) touch(wrap);
  return collect(form);
}
const type = text => wrap => { wrap._custom.value = text; };  // what the user puts in the custom_value box

const out = {
  duration_ms: submit({ name: 'delay', selector: { duration: { enable_millisecond: true } }, default: { milliseconds: 500 } }),
  duration_ms_not_offered: submit({ name: 'delay', selector: { duration: {} }, default: { milliseconds: 500 } }),
  duration_plain: submit({ name: 'delay', selector: { duration: {} }, default: { hours: 1, minutes: 30 } }),
  list_multiple: submit({ name: 'choices', selector: { select: { mode: 'list', multiple: true, options: ['a', 'b', 'c'] } }, default: ['a', 'b'] }),
  list_multiple_scalar_default: submit({ name: 'choices', selector: { select: { mode: 'list', multiple: true, options: ['a', 'b'] } }, default: 'a' }),
  list_single: submit({ name: 'choice', selector: { select: { mode: 'list', options: ['a', 'b'] } }, default: 'b' }),
  dropdown_multiple: submit({ name: 'choices', selector: { select: { multiple: true, options: ['a', 'b'] } }, default: ['b'] }),

  // F10: HA's cv.time_period_dict takes floats, so a fractional default has to be submittable
  duration_fractional_seconds: submit({ name: 'delay', selector: { duration: {} }, default: { seconds: 0.5 } }),
  duration_fractional_minutes: submit({ name: 'delay', selector: { duration: {} }, default: { minutes: 1.5 } }),
  duration_part_step: (() => { const w = field({ name: 'delay', selector: { duration: {} } }, {}, null); return { seconds: String(w._parts.seconds.step) }; })(),

  // F11: custom_value -- a default the options do not list is kept, and a new one can be typed
  custom_multi_dropdown: submit({ name: 'choices', selector: { select: { options: ['a', 'b'], multiple: true, custom_value: true } }, default: ['a', 'custom'] }),
  custom_multi_list: submit({ name: 'choices', selector: { select: { options: ['a', 'b'], multiple: true, custom_value: true, mode: 'list' } }, default: ['a', 'custom'] }),
  custom_multi_typed: submit({ name: 'choices', selector: { select: { options: ['a', 'b'], multiple: true, custom_value: true } }, default: ['a'] }, type('x, y')),
  custom_single_default: submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true } }, default: 'custom' }),
  custom_single_list_default: submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true, mode: 'list' } }, default: 'custom' }),
  custom_single_typed: submit({ name: 'choice', selector: { select: { options: ['a', 'b'], custom_value: true } }, default: 'a' }, type('typed')),
  no_custom_box_without_custom_value: (() => ({ has: !!field({ name: 'choice', selector: { select: { options: ['a', 'b'] } } }, {}, null)._custom }))(),
};
console.log(JSON.stringify(out));
