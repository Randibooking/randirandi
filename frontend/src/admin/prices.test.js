// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  amountOf, amountText, bookBody, bookNameTaken, daysText, draftsOf, exampleText, figure, freshDraft, goingBy, length, missing, money,
  priceFailure, pricedNames, refusals, saveBody, sheetRefusals, steps, timesText, withTier,
} from './prices.js';

// A flexible container 10:00-22:00, 1 h to 12 h, Monday to Friday.
const container = {
  id: 7, number: 2, book: 1, type: 'Room hire', asset: 'Hall', weekdays: [0, 1, 2, 3, 4], start: 600, end: 1320, period: 'flexible',
  interval: 60, minimum: 60, maximum: 720, mode: 'exclusive', unit: '', groups: [], categories: [10, 11], entries: [],
};
const entry = (category, tiers, more = {}) => ({ category, group: null, tiers_go_by: 'length', billing_basis: 'per_booking', tiers, ...more });
const private_ = entry(10, [{ up_to: 240, paid_per: 240, amount: 25000 }, { up_to: 720, paid_per: 240, amount: 10000 }]);
const shared = {
  ...container, mode: 'shared', unit: 'machine',
  groups: [{ id: 5, name: 'Krebs', word: 'laundry' }], entries: [],
};

test('lengths, times and days are worded as the drawing words them', () => {
  assert.equal(length(240), '4 h');
  assert.equal(length(90), '1 h 30 min');
  assert.equal(length(0), '0 min');
  assert.equal(timesText(container), '10:00–22:00');
  assert.equal(timesText({ start: 1320, end: 1440 + 600 }), '22:00–10:00 next day');
  assert.equal(daysText([0, 1, 2, 3, 4]), 'Mon–Fri');
  assert.equal(daysText([6, 0, 1, 2, 3]), 'Mon–Thu, Sun');
  assert.equal(daysText([0, 1, 2, 3, 4, 5, 6]), 'Every day');
});

test('an amount is shown in the main unit and typed with up to two decimals', () => {
  assert.equal(figure(25000), '250');
  assert.equal(figure(24950), '249,50');
  assert.equal(figure(0), '0');
  assert.equal(figure(1234500), '12 345');
  assert.equal(money(25000, 'NOK'), '250 NOK');
  assert.equal(amountText(24950), '249.50');
  assert.equal(amountText(25000), '250');
  assert.equal(amountText(NaN), '');
  assert.equal(amountOf('250'), 25000);
  assert.equal(amountOf('249.5'), 24950);
  assert.equal(amountOf('249,50'), 24950);
  assert.equal(amountOf(' 0 '), 0);
  for (const bad of ['', 'x', '-1', '1.234', '1e3', '2147483648', '1.', '.5']) assert.ok(Number.isNaN(amountOf(bad)), bad);
  assert.equal(amountOf('21474836.47'), 2147483647);
});

test('an example is worded by its length or by the times it runs between', () => {
  const at = (start, len) => ({ start, length: len, total: 1 });
  assert.equal(exampleText(private_, container, at(600, 300)), '5 h');
  const time = entry(11, [{ up_to: 1320, paid_per: 240, amount: 50000 }], { tiers_go_by: 'time of day' });
  assert.equal(exampleText(time, container, at(600, 420)), '10:00–17:00');
  assert.equal(exampleText(time, container, at(1200, 240)), '20:00–24:00');
  const fixed = { ...container, period: 'fixed', start: 1320, end: 1440 + 600 };
  assert.equal(exampleText(private_, fixed, at(1320, 720)), '22:00–10:00 next day');
});

test('a draft is the entry as a sheet edits it, and goes back as the server takes it', () => {
  const c = { ...container, entries: [private_] };
  const drafts = draftsOf(c, 10);
  assert.equal(drafts.existed, true);
  assert.deepEqual(drafts.normal.tiers.map((t) => t.text), ['250', '100']);
  assert.deepEqual(saveBody(drafts, c.groups, 'abc'), { entries: [{
    group: null, tiers_go_by: 'length', billing_basis: 'per_booking',
    tiers: [{ up_to: 240, paid_per: 240, amount: 25000 }, { up_to: 720, paid_per: 240, amount: 10000 }],
  }], stamp: 'abc' });
  const none = draftsOf(c, 11);
  assert.equal(none.existed, false);
  assert.deepEqual(none.normal.tiers, [{ up_to: 720, paid_per: 60, text: '0' }]);
  assert.deepEqual(freshDraft({ ...container, period: 'fixed', maximum: 600 }, null, 'per_booking').tiers[0].paid_per, 600);
});

test('the group prices follow the normal price in the body, only those that are on', () => {
  const c = { ...shared, entries: [entry(10, [{ up_to: 720, paid_per: 60, amount: 500 }], { billing_basis: 'per_capacity_unit' }),
    entry(10, [{ up_to: 720, paid_per: 60, amount: 800 }], { billing_basis: 'per_capacity_unit', group: 5 })] };
  const drafts = draftsOf(c, 10);
  const body = saveBody(drafts, c.groups);
  assert.deepEqual(body.entries.map((e) => e.group), [null, 5]);
  assert.deepEqual(saveBody({ ...drafts, 5: null }, c.groups).entries.map((e) => e.group), [null]);
});

test('the refusals before a Save are the server\'s, in the drawing\'s words', () => {
  const draft = (tiers, more = {}) => ({ group: null, tiers_go_by: 'length', billing_basis: 'per_booking', tiers, ...more });
  const t = (up_to, text = '1') => ({ up_to, paid_per: 60, text });
  assert.deepEqual(refusals(draft([t(720)]), container, ''), []);
  assert.deepEqual(refusals(draft([t(240)]), container, ''), ['The tiers stop at 4 h; the longest booking is 12 h.']);
  assert.deepEqual(refusals(draft([t(480), t(480), t(720)]), container, ''), ['Each tier reaches past the one before.']);
  assert.deepEqual(refusals(draft([t(720, '')]), container, ''), ['Each tier has an amount of 0 or more, with up to two decimals.']);
  assert.deepEqual(refusals(draft([t(720, '-1')]), container, 'Krebs'),
    ['Krebs: Each tier has an amount of 0 or more, with up to two decimals.']);
  const time = (tiers) => draft(tiers, { tiers_go_by: 'time of day' });
  assert.deepEqual(refusals(time([t(1020)]), container, ''), ['The tiers stop at 17:00; the period ends at 22:00.']);
  assert.deepEqual(refusals(time([t(1320)]), container, ''), []);
  assert.deepEqual(refusals(time([t(1320)]), { ...container, end: 1440 + 600 }, ''), ['Time of day does not price a period over midnight.']);
  assert.deepEqual(refusals(time([t(1440)]), { ...container, end: 1440 }, ''), []);
  assert.deepEqual(refusals(time([t(1320), t(1440)]), container, ''), []);
  assert.ok(refusals(time([t(1500)]), { ...container, end: 1440 }, '').includes('A tier ends by 24:00.'));
});

test('a price for a group makes the normal price per spot too', () => {
  const normal = freshDraft(shared, null, 'per_booking');
  const own = { ...freshDraft(shared, 5, 'per_capacity_unit') };
  const found = sheetRefusals({ normal, 5: own, existed: false }, shared, 'machine');
  assert.ok(found.includes('With a price for a group, the normal price is per machine too.'));
  assert.deepEqual(sheetRefusals({ normal, 5: null, existed: false }, shared, 'machine').filter((x) => x.includes('group')), []);
});

test('changing what the tiers go by starts them again; a tier is added after the last', () => {
  const d = { group: null, tiers_go_by: 'length', billing_basis: 'per_booking', tiers: [{ up_to: 720, paid_per: 60, text: '3' }] };
  const time = goingBy(d, 'time of day', container);
  assert.deepEqual(time.tiers, [{ up_to: 1320, paid_per: 60, text: '3' }]);
  assert.equal(goingBy(time, 'length', container).tiers[0].up_to, 720);
  assert.equal(goingBy(d, 'time of day', { ...container, end: 1440 }).tiers[0].up_to, 1440);
  const more = withTier({ ...d, tiers: [{ up_to: 240, paid_per: 120, text: '1' }] }, container);
  assert.deepEqual(more.tiers.map((x) => x.up_to), [240, 360]);
  assert.equal(withTier({ ...d, tiers: [{ up_to: 700, paid_per: 120, text: '1' }] }, container).tiers[1].up_to, 720);
  assert.deepEqual(steps(15, 60), [15, 30, 45, 60]);
});

test('the list says what is priced and what is not', () => {
  const categories = [{ id: 10, name: 'Private' }, { id: 11, name: 'Work' }];
  const c = { ...container, entries: [private_] };
  assert.equal(pricedNames(c, categories), 'Private');
  assert.deepEqual(missing(c, categories), ['Work']);
  assert.equal(pricedNames(container, categories), '–');
  // A price for a group alone does not price the category.
  const groupOnly = { ...shared, entries: [entry(10, [{ up_to: 1, paid_per: 1, amount: 1 }], { group: 5 })] };
  assert.deepEqual(missing(groupOnly, categories), ['Private', 'Work']);
});

test('a book name is taken whatever its case, and a change sends what differs', () => {
  const books = [{ id: 1, name: 'Prices 2027', currency: 'NOK', entries: 3 }];
  assert.equal(bookNameTaken(books, ' prices 2027 '), true);
  assert.equal(bookNameTaken(books, 'prices 2027', 1), false);
  assert.equal(bookNameTaken(books, ''), false);
  assert.deepEqual(bookBody(books[0], 'Prices 2027 ', 'NOK'), {});
  assert.deepEqual(bookBody(books[0], 'Spring', 'NOK'), { name: 'Spring' });
  assert.deepEqual(bookBody(books[0], 'Prices 2027', 'EUR'), { currency: 'EUR' });
  assert.deepEqual(bookBody(null, ' Spring ', 'DKK'), { name: 'Spring', currency: 'DKK' });
});

test('a refusal says the model\'s reason when the server gave one', () => {
  const refused = (code, body) => ({ kind: 'refused', status: 409, code, body });
  assert.equal(priceFailure(refused('tiers-incomplete', { reason: 'The price tiers reach 4 h.' })), 'The price tiers reach 4 h.');
  assert.match(priceFailure(refused('currency-fixed', {})), /currency stays as it is/);
  assert.match(priceFailure(refused('groups-per-spot', {})), /per spot/);
  assert.equal(priceFailure(refused('in-use', { reason: 'x', used: { containers: 2, bookings: 1 } })), 'In use: 2 containers, 1 booking.');
  assert.equal(priceFailure(refused('name-taken', {})), 'Taken');
  assert.equal(priceFailure({ kind: 'offline' }), 'The server could not be reached. Nothing was changed.');
  assert.equal(priceFailure(refused('unheard-of', {})), 'The change could not be made.');
});
