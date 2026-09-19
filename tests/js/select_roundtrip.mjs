// Round-trip fixtures for a select with multiple and custom_value, run by BOTH form harnesses
// (config_form.mjs against static/config.js, services_form.mjs against static/services.js), so the two pages
// answer the same list the same way.  Home Assistant takes each custom item as a string, as it is: a comma or
// surrounding spaces are part of the item.
//   value   the default (config flow) or example (service) the form is drawn with
//   type    items the user adds, each in a box of its own, before submitting
//   sent    what the form must send; without it, the value itself (an untouched form changes nothing)

export const selector = { options: ['alpha', 'beta'], multiple: true, custom_value: true };

export const roundTrips = {
  listed_only: { value: ['alpha'] },
  custom_with_comma: { value: ['alpha', 'Smith, John'] },
  custom_padded: { value: ['  padded  '] },
  custom_comma_and_padded: { value: ['alpha', 'beta', 'Smith, John', '  padded  '] },
  custom_markup: { value: ['say "hi" & <bye>', 'a,b,,c'] },
  list_mode: { value: ['beta', 'Smith, John', '  padded  '], mode: 'list' },
  typed_with_comma: { value: ['alpha'], type: ['Smith, John', '  padded  '], sent: ['alpha', 'Smith, John', '  padded  '] },
  typed_blank_is_nothing: { value: ['alpha'], type: ['   '], sent: ['alpha'] },
  typed_after_custom: { value: ['Smith, John'], type: ['Doe, Jane'], sent: ['Smith, John', 'Doe, Jane'] },
  // F4: "null" and "undefined" are strings HA takes like any other.  The config flow page stringified the value
  // before deciding whether there was one, so it threw these two away as if the default had been missing.
  word_null: { value: ['null'] },
  word_undefined: { value: ['undefined'] },
  words_after_a_listed_one: { value: ['alpha', 'null', 'undefined'] },
  typed_words: { value: ['alpha'], type: ['null', 'undefined'], sent: ['alpha', 'null', 'undefined'] },
};

// a list of custom items, one input each: type into the first empty one, else add a row with the page's own
// button (addButton) and type into that
export function typeItems(itemsOf, addButton, texts) {
  for (const text of texts) {
    let box = itemsOf().find(i => i.value === '');
    if (!box) { addButton().click(); box = itemsOf().find(i => i.value === ''); }
    box.value = text;
  }
}
