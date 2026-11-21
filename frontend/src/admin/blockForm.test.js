// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BLOCK_WHEN, BLOCK_WORDS, blockFailure, blocksQuery, canBlock, firstStretch, layQuery, makeBody, pieceWords, stretchAsked,
} from './blockForm.js';

test('the periods are the drawing\'s four, Coming first', () => {
  assert.deepEqual(BLOCK_WHEN.map(([key]) => key), ['coming', 'past', 'day', 'period']);
});

test('the list query follows the Schedule\'s: dates only for a day or a period', () => {
  const filters = { when: 'coming', from: '2027-03-01', to: '2027-03-07', dir: 'asc', page: 0 };
  assert.deepEqual(blocksQuery(filters), { when: 'coming', q: undefined, order: 'earliest', page: 1 });
  assert.deepEqual(blocksQuery({ ...filters, when: 'past', dir: 'desc', page: 2 }),
    { when: 'past', q: undefined, order: 'latest', page: 3 });
  assert.deepEqual(blocksQuery({ ...filters, when: 'day' }),
    { when: 'day', from: '2027-03-01', q: undefined, order: 'earliest', page: 1 });
  // The wrong way round reads the same.
  assert.deepEqual(blocksQuery({ ...filters, when: 'period', from: '2027-03-07', to: '2027-03-01' }),
    { when: 'period', from: '2027-03-01', to: '2027-03-07', q: undefined, order: 'earliest', page: 1 });
});

test('a new block starts a week from today, 10:00 to 22:00', () => {
  assert.deepEqual(firstStretch('2027-02-26'), { start: '2027-03-05T10:00', end: '2027-03-05T22:00' });
});

test('the preview is asked for only when the stretch runs forwards on an asset', () => {
  const ok = { asset: 3, start: '2027-03-08T10:00', end: '2027-03-08T12:00' };
  assert.equal(stretchAsked(ok), true);
  assert.equal(stretchAsked({ ...ok, asset: '' }), false);
  assert.equal(stretchAsked({ ...ok, end: ok.start }), false);
  assert.equal(stretchAsked({ ...ok, end: '2027-03-08T09:00' }), false);
  assert.equal(stretchAsked({ ...ok, start: '' }), false);
  assert.deepEqual(layQuery({ ...ok, note: 'x' }), ok);
});

test('the make sends the note without the spaces round it', () => {
  assert.deepEqual(makeBody({ asset: 3, start: 'a', end: 'b', note: '  Deep clean ' }),
    { asset: 3, start: 'a', end: 'b', note: 'Deep clean' });
});

test('a piece reads its times and its booking type', () => {
  assert.equal(pieceWords({ start_date: '2027-03-08', start_time: '18:00', end_date: '2027-03-08', end_time: '22:00', booking_type: 'Room hire' }),
    '2027-03-08 18:00 to 2027-03-08 22:00 · Room hire');
});

test('Block is enabled only for a current preview with pieces and no refusal', () => {
  const laid = { pieces: [{}], booked: [], blocked: false, refusal: null };
  assert.equal(canBlock(laid, false), true);
  assert.equal(canBlock(laid, true), false);
  assert.equal(canBlock(null, false), false);
  assert.equal(canBlock({ ...laid, refusal: 'booked', booked: [4] }, false), false);
  assert.equal(canBlock({ ...laid, refusal: 'blocked', blocked: true }, false), false);
  assert.equal(canBlock({ ...laid, pieces: [] }, false), false);
});

test('every refusal the server answers has its words, and the rest a plain fallback', () => {
  for (const code of ['nothing', 'booked', 'blocked', 'busy', 'past', 'not-found']) assert.ok(BLOCK_WORDS[code]);
  assert.equal(blockFailure({ kind: 'refused', code: 'busy' }), BLOCK_WORDS.busy);
  assert.equal(blockFailure({ kind: 'refused', code: 'surprise' }), 'The block could not be saved.');
  assert.match(blockFailure({ kind: 'offline' }), /could not be reached/);
  assert.match(blockFailure({ kind: 'forbidden' }), /out of date/);
});
