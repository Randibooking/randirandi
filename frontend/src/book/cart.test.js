// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  afterCancel, afterOrder, afterPaymentStart, calendarChoices, cartView, clockWords, groupLines, lineView, manyAssets, onlyLine, outcomeOf, paidWithVipps, paidWords, payWords,
  readOutcome, READ_AGAIN, removeLine, secondsLeft, sumWords, typeOrder,
} from './cart.js';
import { moneyWords } from './add.js';
import { createStore } from './store.js';

const line = (id, type, start, end, total, extra = {}) => ({
  id, type: { id: 1, name: type }, category: { id: 1, name: 'Member' }, start, end, total, state: 'ok', text: '', ...extra,
});
const choices = {
  assets: [
    { types: [{ name: 'Overnight stay' }, { name: 'Hall hire' }] },
    { types: [{ name: 'Hall hire' }, { name: 'Sauna' }] },
  ],
};

test('typeOrder: the choices\' order, each name once', () => {
  assert.deepEqual(typeOrder(choices), ['Overnight stay', 'Hall hire', 'Sauna']);
  assert.deepEqual(typeOrder(null), []);
});

test('groupLines: a group per type in the organization\'s order, lines in date order', () => {
  const lines = [
    line('a', 'Hall hire', '2026-11-05T10:00:00+01:00', '2026-11-05T12:00:00+01:00', 500),
    line('b', 'Overnight stay', '2026-11-03T15:00:00+01:00', '2026-11-04T11:00:00+01:00', 800),
    line('c', 'Overnight stay', '2026-11-02T15:00:00+01:00', '2026-11-03T11:00:00+01:00', 800),
    line('d', 'Hall hire', '2026-11-01T10:00:00+01:00', '2026-11-01T12:00:00+01:00', 0),
    line('e', 'Other', '2026-11-01T10:00:00+01:00', '2026-11-01T12:00:00+01:00', null),
  ];
  const groups = groupLines(lines, typeOrder(choices));
  assert.deepEqual(groups.map((g) => g.type), ['Overnight stay', 'Hall hire', 'Other']);
  assert.deepEqual(groups[0].lines.map((l) => l.id), ['c', 'b']);
  assert.deepEqual(groups[1].lines.map((l) => l.id), ['d', 'a']);
  assert.deepEqual(groups.map((g) => [g.count, g.sum]), [[2, 1600], [2, 500], [1, 0]]);
  assert.deepEqual(groups.map((g) => g.night), [true, false, false]);
});

test('groupLines: an order read names the type as text', () => {
  const groups = groupLines([{ type: 'Sauna', start: '2026-11-01T10:00:00+01:00', end: '2026-11-01T11:00:00+01:00', total: 0 }], ['Sauna']);
  assert.equal(groups[0].type, 'Sauna');
});

test('groupLines: types outside the order go last by name', () => {
  const l = (type) => line(type, type, '2026-11-01T10:00:00+01:00', '2026-11-01T11:00:00+01:00', 1);
  assert.deepEqual(groupLines([l('Z'), l('A'), l('K')], ['K']).map((g) => g.type), ['K', 'A', 'Z']);
});

test('lineView: date, time, category, price; a mark only in the cart', () => {
  const l = line('a', 'Overnight stay', '2026-10-20T15:00:00+02:00', '2026-10-21T11:00:00+02:00', 80000,
    { state: 'taken', text: 'taken' });
  const view = lineView(l, 'NOK', true);
  assert.equal(view.date, 'Tuesday 20 October');
  assert.equal(view.time, '15:00–11:00 next day');
  assert.equal(view.as, 'Member');
  assert.equal(view.marked, 'taken');
  assert.match(view.price, /800/);
  assert.equal(lineView(l, 'NOK', false).marked, null);
  assert.equal(lineView({ ...l, total: 0 }, 'NOK').price, 'No charge');
});

test('cartView: the held order, the lines and which are marked', () => {
  const cart = {
    total: 10, ready: false, order: { id: 'o', state: 'held' },
    bookings: [line('a', 'X', '2026-11-01T10:00:00+01:00', '2026-11-01T11:00:00+01:00', 10, { state: 'taken', text: 'taken' }),
      line('b', 'X', '2026-11-02T10:00:00+01:00', '2026-11-02T11:00:00+01:00', 10)],
  };
  const view = cartView(cart);
  assert.equal(view.held.id, 'o');
  assert.deepEqual(view.marked.map((l) => l.id), ['a']);
  assert.equal(cartView({ ...cart, order: { id: 'o', state: 'confirmed' } }).held, null);
  assert.equal(cartView(null).lines.length, 0);
  assert.equal(cartView(null).shown.length, 0);
  // Not held: the cart's rows show.
  assert.deepEqual(cartView({ ...cart, order: null }).shown.map((l) => l.id), ['a', 'b']);
});

test('cartView: a held order shows its own bookings with their prices', () => {
  // While its order holds them, the cart's rows are not priced (total null).
  const rows = [line('a', 'Hall', '2026-10-21T10:00:00+02:00', '2026-10-21T11:00:00+02:00', null, { state: 'held' })];
  const booking = {
    container: { id: 1, number: 1 }, spots: [], spot_word: null, asset: 'Hall', type: 'Hall', category: 'Private',
    places: 1, start: '2026-10-21T10:00:00+02:00', end: '2026-10-21T11:00:00+02:00', total: 25000, state: 'held',
  };
  const view = cartView({ total: 25000, order: { id: 'o', state: 'held', total: 25000, bookings: [booking] }, bookings: rows });
  const [group] = groupLines(view.shown);
  assert.equal(group.sum, 25000);
  assert.equal(sumWords(group.sum, 'NOK'), moneyWords(25000, 'NOK'));
  assert.equal(lineView(group.lines[0], 'NOK').price, moneyWords(25000, 'NOK'));
});

test('payWords, secondsLeft, clockWords', () => {
  assert.equal(payWords(0, 'NOK'), 'Confirm booking');
  assert.match(payWords(25000, 'NOK'), /^Go to payment · .*250/);
  const now = Date.parse('2026-10-20T12:00:00+02:00');
  assert.equal(secondsLeft('2026-10-20T12:15:00+02:00', now), 900);
  assert.equal(secondsLeft('2026-10-20T11:00:00+02:00', now), 0);
  assert.equal(secondsLeft(null, now), null);
  assert.equal(clockWords(905), '15:05');
  assert.equal(clockWords(0), '0:00');
});

test('afterOrder: free goes to the outcome, paid to Vipps', () => {
  assert.deepEqual(afterOrder({ ok: true, data: { order: 'o', confirmed: true } }), { kind: 'done', order: 'o' });
  assert.deepEqual(afterOrder({ ok: true, data: { order: 'o', redirect: 'https://vipps.test/x' } }),
    { kind: 'vipps', address: 'https://vipps.test/x', order: 'o' });
  assert.deepEqual(afterOrder({ ok: true, data: { order: 'o', redirect: '/' } }), { kind: 'done', order: 'o' });
});

test('afterOrder: payment unavailable keeps the order and offers again; a refusal carries the server\'s text', () => {
  const unavailable = { kind: 'refused', status: 409, code: 'payment-unavailable', text: null, body: { order: 'o' } };
  assert.deepEqual(afterOrder(unavailable), { kind: 'retry' });
  assert.deepEqual(afterOrder({ ...unavailable, status: 503 }), { kind: 'retry' });
  const changed = { kind: 'refused', status: 409, code: 'price-changed', text: 'the price has changed', body: {} };
  assert.deepEqual(afterOrder(changed), { kind: 'refused', text: 'the price has changed', code: 'price-changed' });
  assert.equal(afterOrder({ kind: 'refused', status: 409, code: 'x', text: null, body: {} }).text, 'Could not book');
  assert.equal(afterOrder({ kind: 'offline' }).kind, 'offline');
  assert.equal(afterOrder({ kind: 'signed-out' }).kind, 'signed-out');
});

test('removeLine: success clears the kept calendars, a refusal does not', async () => {
  const store = createStore();
  store.put('k', 1);
  const done = await removeLine({ csrf: 't', id: 'a', store, remove: async () => ({ ok: true, data: { count: 0 } }) });
  assert.deepEqual(done, { kind: 'removed', cart: { count: 0 } });
  assert.equal(store.size, 0);
  store.put('k', 1);
  const refused = await removeLine({ csrf: 't', id: 'a', store,
    remove: async () => ({ kind: 'refused', status: 409, text: 'a booking is waiting for payment' }) });
  assert.deepEqual(refused, { kind: 'refused', text: 'a booking is waiting for payment' });
  assert.equal(store.size, 1);
});

test('outcomeOf: confirmed, waiting (read again a few times), not paid', () => {
  assert.deepEqual(outcomeOf({ state: 'confirmed' }), { kind: 'confirmed', again: false });
  assert.deepEqual(outcomeOf({ state: 'settling' }, 0), { kind: 'waiting', again: true });
  assert.deepEqual(outcomeOf({ state: 'settling' }, READ_AGAIN), { kind: 'waiting', again: false });
  assert.deepEqual(outcomeOf({ state: 'held' }, 0), { kind: 'waiting', again: false });
  assert.deepEqual(outcomeOf({ state: 'cancelled' }), { kind: 'cancelled', again: false });
  assert.deepEqual(outcomeOf({ state: 'not-completed' }), { kind: 'not-paid', again: false });
});

test('afterPaymentStart: Vipps; try again on 409, 503 or no answer; another refusal reads the cart', () => {
  const refused = (status) => ({ kind: 'refused', status, code: null, text: null, body: {} });
  assert.deepEqual(afterPaymentStart({ ok: true, data: { redirect: 'https://vipps.test/x' } }, 'o'),
    { kind: 'vipps', address: 'https://vipps.test/x', order: 'o' });
  assert.deepEqual(afterPaymentStart({ ok: true, data: { redirect: '/' } }, 'o'), { kind: 'done', order: 'o' });
  for (const status of [409, 503]) assert.deepEqual(afterPaymentStart(refused(status), 'o'), { kind: 'retry' });
  assert.deepEqual(afterPaymentStart({ kind: 'offline' }, 'o'), { kind: 'retry' });
  for (const status of [400, 404]) assert.deepEqual(afterPaymentStart(refused(status), 'o'), { kind: 'gone' });
  assert.deepEqual(afterPaymentStart({ kind: 'signed-out' }, 'o'), { kind: 'signed-out' });
  assert.deepEqual(afterPaymentStart({ kind: 'forbidden', status: 403 }, 'o'), { kind: 'forbidden' });
});

test('afterCancel: the cart back; paid first goes to the outcome; gone reads again; a refusal its text', () => {
  const cart = { count: 1, locked: false };
  assert.deepEqual(afterCancel({ ok: true, data: cart }), { kind: 'cart', cart });
  assert.deepEqual(afterCancel({ kind: 'refused', status: 409, code: 'confirmed', text: 'confirmed' }), { kind: 'paid' });
  assert.deepEqual(afterCancel({ kind: 'refused', status: 404, code: 'not-found', text: null }), { kind: 'gone' });
  assert.deepEqual(afterCancel({ kind: 'refused', status: 409, code: 'settling', text: 'the payment is being checked' }),
    { kind: 'refused', text: 'the payment is being checked' });
  assert.deepEqual(afterCancel({ kind: 'refused', status: 500, code: null, text: null }),
    { kind: 'refused', text: 'Could not cancel the payment' });
  assert.deepEqual(afterCancel({ kind: 'offline' }), { kind: 'offline' });
  assert.deepEqual(afterCancel({ kind: 'signed-out' }), { kind: 'signed-out' });
});

test('lineView: places when more than one, from the cart or the order read; the asset only when asked', () => {
  const line = { id: 'r', start: '2026-10-23T19:00:00+02:00', end: '2026-10-23T21:00:00+02:00',
    category: { id: 1, name: 'Private' }, total: 20000, asset: { id: 1, name: 'Riverside Hall' } };
  assert.equal(lineView({ ...line, quantity: 2, spot_word: 'seat' }, 'NOK', true).places, '2 × seat');
  assert.equal(lineView({ ...line, quantity: 1, spot_word: 'seat' }, 'NOK', true).places, null);
  assert.equal(lineView({ ...line, asset: 'Riverside Hall', places: 3, spot_word: 'bed' }, 'NOK').places, '3 × bed');
  // Without a floor plan's word, the label's.
  assert.equal(lineView({ ...line, quantity: 2, spot_word: null }, 'NOK', true).places, '2 × spot');
  assert.equal(lineView(line, 'NOK', true).asset, null);
  assert.equal(lineView(line, 'NOK', true, true).asset, 'Riverside Hall');
  assert.equal(lineView({ ...line, asset: 'Garden Shed' }, 'NOK', false, true).asset, 'Garden Shed');
});

test('manyAssets: true only when the lines are on more than one asset', () => {
  const on = (name) => ({ asset: { name } });
  assert.equal(manyAssets([{ lines: [on('Riverside Hall'), on('Riverside Hall')] }]), false);
  assert.equal(manyAssets([{ lines: [on('Riverside Hall')] }, { lines: [{ asset: 'Garden Shed' }] }]), true);
});

test('readOutcome: the order, not found, or the other kinds as they are', () => {
  assert.deepEqual(readOutcome({ ok: true, data: { id: 'o' } }), { kind: 'order', order: { id: 'o' } });
  assert.deepEqual(readOutcome({ kind: 'refused', status: 404, body: {} }), { kind: 'not-found' });
  assert.deepEqual(readOutcome({ kind: 'offline' }), { kind: 'offline' });
  assert.deepEqual(readOutcome({ kind: 'refused', status: 500, body: {} }), { kind: 'refused' });
});

test('paidWords', () => {
  assert.equal(paidWords({ total: 0, currency: 'NOK' }), 'free of charge');
  assert.match(paidWords({ total: 25000, currency: 'NOK' }), /^paid .*250/);
});

test('calendarChoices: the file, then each confirmed booking in Google Calendar', () => {
  const google = (n) => `https://calendar.google.com/calendar/render?action=TEMPLATE&n=${n}`;
  const one = { id: 'a b', bookings: [{ start: '2027-03-02T10:00:00+01:00', end: '2027-03-02T12:00:00+01:00', google: google(1) }] };
  assert.deepEqual(calendarChoices(one), [
    { key: 'file', href: '/booking/order/a%20b/calendar/', text: 'Calendar file', file: true },
    { key: 'google-0', href: google(1), text: 'Google Calendar', file: false },
  ]);
  const two = { id: 'x', bookings: [
    { start: '2027-03-02T10:00:00+01:00', end: '2027-03-02T12:00:00+01:00', google: google(1) },
    { start: '2027-03-03T10:00:00+01:00', end: '2027-03-03T12:00:00+01:00', google: null },
    { start: '2027-03-06T22:00:00+01:00', end: '2027-03-07T08:00:00+01:00', google: google(3) },
  ] };
  assert.deepEqual(calendarChoices(two).map((choice) => choice.text), [
    'Calendar file', 'Google Calendar · Tuesday 2 March 10:00–12:00', 'Google Calendar · Saturday 6 March 22:00–08:00 next day',
  ]);
  // On several assets, each names its asset.
  const twoAssets = { id: 'x', bookings: [
    { asset: 'Hall', start: '2027-03-02T10:00:00+01:00', end: '2027-03-02T12:00:00+01:00', google: google(1) },
    { asset: 'Sauna', start: '2027-03-02T10:00:00+01:00', end: '2027-03-02T12:00:00+01:00', google: google(2) },
  ] };
  assert.deepEqual(calendarChoices(twoAssets).slice(1).map((choice) => choice.text), [
    'Google Calendar · Hall · Tuesday 2 March 10:00–12:00', 'Google Calendar · Sauna · Tuesday 2 March 10:00–12:00',
  ]);
  // The same asset and times twice: the type tells them apart.
  const sameTimes = { id: 'x', bookings: [
    { asset: 'Hall', type: 'Meeting', start: '2027-03-02T10:00:00+01:00', end: '2027-03-02T12:00:00+01:00', google: google(1) },
    { asset: 'Hall', type: 'Party', start: '2027-03-02T10:00:00+01:00', end: '2027-03-02T12:00:00+01:00', google: google(2) },
  ] };
  assert.deepEqual(calendarChoices(sameTimes).slice(1).map((choice) => choice.text), [
    'Google Calendar · Meeting · Hall · Tuesday 2 March 10:00–12:00', 'Google Calendar · Party · Hall · Tuesday 2 March 10:00–12:00',
  ]);
  // Nothing without a confirmed booking, and never an address elsewhere.
  assert.deepEqual(calendarChoices({ id: 'x', bookings: [{ start: '', end: '' }] }), []);
  assert.deepEqual(calendarChoices({ id: 'x', bookings: [{ start: '', end: '', google: 'https://example.com/?x' }] }), []);
  assert.deepEqual(calendarChoices({ id: 'x' }), []);
});

test('onlyLine: one line in all hides sums and Total; two or more keep them', () => {
  const a = line('a', 'X', '2026-11-01T10:00:00+01:00', '2026-11-01T11:00:00+01:00', 150000);
  const b = line('b', 'X', '2026-11-02T10:00:00+01:00', '2026-11-02T11:00:00+01:00', 150000);
  const c = line('c', 'Y', '2026-11-02T10:00:00+01:00', '2026-11-02T11:00:00+01:00', 150000);
  assert.equal(onlyLine(groupLines([a])), true);
  assert.equal(onlyLine(groupLines([a, b])), false);
  assert.equal(onlyLine(groupLines([a, c])), false);
  assert.equal(onlyLine(groupLines([])), false);
});

test('paidWithVipps: only when there is something to pay', () => {
  assert.equal(paidWithVipps(150000), true);
  assert.equal(paidWithVipps(0), false);
  assert.equal(paidWithVipps(null), false);
});

test('cartView carries the identifier of the booking conditions; Pay needs the box and a ready cart', async () => {
  const { canPay } = await import('./cart.js');
  const cart = { ready: true, total: 400, bookings: [], conditions: { version: 'v1' } };
  const view = cartView(cart);
  assert.equal(view.version, 'v1');
  assert.equal(canPay(view, false), false);
  assert.equal(canPay(view, true), true);
  assert.equal(canPay(cartView({ ...cart, ready: false }), true), false);
  assert.equal(cartView({ ready: true, bookings: [] }).version, null);
  assert.equal(canPay(cartView({ ready: true, bookings: [] }), true), false);
});

test('afterOrder: the conditions refusals carry the server\'s words, so the cart is read again', () => {
  const none = { kind: 'refused', status: 409, code: 'conditions-not-accepted', text: 'accept the booking conditions and the prices to book', body: {} };
  assert.deepEqual(afterOrder(none), { kind: 'refused', text: 'accept the booking conditions and the prices to book', code: 'conditions-not-accepted' });
  const changed = { kind: 'refused', status: 409, code: 'conditions-changed', text: 'the booking conditions have changed: read them and accept again', body: {} };
  assert.equal(afterOrder(changed).code, 'conditions-changed');
});
