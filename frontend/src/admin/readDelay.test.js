// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { SEARCH_WAIT, readDelay } from './readDelay.js';

const key = (query) => JSON.stringify(query);

test('the first ask of a list goes at once, with or without a search', () => {
  assert.equal(readDelay(null, key({ order: 'newest', page: 1 })), 0);
  assert.equal(readDelay(null, key({ q: 'alex', page: 1 })), 0);
});

test('a change of the search text is waited out', () => {
  assert.equal(readDelay(key({ page: 1 }), key({ q: 'a', page: 1 })), SEARCH_WAIT);
  assert.equal(readDelay(key({ q: 'a', page: 1 }), key({ q: 'al', page: 1 })), SEARCH_WAIT);
  assert.equal(readDelay(key({ q: 'al', page: 1 }), key({ page: 1 })), SEARCH_WAIT);
});

test('any other change, and the same query asked again, goes at once', () => {
  assert.equal(readDelay(key({ q: 'al', page: 1 }), key({ q: 'al', page: 2 })), 0);
  assert.equal(readDelay(key({ state: 'paid', page: 1 }), key({ state: 'held', page: 1 })), 0);
  assert.equal(readDelay(key({ q: 'al', page: 1 }), key({ q: 'al', page: 1 })), 0);
  assert.equal(readDelay(key({ asset: 1, start: 'a' }), key({ asset: 1, start: 'b' })), 0);
});

test('a query that is not an object does not stop the ask', () => {
  assert.equal(readDelay(key(null), key(null)), 0);
});
