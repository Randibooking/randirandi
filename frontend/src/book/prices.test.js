// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { daysWords, priceBook, priceColumns, priceQuery } from './prices.js';

test('daysWords: runs of weekdays, the week a circle', () => {
  assert.equal(daysWords([0, 1, 2, 3, 4]), 'Monday to Friday');
  assert.equal(daysWords([5, 6]), 'Saturday and Sunday');
  assert.equal(daysWords([4]), 'Friday');
  assert.equal(daysWords([0, 1, 2, 3, 4, 5, 6]), 'Every day');
  assert.equal(daysWords([6, 0, 1, 2, 3]), 'Sunday to Thursday');
});

const tier = (up_to, paid_per, amount) => ({ up_to, paid_per, amount });
const hours = (from, to) => Array.from({ length: to - from }, (_, i) => `2026-10-26T${String(from + i).padStart(2, '0')}:00:00+01:00`);
const hall = (extra = {}) => ({
  id: 1, number: 1, asset: { id: 1, name: 'Hall' }, type: { id: 1, name: 'Hire' }, categories: [1, 2], period_type: 'flexible',
  with_payment: true, interval: 60, minimum_length: 60, maximum_length: 720,
  prices: { currency: 'NOK', categories: [
    { category: 1, tiers_go_by: 'length', groups: [], tiers: [tier(240, 240, 25000), tier(720, 60, 10000)] },
    { category: 2, tiers_go_by: 'length', groups: [], tiers: [tier(240, 240, 40000), tier(720, 60, 15000)] },
  ] },
  periods: [{ day: '2026-10-26', rows: hours(10, 22), end: '2026-10-26T22:00:00+01:00', category_hours: [] }],
  ...extra,
});
const categories = [{ id: 1, name: 'Private' }, { id: 2, name: 'Work' }];

test('priceBook: a section per type, a row per container, a column per category, examples priced in every column', () => {
  const [type] = priceBook([{ containers: [hall()] }], categories, []);
  assert.equal(type.name, 'Hire');
  assert.equal(type.night, false);
  const [block] = type.blocks;
  assert.equal(block.days, 'Monday');
  assert.equal(block.hours, '10:00–22:00');
  assert.deepEqual(block.cells.map((cell) => cell.offered), [true, true]);
  assert.deepEqual(block.cells[0].lines, ['250 kr for the first 4 h', 'Then 100 kr per started 1 h', 'At most 12 h']);
  assert.equal(block.examples[0].hours, '10:00–14:00');
  assert.deepEqual(block.examples[0].totals, ['250 kr', '400 kr']);
});

test('priceBook: a category the container does not take is not offered; free is No charge', () => {
  const only = hall({ categories: [1] });
  const [type] = priceBook([{ containers: [only] }], categories, []);
  assert.deepEqual(type.blocks[0].cells.map((cell) => cell.offered), [true, false]);
  assert.equal(type.blocks[0].examples[0].totals[1], null);
  const free = hall({ with_payment: false });
  assert.deepEqual(priceBook([{ containers: [free] }], categories, [])[0].blocks[0].cells[0].lines, ['No charge']);
});

test('priceBook: types in the organization\'s order', () => {
  const night = hall({ id: 2, number: 2, type: { id: 2, name: 'Night' } });
  const names = (order) => priceBook([{ containers: [hall(), night] }], categories, order).map((type) => type.name);
  assert.deepEqual(names(['Night', 'Hire']), ['Night', 'Hire']);
  assert.deepEqual(names(['Hire', 'Night']), ['Hire', 'Night']);
});

test('priceColumns: every standard category as sent, none dropped for what the member holds', () => {
  const sent = [{ id: 1, name: 'Private', icon: 'person' }, { id: 2, name: 'Work', icon: 'work' }];
  assert.deepEqual(priceColumns(sent), sent);
  assert.deepEqual(priceColumns(undefined), []);
});

test('priceQuery: the standard role\'s answer for the column\'s category', () => {
  assert.deepEqual(priceQuery({ asset: 1, category: 9 }, 2, '2026-10-25', 14),
    { asset: 1, category: 2, start: '2026-10-25', days: 14, prices: 1 });
});

test('priceBook: a category only the standard role holds has its prices in its column', () => {
  const [type] = priceBook([{ containers: [hall({ categories: [1, 2] })] }], categories, []);
  assert.deepEqual(type.blocks[0].cells.map((cell) => cell.offered), [true, true]);
  assert.deepEqual(type.blocks[0].examples[0].totals, ['250 kr', '400 kr']);
});
