// SPDX-License-Identifier: AGPL-3.0-only
// The counted texts (`line.places`, `filter.quantity-chip`, `length.hours`) are
// `<key>.one` and `<key>.other`: one for exactly 1, other for 0, 2 and more,
// the rule English and Norwegian share. tests/test_labels_s13b.py holds the
// server's side with the same table.
import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { lengthWords } from './add.js';
import { label, plural, pluralForm, setLabels } from './labels.js';

afterEach(() => setLabels({}));

// count, then the form, then what each counted text says for it in English.
const TABLE = [
  [0, 'other', '0 × bed', '0 × bed', '0 h'],
  [1, 'one', '1 × bed', '1 × bed', '1 h'],
  [2, 'other', '2 × bed', '2 × bed', '2 h'],
];

test('one for exactly 1 and other for 0 and 2', () => {
  for (const [count, form] of TABLE) assert.equal(pluralForm(count), form);
});

test('each counted text has both forms and fills the count', () => {
  for (const [count, , places, chip, hours] of TABLE) {
    assert.equal(plural('line.places', count, { word: 'bed' }), places);
    assert.equal(plural('filter.quantity-chip', count, { word: 'bed' }), chip);
    assert.equal(plural('length.hours', count), hours);
  }
});

test('the organization\'s own text for a form is used for that form only', () => {
  setLabels({ 'length.hours.one': '{count} time', 'length.hours.other': '{count} timer' });
  assert.equal(plural('length.hours', 1), '1 time');
  assert.equal(plural('length.hours', 0), '0 timer');
  assert.equal(plural('length.hours', 2), '2 timer');
  assert.equal(lengthWords(60), '1 time');
  assert.equal(lengthWords(150), '2 timer 30 min');
});

test('a text with < and & is shown as it is written: React renders it as text', () => {
  setLabels({ 'login.title': 'Sign <in> & out', 'login.code-label': 'A & B <b>' });
  const html = renderToStaticMarkup(React.createElement('p', null, label('login.title'), ' ', label('login.code-label')));
  assert.equal(html, '<p>Sign &lt;in&gt; &amp; out A &amp; B &lt;b&gt;</p>');
});
