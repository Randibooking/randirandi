// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { addressOf, calendarQuery, filterOf, pickOf, readAddress } from './filter.js';

const container = (extra = {}) => ({
  id: 1, number: 1, with_payment: false, categories: [1, 2], max_quantity: 1,
  period_type: 'fixed', consumption_mode: 'exclusive', interval: 60, minimum_length: 60, maximum_length: 60,
  ...extra,
});
const type = (id, extra = {}, containers = [container()]) => ({
  id, name: `Type ${id}`, description: '', categories: [1, 2], quantity_for: [], containers, ...extra,
});
const asset = (id, extra = {}) => ({
  id, name: `Asset ${id}`, description: '', max_quantity: 1, space: null, types: [type(10 + id)], ...extra,
});
const choices = (extra = {}) => ({
  categories: [{ id: 1, name: 'Private' }, { id: 2, name: 'Work' }],
  category: 1,
  assets: [asset(1, { description: 'By the sea' })],
  spaces: [],
  ...extra,
});
const spaced = (booking_page) => choices({
  assets: [asset(1), asset(2, { space: 7 }), asset(3, { space: 7 })],
  spaces: [{ id: 7, name: 'Harbour', booking_page: booking_page }],
});

test('nothing to book is null', () => {
  assert.equal(filterOf(choices({ assets: [] })), null);
  assert.equal(filterOf(choices({ categories: [] })), null);
  assert.equal(filterOf(undefined), null);
});

test('an asset outside a space is a page named after it, description only when set', () => {
  const filter = filterOf(choices());
  assert.deepEqual(filter.page, { key: 'asset:1', name: 'Asset 1', description: 'By the sea' });
  assert.equal(filterOf(choices({ assets: [asset(1)] })).page.description, '');
});

test('a whole space is one page named after the space; its assets are not asked', () => {
  const filter = filterOf(spaced('whole_space'), { space: 7 });
  assert.equal(filter.page.name, 'Harbour');
  assert.deepEqual(filter.assets, []);
  assert.deepEqual(calendarQuery(filter), { category: 1, space: 7 });
});

test('a one-asset space asks which one, the first by default', () => {
  const filter = filterOf(spaced('one_asset'), { space: 7 });
  assert.equal(filter.page.name, 'Harbour');
  assert.deepEqual(filter.assets.map((row) => row.id), [2, 3]);
  assert.equal(filter.asset, 2);
  assert.equal(filterOf(spaced('one_asset'), { asset: 3 }).asset, 3);
  assert.deepEqual(calendarQuery(filterOf(spaced('one_asset'), { asset: 3 })), { category: 1, asset: 3 });
});

test('the address picks the page, otherwise the first; unknown ids fall back', () => {
  assert.equal(filterOf(spaced('whole_space')).page.key, 'asset:1');
  assert.equal(filterOf(spaced('whole_space'), { asset: 3 }).page.key, 'space:7');
  assert.equal(filterOf(spaced('whole_space'), { space: 99 }).page.key, 'asset:1');
  assert.equal(filterOf(spaced('one_asset'), { asset: 99 }).page.key, 'asset:1');
});

test('category: the address, else the member\'s last, else the first; shown with more than one', () => {
  assert.equal(filterOf(choices(), { category: 2 }).category, 2);
  assert.equal(filterOf(choices({ category: 2 })).category, 2);
  assert.equal(filterOf(choices({ category: 2 }), { category: 9 }).category, 2);
  assert.equal(filterOf(choices({ category: null })).category, 1);
  assert.equal(filterOf(choices({ categories: [{ id: 1, name: 'Private' }] })).categories.length, 1);
});

test('types: only the chosen category\'s, only when more than one', () => {
  const two = choices({ assets: [asset(1, { types: [type(10), type(11, { categories: [1] })] })] });
  assert.deepEqual(filterOf(two).types.map((row) => row.id), [10, 11]);
  assert.deepEqual(filterOf(two, { category: 2 }).types, []);
  assert.equal(filterOf(two, { type: 11 }).type, 11);
  // A type the category does not offer stays chosen (); the day says so.
  assert.equal(filterOf(two, { type: 11, category: 2 }).type, 11);
  assert.deepEqual(filterOf(two, { type: 11, category: 2 }).types.map((row) => row.id), [10, 11]);
  assert.deepEqual(calendarQuery(filterOf(two, { type: 11, category: 2 })).type, 11);
  const kept = filterOf(two, { type: 11, category: 2 });
  assert.deepEqual(filterOf(two, readAddress(`?${addressOf(kept)}`)), kept);
  // Also when the category offers only one type of its own.
  const lone = choices({ assets: [asset(1, { types: [type(10), type(11, { categories: [1] }), type(12, { categories: [1] })] })] });
  assert.equal(filterOf(lone, { type: 11, category: 2 }).type, 11);
  assert.deepEqual(filterOf(lone, { type: 11, category: 2 }).types.map((row) => row.id), [10, 11]);
  assert.equal(filterOf(two, { type: 99 }).type, null);
  assert.deepEqual(filterOf(choices()).types, []);
});

test('a whole space lists the types of all its assets once', () => {
  const filter = filterOf(spaced('whole_space'), { space: 7 });
  assert.deepEqual(filter.types.map((row) => row.id), [12, 13]);
});

test('how many: shared containers with more than one spot, up to the largest', () => {
  const shared = (max) => container({ consumption_mode: 'shared', max_quantity: max });
  const many = choices({ assets: [asset(1, { types: [type(10, {}, [shared(1), shared(3)]), type(11, {}, [shared(2)])] })] });
  assert.deepEqual(filterOf(many).quantities, [1, 2, 3]);
  assert.equal(filterOf(many, { quantity: 3 }).quantity, 3);
  assert.equal(filterOf(many, { quantity: 4 }).quantity, 1);
  assert.deepEqual(filterOf(many, { type: 11 }).quantities, [1, 2]);
  const exclusive = choices({ assets: [asset(1, { types: [type(10, {}, [container({ max_quantity: 5 })])] })] });
  assert.deepEqual(filterOf(exclusive).quantities, []);
  assert.deepEqual(filterOf(choices({ assets: [asset(1, { types: [type(10, {}, [shared(1)])] })] })).quantities, []);
});

test('a quantity falls back when the category can no longer have it', () => {
  const only = container({ consumption_mode: 'shared', max_quantity: 4, categories: [1] });
  const c = choices({ assets: [asset(1, { types: [type(10, {}, [only])] })] });
  assert.equal(filterOf(c, { quantity: 3 }).quantity, 3);
  assert.equal(filterOf(c, { quantity: 3, category: 2 }).quantity, 1);
});

test('keep us together: the groups of the shared containers\' floor plans, each once', () => {
  const rows = [{ id: 4, number: 1, name: 'Row A', word: 'row', spots: 6 }, { id: 5, number: 2, name: 'Row B', word: 'row', spots: 6 }];
  const hall = (id, groups) => container({ id, consumption_mode: 'shared', max_quantity: 8, groups });
  const c = choices({ assets: [asset(1, { types: [type(10, {}, [hall(1, rows), hall(2, [rows[1]])])] })] });
  assert.deepEqual(filterOf(choices()).groups, []);
  assert.deepEqual(filterOf(c).groups.map((row) => row.name), ['Row A', 'Row B']);
  assert.equal(filterOf(c, { group: 5 }).group, 5);
  assert.equal(filterOf(c, { group: 9 }).group, null);
  // An exclusive container's groups are never asked for.
  const lone = choices({ assets: [asset(1, { types: [type(10, {}, [container({ groups: rows })])] })] });
  assert.deepEqual(filterOf(lone, { group: 4 }).groups, []);
});

test('keep us together: only a group that holds the number asked; the group chosen caps the number', () => {
  const tables = [
    { id: 4, number: 1, name: 'Window', word: 'table', spots: 6 },
    { id: 5, number: 2, name: 'Bar', word: 'table', spots: 4 },
    { id: 6, number: 3, name: 'Empty', word: 'table', spots: 0 },
  ];
  const c = choices({ assets: [asset(1, { types: [type(10, {}, [container({ consumption_mode: 'shared', max_quantity: 8, groups: tables })])] })] });
  assert.deepEqual(filterOf(c).groups.map((row) => row.name), ['Window', 'Bar']);
  assert.deepEqual(filterOf(c, { quantity: 6 }).groups.map((row) => row.name), ['Window']);
  assert.equal(filterOf(c, { quantity: 6, group: 5 }).group, null);
  assert.equal(filterOf(c).quantities.length, 8);
  assert.deepEqual(filterOf(c, { group: 5 }).quantities, [1, 2, 3, 4]);
  assert.deepEqual(filterOf(c, { quantity: 3, group: 5 }), filterOf(c, pickOf(filterOf(c, { quantity: 3, group: 5 }))));
});

test('keep us together: a group name two floor plans share says its asset, or its floor plan on one asset', () => {
  const window = (id, plan) => ({ id, number: 1, name: 'Window', word: 'table', spots: 4, plan });
  const hall = (id, group) => container({ id, consumption_mode: 'shared', max_quantity: 4, groups: [group] });
  const c = choices({
    assets: [asset(1, { space: 7, types: [type(11, {}, [hall(1, window(4, 'Hall'))])] }),
      asset(2, { space: 7, types: [type(12, {}, [hall(2, window(5, 'Hall'))])] })],
    spaces: [{ id: 7, name: 'Harbour', booking_page: 'whole_space' }],
  });
  assert.deepEqual(filterOf(c).groups.map((row) => row.name), ['Window · Asset 1', 'Window · Asset 2']);
  const one = choices({ assets: [asset(1, { types: [type(11, {}, [hall(1, window(4, 'Ground floor')), hall(2, window(5, 'Terrace'))])] })] });
  assert.deepEqual(filterOf(one).groups.map((row) => row.name), ['Window · Ground floor', 'Window · Terrace']);
});

test('a kept type keeps the number and the group: they go by the category\'s own containers', () => {
  const tables = [{ id: 4, number: 1, name: 'Window', word: 'table', spots: 6, plan: 'Hall' }];
  const dinner = type(10, {}, [container({ consumption_mode: 'shared', max_quantity: 6, groups: tables })]);
  const party = type(11, { categories: [1] });
  const c = choices({ assets: [asset(1, { types: [dinner, party] })] });
  const filter = filterOf(c, { category: 2, type: 11, quantity: 4, group: 4 });
  assert.deepEqual([filter.type, filter.quantity, filter.group], [11, 4, 4]);
  assert.deepEqual(filterOf(c, readAddress(`?${addressOf(filter)}`)), filter);
  // Show all (the chip's x) loosens the type only.
  const all = filterOf(c, { ...pickOf(filter), type: null });
  assert.deepEqual([all.type, all.quantity, all.group], [null, 4, 4]);
});

test('a type on another asset of the page stays chosen', () => {
  const c = spaced('one_asset');
  const page = filterOf(c, { asset: 2 });
  assert.ok(page.assets.length > 1);
  assert.equal(filterOf(c, { asset: 2, type: 13 }).type, 13);
});

test('the filter has no length: the calendar is asked without one', () => {
  const flexible = container({ period_type: 'flexible', interval: 30, minimum_length: 60, maximum_length: 120 });
  const filter = filterOf(choices({ assets: [asset(1, { types: [type(10, {}, [flexible])] })] }), { length: 90 });
  assert.equal('length' in filter, false);
  assert.equal('lengths' in filter, false);
  assert.equal('length' in calendarQuery(filter), false);
  assert.equal(addressOf(filter), 'category=1');
});

test('the address: the category when there is a choice, other defaults left out; a round trip', () => {
  const flexible = container({
    period_type: 'flexible', consumption_mode: 'shared', max_quantity: 3, interval: 30, minimum_length: 60, maximum_length: 120,
    groups: [{ id: 4, number: 1, name: 'Row A', word: 'row', spots: 3 }],
  });
  const c = choices({
    assets: [asset(1), asset(2, { types: [type(20), type(21, {}, [flexible])] })],
  });
  assert.equal(addressOf(filterOf(c)), 'category=1');
  assert.equal(addressOf(filterOf({ ...c, categories: [{ id: 1, name: 'Private' }] })), '');
  const filter = filterOf(c, { category: 2, asset: 2, type: 21, quantity: 2, group: 4 });
  const search = addressOf(filter);
  assert.equal(search, 'category=2&type=21&asset=2&quantity=2&group=4');
  assert.deepEqual(filterOf(c, readAddress(`?${search}`)), filter);
  assert.deepEqual(filterOf(c, pickOf(filter)), filter);
});

test('the address of a space page and of a later page in a one-asset space', () => {
  const whole = filterOf(spaced('whole_space'), { space: 7 });
  assert.equal(addressOf(whole), 'category=1&space=7');
  assert.equal(filterOf(spaced('whole_space'), pickOf(whole)).page.key, 'space:7');
  const one = filterOf(spaced('one_asset'), { asset: 2 });
  assert.equal(addressOf(one), 'category=1&asset=2');
  assert.equal(addressOf(filterOf(spaced('one_asset'))), 'category=1');
});

test('readAddress takes numbers only', () => {
  assert.deepEqual(readAddress('?category=2&type=x&asset=-1&group='), {
    category: 2, type: null, asset: null, space: null, quantity: null, group: null,
  });
});

test('the calendar query carries only what is chosen', () => {
  assert.deepEqual(calendarQuery(filterOf(choices())), { category: 1, asset: 1 });
  const two = choices({ assets: [asset(1, { types: [type(10), type(11)] })] });
  assert.deepEqual(calendarQuery(filterOf(two, { category: 2, type: 11 })), { category: 2, asset: 1, type: 11 });
});

test('pickOf gives the resolved choice in the address\'s shape', () => {
  const flexible = container({ period_type: 'flexible', interval: 30, minimum_length: 60, maximum_length: 90 });
  const c = choices({ assets: [asset(1, { types: [type(10, {}, [flexible]), type(11)] })] });
  assert.deepEqual(pickOf(filterOf(c, { category: 9, type: 99 })), {
    category: 1, type: null, asset: 1, space: null, quantity: 1, group: null,
  });
  assert.deepEqual(pickOf(filterOf(spaced('whole_space'), { asset: 3 })), {
    category: 1, type: null, asset: null, space: 7, quantity: 1, group: null,
  });
});

test('an asset outside a one-asset space falls back to the space\'s first', () => {
  assert.equal(filterOf(spaced('one_asset'), { space: 7, asset: 99 }).asset, 2);
  assert.equal(filterOf(spaced('one_asset'), { space: 7, asset: 1 }).asset, 2);
});

test('?asset= on a whole-space page opens the space page', () => {
  const filter = filterOf(spaced('whole_space'), readAddress('?asset=3'));
  assert.equal(filter.page.key, 'space:7');
  assert.deepEqual(calendarQuery(filter), { category: 1, space: 7 });
  assert.equal(addressOf(filter), 'category=1&space=7');
});

test('the address on load: read, resolved, written back', () => {
  const load = (search) => addressOf(filterOf(spaced('one_asset'), readAddress(search)));
  assert.equal(load(''), 'category=1');
  assert.equal(load('?category=2&asset=3'), 'category=2&asset=3');
  assert.equal(load('?category=9&asset=99&type=4&quantity=7&group=1'), 'category=1');
});

test('the spot word: the one the filtered containers that sell spots share, else none', () => {
  const shared = (word, extra = {}) => container({ consumption_mode: 'shared', max_quantity: 4, spot_word: word, ...extra });
  const one = (...containers) => choices({ assets: [asset(1, { types: [type(10, {}, containers)] })] });
  assert.equal(filterOf(one(shared('seat'))).spotWord, 'seat');
  // An exclusive container sells no spots, so its word counts for nothing.
  assert.equal(filterOf(one(shared('bed'), container({ id: 2, number: 2, spot_word: null }))).spotWord, 'bed');
  assert.equal(filterOf(one(shared('seat'), shared('bed', { id: 2, number: 2 }))).spotWord, null);
  assert.equal(filterOf(one(shared(null))).spotWord, null);
  assert.equal(filterOf(one(container())).spotWord, null);
});
