// A DOM small enough to keep in the tests, shared by the page harnesses next to it.  Only what the pages
// under test use, but where they use it, it behaves the way a browser does -- above all .value, which a
// browser keeps as a string: an input given an array holds "one,two", and that is what the page reads back.

import fs from 'fs';
import path from 'path';

// the pages' own esc() (static/hri.js, which every page loads), read out of the file next to the page under test
export function pageEsc(pagePath) {
  const line = /^const esc=.*;$/m.exec(fs.readFileSync(path.join(path.dirname(pagePath), 'hri.js'), 'utf8'));
  return new Function(`${line[0]}\nreturn esc;`)();
}

// a one-line helper of static/hri.js (which ui.py puts at the top of every page, so the pages use it without
// declaring it), read out of the file next to the page under test and handed to the code under test
export function pageConst(pagePath, name) {
  const src = fs.readFileSync(path.join(path.dirname(pagePath), 'hri.js'), 'utf8');
  const line = new RegExp(`^const ${name}=.*;$`, 'm').exec(src);
  if (!line) throw new Error(`static/hri.js declares no ${name} on a line of its own`);
  return new Function(`${line[0]}\nreturn ${name};`)();
}

// the pages' own version order (static/hri.js), read out of the same file: every page loads it
export function pageVcmp(pagePath) {
  const src = fs.readFileSync(path.join(path.dirname(pagePath), 'hri.js'), 'utf8');
  const lines = src.split('\n').filter(l => l.startsWith('const vparts=') || l.startsWith('const vcmp='));
  return new Function(`${lines.join('\n')}\nreturn vcmp;`)();
}

// controls whose .value is a string in a browser
const VALUED = new Set(['input', 'textarea', 'select', 'option']);
const VOID = new Set(['input', 'br', 'hr', 'img', 'meta', 'link']);
const FLOAT = /^-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?$/;  // HTML's valid floating-point number
const COLOR = /^#[0-9a-fA-F]{6}$/;
const camel = s => s.replace(/-([a-z])/g, (m, c) => c.toUpperCase());
const unescapeHTML = s => s.replace(/&(amp|lt|gt|quot|#39);/g, (m, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" }[e]));

export class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.style = {}; this.className = '';
    this.type = ''; this.checked = false; this.selected = false; this.name = ''; this.id = ''; this.parentNode = null;
    this.multiple = false; this.size = 0; this.min = null; this.max = null; this.step = null; this.listeners = [];
    this._text = '';
    if (VALUED.has(tag)) Object.defineProperty(this, 'value', { get: () => this.getValue(), set: v => this.setValue(v), enumerable: true });
    else this.value = '';
    this._value = tag === 'option' ? null : '';
  }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(t) { this.children = []; this._text = String(t ?? ''); }
  get parentElement() { return this.parentNode; }
  getValue() {
    if (this.tag === 'option') return this._value === null ? this.textContent : this._value;
    if (this.tag === 'select') {  // the selected option's value; a single select with none marked shows its first
      const opts = this.options, picked = opts.filter(o => o.selected);
      const o = this.multiple ? picked[0] : (picked[picked.length - 1] || opts[0]);
      return o ? o.value : '';
    }
    return this._value;
  }
  setValue(v) {
    if (this.tag === 'select') {  // selects the option with that value, or none
      const s = String(v); let found = false;
      for (const o of this.options) { o.selected = !found && o.value === s; if (o.selected) found = true; }
      return;
    }
    // input and textarea take null as empty ([LegacyNullToEmptyString]); everything else becomes String(v)
    let s = v === null && this.tag !== 'option' ? '' : String(v);
    if (this.tag === 'input' && this.type === 'number' && !FLOAT.test(s)) s = '';  // value sanitization
    if (this.tag === 'input' && this.type === 'color') s = COLOR.test(s) ? s.toLowerCase() : '#000000';
    this._value = s;
  }
  get options() { return this.descendants().filter(c => c.tag === 'option'); }
  get selectedOptions() { return this.options.filter(o => o.selected); }
  get elementChildren() { return this.children.filter(c => c.tag !== '#text'); }
  get previousElementSibling() {
    const sibs = this.parentNode ? this.parentNode.elementChildren : [];
    return sibs[sibs.indexOf(this) - 1] || null;
  }
  appendChild(child) { if (child.parentNode) child.remove(); child.parentNode = this; this.children.push(child); return child; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this); this.parentNode = null; }
  focus() {}
  addEventListener(type, fn) { this.listeners.push([type, fn]); }
  click() {  // bubbles: onclick and click listeners of the element and every ancestor
    const e = { target: this, type: 'click', preventDefault() {} };
    for (let n = this; n; n = n.parentNode) {
      if (n.onclick) n.onclick(e);
      for (const [t, fn] of n.listeners) if (t === 'click') fn(e);
    }
  }
  set innerHTML(html) { this.children = []; this._text = ''; for (const n of parse(html)) this.appendChild(n); }
  get innerHTML() { return ''; }
  insertAdjacentHTML(where, html) {
    if (where !== 'beforebegin') throw new Error(`insertAdjacentHTML ${where}: not in the stub`);
    const p = this.parentNode, at = p.children.indexOf(this), nodes = parse(html);
    nodes.forEach(n => { n.parentNode = p; });
    p.children.splice(at, 0, ...nodes);
  }
  descendants() { return this.elementChildren.flatMap(c => [c, ...c.descendants()]); }
  matches(sel) { return sel.split(',').some(s => matchCompound(this, s.trim())); }
  closest(sel) { for (let n = this; n; n = n.parentNode) if (n.matches(sel)) return n; return null; }
  querySelectorAll(sel) {
    if (sel.startsWith(':scope > ')) return this.elementChildren.filter(c => c.matches(sel.slice(9)));
    return this.descendants().filter(c => c.matches(sel));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

// tag, #id, .class, [attr], [attr=value] and :checked, joined without spaces
function matchCompound(el, sel) {
  const re = /^([a-z0-9]+)|#((?:\\.|[\w-])+)|\.([\w-]+)|\[([\w-]+)(?:=["']?([^\]"']*)["']?)?\]|:(checked)/gy;
  let m, pos = 0;
  while (pos < sel.length) {
    re.lastIndex = pos; m = re.exec(sel);
    if (!m) throw new Error(`selector not in the stub: ${sel}`);
    pos = re.lastIndex;
    if (m[1] && el.tag !== m[1]) return false;
    if (m[2] && el.id !== m[2].replace(/\\(.)/g, '$1')) return false;
    if (m[3] && !el.className.split(/\s+/).includes(m[3])) return false;
    if (m[4]) {
      const have = m[4].startsWith('data-') ? el.dataset[camel(m[4].slice(5))] : (el[m[4]] === '' || el[m[4]] == null ? undefined : String(el[m[4]]));
      if (have === undefined || (m[5] !== undefined && have !== m[5])) return false;
    }
    if (m[6] && !el.checked) return false;
  }
  return true;
}

export function parse(html) {
  const root = new El('#root'), stack = [root];
  const token = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|([^<]+)/g;
  let m;
  while ((m = token.exec(String(html)))) {
    const top = stack[stack.length - 1];
    if (m[1]) {  // closing tag
      const i = stack.map(e => e.tag).lastIndexOf(m[1].toLowerCase());
      if (i > 0) stack.length = i;
    } else if (m[2]) {
      const el = new El(m[2].toLowerCase());
      for (const a of m[3].matchAll(/([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
        const name = a[1].toLowerCase(), val = unescapeHTML(a[2] ?? a[3] ?? a[4] ?? '');
        if (name.startsWith('data-')) el.dataset[camel(name.slice(5))] = val;
        else if (name === 'class') el.className = val;
        else if (['checked', 'selected', 'multiple', 'disabled'].includes(name)) el[name] = true;
        else if (name !== 'style') el[name] = val;  // type comes before value in the pages' markup, as sanitization needs
      }
      top.appendChild(el);
      if (el.tag === 'textarea') {  // raw text up to its end tag: the initial value
        const end = String(html).indexOf('</textarea>', token.lastIndex);
        el.value = unescapeHTML(String(html).slice(token.lastIndex, end < 0 ? undefined : end));
        token.lastIndex = end < 0 ? String(html).length : end + '</textarea>'.length;
      } else if (!VOID.has(el.tag)) stack.push(el);
    } else if (m[4]) {
      const t = new El('#text'); t._text = unescapeHTML(m[4]); top.appendChild(t);
    }
  }
  return root.children.map(c => { c.parentNode = null; return c; });
}

export const document = { createElement: t => new El(t), createTextNode: t => { const n = new El('#text'); n._text = String(t); return n; } };
export class Option extends El {
  constructor(label, value) { super('option'); this.textContent = label; if (value !== undefined) this.value = value; this.selected = false; }
}
