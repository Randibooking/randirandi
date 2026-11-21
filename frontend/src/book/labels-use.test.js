// SPDX-License-Identifier: AGPL-3.0-only
// Every word the member pages show is a label (randirandi/locale/en.json), and
// every label is shown: the keys the code uses exist, the keys that exist are
// used (the server's refusal and state keys by the server), and no member JSX
// holds visible or spoken text of its own.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { labelKeys } from './labels.js';

const here = new URL('./', import.meta.url);
const sources = readdirSync(here)
  .filter((name) => /\.jsx?$/.test(name) && !name.endsWith('.test.js'))
  .map((name) => ({ name, text: readFileSync(new URL(name, here), 'utf8') }));

// The sign-in pages are drawn outside this folder (frontend/src/main.jsx and
// login/screen.js); their keys count as used, and their texts are the sign-in
// card's, which an administrator's screens share: the JSX scan below stays on
// the member pages.
const usage = [...sources, ...['../main.jsx', '../login/screen.js'].map((name) => ({ name, text: readFileSync(new URL(name, here), 'utf8') }))];

const keys = labelKeys();
const spaces = new Set(keys.filter((key) => key.includes('.')).map((key) => key.slice(0, key.indexOf('.'))));
const withoutComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1');

// What a source uses: whole keys, and prefixes where the key is built (a
// template such as `view.${key}` uses every key that starts with "view.").
export function usesOf(text) {
  const code = withoutComments(text);
  const exact = new Set();
  const prefixes = new Set();
  for (const found of code.matchAll(/\b(?:label|labelParts|serverWords)\(\s*(['"`])([^'"`$\n]+)\1/g)) exact.add(found[2]);
  for (const found of code.matchAll(/\b(?:label|labelParts)\(\s*`([a-z-]+\.)\$\{/g)) prefixes.add(found[1]);
  // A key written down for later (a table of them): a quoted word that starts
  // with a namespace of the label set, such as 'state.booked'.
  for (const found of code.matchAll(/(['"])([a-z][a-z-]*)\.([a-z0-9.-]+)\1/g)) {
    if (spaces.has(found[2])) exact.add(`${found[2]}.${found[3]}`);
  }
  // A counted text: `plural('line.places', n)` uses both its forms, not the stem.
  for (const found of code.matchAll(/\bplural\(\s*(['"`])([^'"`$\n]+)\1/g)) {
    exact.delete(found[2]);
    exact.add(`${found[2]}.one`);
    exact.add(`${found[2]}.other`);
  }
  return { exact, prefixes };
}

// The server's own keys, the refusals and states of booking_words.py: the keys
// without a dot (tests/test_labels.py holds that they are exactly those).
const server = new Set(keys.filter((key) => !key.includes('.')));

// The words the server writes for a member, the calendar file and the emails:
// the keys in the namespaces `calendar`, `email`, `event`, `conditions`, `login` (the sign-in pages and mails) and
// `day-limit` (the refusal's counted texts); tests/test_labels.py holds that the server writes each, and only keys of
// the label set.
const written = new Set(keys.filter((key) => /^(calendar|email|event|conditions|login|day-limit)\./.test(key)));

const LETTER = /\p{L}/u;

// The text of the template literal that opens at `code[start]` (a backtick),
// outside its `${...}` holes, which may hold templates of their own; and where
// it ends.
function templateAt(code, start) {
  let text = '';
  let depth = 0;
  let index = start + 1;
  while (index < code.length) {
    const char = code[index];
    if (depth === 0 && char === '`') return { text, end: index };
    if (char === '$' && code[index + 1] === '{') {
      depth += 1;
      index += 2;
    } else if (depth > 0 && char === '{') {
      depth += 1;
      index += 1;
    } else if (depth > 0 && char === '}') {
      depth -= 1;
      index += 1;
    } else if (depth > 0 && char === '`') {
      index = templateAt(code, index).end + 1;
    } else {
      if (depth === 0) text += char;
      index += 1;
    }
  }
  return { text, end: code.length };
}
const outsideHoles = (text) => text.replace(/\$\{[^}]*\}/g, '');

// Visible or spoken text written in JSX instead of given by `label`: a text
// between tags, a spoken attribute (aria-label, title, label, placeholder,
// alt), a string after `??`, `||` or `?` in a child. Symbols, digits and
// punctuation are allowed. Returns what it found, as short quotes.
export function textIn(text) {
  const code = withoutComments(text)
    .replace(/^import[\s\S]*?;\s*$/gm, '')
    .replace(/=>/g, '= >').replace(/>=/g, '> =');
  const found = [];
  // The spoken attributes, as a string or a template literal.
  for (const match of code.matchAll(/\b(?:aria-label|title|label|placeholder|alt|helperText)=\{?\s*(["'`])/g)) {
    const open = match.index + match[0].length - 1;
    if (match[1] === '`') {
      if (LETTER.test(templateAt(code, open).text)) found.push(match[0]);
    } else if (LETTER.test(code.slice(open + 1, code.indexOf(match[1], open + 1)))) {
      found.push(match[0]);
    }
  }
  // A string after `??`, `||` or `?` that reads as words: it starts with a
  // capital or holds a space ('primary' and 'flex' are values, not words).
  for (const match of code.matchAll(/(?:\?\?|\|\||[^?]\?(?!\.))\s*(["'`])([^"'`]*)\1/g)) {
    const words = outsideHoles(match[2]);
    if (LETTER.test(words) && /^\p{Lu}|^\p{L}.*\s/u.test(words.trim())) found.push(match[0]);
  }
  // A template literal given as a child: `{`text ${x}`}`.
  for (const match of code.matchAll(/(?<=[>}]\s*)\{\s*`([^`]*)`\s*\}/g)) {
    if (LETTER.test(outsideHoles(match[1]))) found.push(match[0]);
  }
  // Text between tags and braces; a run with code in it is not text.
  const bare = code.replace(/`(?:[^`\\]|\\.)*`/g, '``');
  for (const match of bare.matchAll(/(?<=[>}])([^<>{}]+)(?=[<{])/g)) {
    // The code's own words between elements are not text.
    const run = match[1].replace(/\b(?:return|else|const|let|var|if|for|case|default|new|typeof|export|function)\b/g, '');
    // `, held:` is an object's next key.
    if (LETTER.test(run) && !/[=&|?;()[\]]/.test(run) && !/^\s*,\s*\w+:\s*$/.test(run)) found.push(match[1].trim());
  }
  return found;
}

test('every key the member pages use is in the label set', () => {
  for (const { name, text } of usage) {
    const { exact, prefixes } = usesOf(text);
    for (const key of exact) assert.ok(keys.includes(key), `${name} uses "${key}", which is not a label`);
    for (const prefix of prefixes) {
      assert.ok(keys.some((key) => key.startsWith(prefix)), `${name} builds keys under "${prefix}", which has none`);
    }
  }
});

test('every key in the label set is used by the member pages or by the server', () => {
  const used = new Set();
  const prefixes = new Set();
  for (const { text } of usage) {
    const found = usesOf(text);
    found.exact.forEach((key) => used.add(key));
    found.prefixes.forEach((prefix) => prefixes.add(prefix));
  }
  const unused = keys.filter((key) => !used.has(key) && !server.has(key) && !written.has(key)
    && ![...prefixes].some((prefix) => key.startsWith(prefix)));
  assert.deepEqual(unused, []);
  assert.ok(server.size >= 16, 'the server\'s keys were read');
});

test('the member JSX holds no visible or spoken text of its own', () => {
  for (const { name, text } of sources.filter((source) => source.name.endsWith('.jsx'))) {
    assert.deepEqual(textIn(text), [], `${name} has text that is not a label`);
  }
});

test('the scan catches text written in JSX', () => {
  assert.ok(textIn('<Button>Show more</Button>').length);
  assert.ok(textIn('<Typography>Order {number}</Typography>').length);
  assert.ok(textIn('<Typography>{x} items</Typography>').length);
  assert.ok(textIn('<IconButton aria-label="Close" />').length);
  assert.ok(textIn('<IconButton aria-label={`${name} fewer`} />').length);
  assert.ok(textIn('<Chip label="Filter" />').length);
  assert.ok(textIn("<b>{view.total ?? 'Free'}</b>").length);
  assert.ok(textIn("<b>{pending ? 'Please wait' : label('x.y')}</b>").length);
});

test('the scan catches a template literal given as a child', () => {
  assert.ok(textIn('<Typography>{`From ${hour.label}`}</Typography>').length);
});

test('the scan lets symbols, values and labels through', () => {
  assert.deepEqual(textIn("<Typography>{label('place.me')}</Typography>"), []);
  assert.deepEqual(textIn('<Typography> · {group.count}</Typography>'), []);
  assert.deepEqual(textIn('<IconButton aria-label={`${a}, ${b}`} />'), []);
  assert.deepEqual(textIn('<Box component="img" alt="" />'), []);
  assert.deepEqual(textIn("const f = (a) => a > 0 && <Box sx={{ display: 'flex' }} />;"), []);
  assert.deepEqual(textIn('<Typography>–</Typography>'), []);
});

test('the key scan reads whole keys, tables of keys and built keys', () => {
  const { exact, prefixes } = usesOf("label('place.me'); const W = { a: 'state.booked' }; label(`view.${key}`); const c = 'text.secondary';");
  assert.deepEqual([...exact].sort(), ['place.me', 'state.booked']);
  assert.deepEqual([...prefixes], ['view.']);
});
