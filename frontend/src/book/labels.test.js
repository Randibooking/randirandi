// SPDX-License-Identifier: AGPL-3.0-only
import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import {
  hasLabel, label, labelKeys, labelParts, plural, serverWords, setLabels,
} from './labels.js';

afterEach(() => setLabels({}));

test('a key without the organization\'s text says its default', () => {
  assert.equal(label('place.book'), 'Book');
  assert.equal(label('place.me'), 'My page');
});

test('the organization\'s text wins for its key; the rest stay default', () => {
  setLabels({ 'place.book': 'Reserve' });
  assert.equal(label('place.book'), 'Reserve');
  assert.equal(label('place.me'), 'My page');
});

test('placeholders are filled; one without a value stays as written', () => {
  assert.equal(label('stack.needs', { length: '2 h', time: '17:00' }), 'Needs 2 h free from 17:00');
  assert.equal(label('stack.needs', { length: '2 h' }), 'Needs 2 h free from {time}');
  assert.equal(label('filter.quantity-chip.other', { count: 0 }), '0 × {word}');
});

test('the organization\'s text is filled too, in its own order', () => {
  setLabels({ 'stack.needs': 'At {time} {length} is needed' });
  assert.equal(label('stack.needs', { length: '2 h', time: '17:00' }), 'At 17:00 2 h is needed');
});

test('a value that holds braces is not filled again', () => {
  assert.equal(label('stack.needs', { length: '{time}', time: '17:00' }), 'Needs {time} free from 17:00');
});

test('nothing is made from a word: a spot word goes in as given', () => {
  assert.equal(plural('line.places', 2, { word: 'seat' }), '2 × seat');
  assert.equal(plural('line.places', 3, { word: 'bed' }), '3 × bed');
});

test('an empty text, a text that is not a text and an unknown key in the page\'s labels are not used', () => {
  setLabels({ 'place.book': '', 'place.me': 7, 'no.such.key': 'x' });
  assert.equal(label('place.book'), 'Book');
  assert.equal(label('place.me'), 'My page');
  assert.equal(hasLabel('no.such.key'), false);
});

test('labels that are not a table are no labels', () => {
  for (const junk of [null, undefined, 'Reserve', 5, ['Reserve']]) {
    setLabels(junk);
    assert.equal(label('place.book'), 'Book');
  }
});

test('a key that is not in the label set is an error, not silence', () => {
  assert.throws(() => label('no.such.key'), /no label/);
  assert.throws(() => label('constructor'), /no label/);
});

test('labelParts gives the strings and the values as given, in the text\'s order', () => {
  const time = { clock: true };
  assert.deepEqual(labelParts('cart.held', { reference: 'K7Q4MD', time }), ['Reference ', 'K7Q4MD', ' is held for you for ', time, '.']);
  setLabels({ 'cart.held': '{time} left' });
  assert.deepEqual(labelParts('cart.held', { time }), [time, ' left']);
});

test('serverWords: the label of a server key, else the server\'s own text', () => {
  assert.equal(serverWords('taken', 'x'), 'taken');
  setLabels({ taken: 'already gone' });
  assert.equal(serverWords('taken', 'x'), 'already gone');
  assert.equal(serverWords('fewer-days', 'Ask for fewer days.'), 'Ask for fewer days.');
  assert.equal(serverWords(undefined), null);
  assert.equal(serverWords(null, 'x'), 'x');
});

test('the label set holds the keys of the server\'s refusals and states', () => {
  for (const key of ['not-offered', 'taken', 'does-not-fit-beside-cart', 'already-started', 'price-changed',
    'cart-full', 'other-currency', 'order-held', 'not-ready', 'cart-empty', 'too-large', 'busy', 'held', 'confirmed',
    'cancelled', 'not-completed', 'settling']) {
    assert.ok(labelKeys().includes(key), key);
  }
});
