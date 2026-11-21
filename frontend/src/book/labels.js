// SPDX-License-Identifier: AGPL-3.0-only
// The words the member pages show: the organization's text for a label key,
// otherwise the key's default English text (randirandi/locale/en.json, the one file
// that holds every key). Pure: no DOM.
// A text may hold named placeholders such as `{free}`; they are the key's, and
// `label` fills them. No word is made from another by grammar: nothing is added
// to a label or to a name, so a text is phrased around its placeholders.
import DEFAULTS from '../../../randirandi/locale/en.json' with { type: 'json' };

// The organization's own texts, set once from the page's config (`ui.labels`).
let own = {};

export function setLabels(labels) {
  own = labels && typeof labels === 'object' && !Array.isArray(labels) ? labels : {};
}

export const hasLabel = (key) => Object.hasOwn(DEFAULTS, key);

export const labelKeys = () => Object.keys(DEFAULTS);

const PLACEHOLDER = /\{([a-z][A-Za-z0-9_]*)\}/g;

// The text of a key with its placeholders filled by `values`. A placeholder
// without a value stays as written; a text is filled in one pass, so a value
// holding braces is never filled again.
export function label(key, values = {}) {
  if (!hasLabel(key)) throw new Error(`There is no label "${key}".`);
  const text = typeof own[key] === 'string' && own[key] !== '' ? own[key] : DEFAULTS[key];
  return text.replace(PLACEHOLDER, (found, name) => (Object.hasOwn(values, name) ? String(values[name]) : found));
}

// The same text as pieces, for a placeholder filled with an element: the
// strings between the placeholders and each value as given.
export function labelParts(key, values = {}) {
  const text = label(key, Object.fromEntries(Object.keys(values).map((name) => [name, `{${name}}`])));
  const parts = [];
  let last = 0;
  for (const found of text.matchAll(PLACEHOLDER)) {
    if (found.index > last) parts.push(text.slice(last, found.index));
    parts.push(Object.hasOwn(values, found[1]) ? values[found[1]] : found[0]);
    last = found.index + found[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

// A counted text: `<key>.one` for exactly 1, `<key>.other` for the rest (0, 2
// and more), the rule English and Norwegian share and the server's
// `labels.plural_form`. `{count}` is filled.
export const pluralForm = (count) => (count === 1 ? 'one' : 'other');

export const plural = (key, count, values = {}) => label(`${key}.${pluralForm(count)}`, { ...values, count });

// The text for a server key (a refusal's reason, an order's or booking's state):
// the label when the key is one, otherwise the server's own text. `values`
// fill the label's placeholders (a refusal's `left`).
export const serverWords = (key, fallback = null, values = {}) => (hasLabel(key) ? label(key, values) : fallback);
