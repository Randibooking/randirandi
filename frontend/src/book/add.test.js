// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildCalendar } from './model.js';
import { createStore, keyOf } from './store.js';
import { cartBlocks, dayList } from './words.js';
import {
  afterFailure, canBook, bookingBody, buttonWords, endStop, endStopWords, endsFor, firstChoice, isNight, moneyWords, moveStart, picksOk, priceQuery,
  priceView, quantityOf, quoteOf, refusedWords, spotsFor, startsOf, submitAdd, togglePick,
} from './add.js';

const DAY = '2026-10-20';
const at = (day, hour) => `${day}T${String(hour).padStart(2, '0')}:00:00+02:00`;
const hourly = (day, from, to) => Array.from({ length: to - from }, (_, i) => at(day, from + i));

function period(day, rows, end, extra = {}) {
  return {
    day, rows, end, row_ends: [...rows.slice(1), end], closed_rows: [], taken: [], category_hours: [],
    starts: [], ...extra,
  };
}

// Starts of a 2 h minimum: every row that has two rows after it free.
const starts = (rows, end, last = rows.length - 2) =>
  rows.slice(0, last + 1).map((_, row) => ({ row, end: rows[row + 2] ?? end, spots: [], free: [] }));

function container(periods, extra = {}) {
  return {
    id: 7, number: 1, asset: { id: 1, name: 'Hall' }, type: { id: 1, name: 'Rent' },
    categories: [1], period_type: 'flexible', consumption_mode: 'exclusive', with_payment: true,
    interval: 60, minimum_length: 120, maximum_length: 240, floor_plan: null, periods, ...extra,
  };
}

const build = (containers) =>
  buildCalendar({ worked_out_at: '2026-10-19T08:00:00+02:00', containers }, { start: DAY, days: 1 }).dates[0];
const flexible = (extra = {}, periodExtra = {}) => {
  const rows = hourly(DAY, 8, 16);
  return build([container([period(DAY, rows, at(DAY, 16), { starts: starts(rows, at(DAY, 16)), ...periodExtra })], extra)]).containers[0];
};

test('flexible: starts are the server\'s; lengths run minimum to maximum by interval', () => {
  const one = flexible();
  assert.deepEqual(startsOf(one).map((s) => s.at), hourly(DAY, 8, 15));
  const [first] = startsOf(one);
  assert.deepEqual(endsFor(one, first).map((e) => e.minutes), [120, 180, 240]);
  assert.deepEqual(endsFor(one, startsOf(one)[6]).map((e) => e.minutes), [120]);
});

test('flexible: the free run caps the length, a booking in it ends the run', () => {
  const rows = hourly(DAY, 8, 16);
  const taken = [{ spot: null, first: 4, last: 7, state: 'confirmed', mine: false }];
  const one = build([container([period(DAY, rows, at(DAY, 16), { taken, starts: starts(rows, at(DAY, 16), 2) })])]).containers[0];
  assert.deepEqual(endsFor(one, startsOf(one)[0]).map((e) => e.minutes), [120, 180, 240]);
  assert.deepEqual(endsFor(one, startsOf(one)[2]).map((e) => e.minutes), [120]);
});

test('flexible: a start with a shorter interval and a bigger minimum', () => {
  const one = flexible({ interval: 30, minimum_length: 60, maximum_length: 120 });
  // Rows are hourly here, so only whole-row ends exist: 60 and 120 minutes.
  assert.deepEqual(endsFor(one, startsOf(one)[0]).map((e) => e.minutes), [60, 120]);
});

test('firstChoice opens on the tapped start, else the first, three intervals long when they fit', () => {
  const one = flexible();
  const tapped = firstChoice(one, at(DAY, 10));
  assert.equal(tapped.start.at, at(DAY, 10));
  assert.equal(tapped.end, at(DAY, 13));
  // Two hours left before the free time ends: the shortest.
  assert.equal(firstChoice(one, at(DAY, 14)).end, at(DAY, 16));
  // A minimum longer than three intervals is the proposal.
  const long = flexible({ minimum_length: 240, maximum_length: 360 });
  assert.equal(firstChoice(long, at(DAY, 8)).end, at(DAY, 12));
  // A shared container: the server's own start, the shortest; the same spots
  // may not stay free longer.
  const shared = flexible({ consumption_mode: 'shared' });
  assert.equal(firstChoice(shared, at(DAY, 10)).end, at(DAY, 12));
  // A new start where the kept length does not fit takes the proposed length.
  assert.equal(moveStart(one, startsOf(one).find((row) => row.at === at(DAY, 13)), 240).end, at(DAY, 16));
  assert.equal(moveStart(one, startsOf(one).find((row) => row.at === at(DAY, 9)), 240).end, at(DAY, 13));
  assert.equal(firstChoice(one).start.at, at(DAY, 8));
  assert.equal(firstChoice(one, at(DAY, 23)).start.at, at(DAY, 8));
});

test('a start that is not offered is no choice; no starts is no choice at all', () => {
  const rows = hourly(DAY, 8, 12);
  const none = build([container([period(DAY, rows, at(DAY, 12), { starts: [] })])]).containers[0];
  assert.equal(firstChoice(none), null);
});

test('moving the start keeps the length when the new start allows it, else the shortest', () => {
  const one = flexible();
  assert.equal(moveStart(one, startsOf(one)[1], 240).end, at(DAY, 13));
  // The last start only has 2 h.
  assert.equal(moveStart(one, startsOf(one)[6], 240).end, at(DAY, 16));
});

test('a night is one fixed period over midnight; its end is its own', () => {
  const rows = [at(DAY, 17)];
  const night = build([container([period(DAY, rows, '2026-10-21T10:00:00+02:00', {
    starts: [{ row: 0, end: '2026-10-21T10:00:00+02:00', spots: [], free: [] }] })], { period_type: 'fixed' })]).containers[0];
  assert.equal(isNight(night), true);
  assert.equal(buttonWords(night), 'Book this night');
  const choice = firstChoice(night);
  assert.equal(choice.end, '2026-10-21T10:00:00+02:00');
  assert.deepEqual(endsFor(night, choice.start).map((e) => e.end), ['2026-10-21T10:00:00+02:00']);
});

test('button words: flexible, night, other fixed', () => {
  assert.equal(buttonWords(flexible()), 'Choose a time');
  const fixed = build([container([period(DAY, [at(DAY, 17)], at(DAY, 19),
    { starts: [{ row: 0, end: at(DAY, 19), spots: [], free: [] }] })], { period_type: 'fixed' })]).containers[0];
  assert.equal(isNight(fixed), false);
  assert.equal(buttonWords(fixed), 'Book');
  assert.equal(dayList(build([container([period(DAY, [at(DAY, 17)], at(DAY, 19),
    { starts: [{ row: 0, end: at(DAY, 19), spots: [], free: [] }] })], { period_type: 'fixed' })]))[0].free, true);
});

const TABLES = [{ id: 3, number: 1, name: 'Window', word: 'window' }, { id: 4, number: 2, name: 'Long', word: 'table' }];
const plan = (names, groups = TABLES) => ({
  name: 'Hall', spot_word: 'table', groups,
  spots: [1, 2, 3].map((n) => ({
    id: n, number: n, name: names ? `T${n}` : '', group: { 2: 4, 3: 3 }[n] ?? null,
  })).filter((spot) => spot.group == null || groups.some((group) => group.id === spot.group)),
});

test('spots: free ones from start to end; a taken one drops out', () => {
  const rows = hourly(DAY, 8, 12);
  const taken = [{ spot: 2, first: 1, last: 1, state: 'confirmed', mine: false }];
  const one = build([container([period(DAY, rows, at(DAY, 12), { taken, starts: starts(rows, at(DAY, 12), 2) })],
    { consumption_mode: 'shared', floor_plan: plan(true) })]).containers[0];
  const start = startsOf(one)[0];
  assert.deepEqual(spotsFor(one, start, at(DAY, 10)).free.map((s) => s.number), [1, 3]);
  assert.deepEqual(spotsFor(one, start, at(DAY, 9)).free.map((s) => s.number), [1, 2, 3]);
});

test('spots without names are a count; named ones are listed', () => {
  const rows = hourly(DAY, 8, 12);
  const taken = [{ spot: 2, first: 0, last: 3, state: 'confirmed', mine: false }];
  const mk = (names, groups) => build([container([period(DAY, rows, at(DAY, 12), { taken, starts: starts(rows, at(DAY, 12), 2) })],
    { consumption_mode: 'shared', floor_plan: plan(names, groups) })]).containers[0];
  const bare = mk(false);
  const info = spotsFor(bare, startsOf(bare)[0], at(DAY, 10));
  assert.equal(info.named, false);
  assert.equal(info.count, '2 of 3 free');
  assert.equal(info.word, 'table');
  // Groups always show: each with its own count; spots in no group only in the count.
  assert.deepEqual(info.groupCounts, [{ id: 3, words: 'Window: 1 of 1 free' }, { id: 4, words: 'Long: 0 of 1 free' }]);
  // One group beside spots in no group still shows; one that holds every spot
  // (the one asked to keep together) is the count itself.
  const one = mk(false, TABLES.slice(0, 1));
  assert.deepEqual(spotsFor(one, startsOf(one)[0], at(DAY, 10)).groupCounts, [{ id: 3, words: 'Window: 1 of 1 free' }]);
  const asked = build([container([period(DAY, rows, at(DAY, 12), { taken: [], starts: starts(rows, at(DAY, 12), 2) })],
    { consumption_mode: 'shared', floor_plan: { ...plan(false, TABLES.slice(0, 1)), spots: [{ id: 3, number: 3, name: '', group: 3 }] } })]).containers[0];
  assert.deepEqual(spotsFor(asked, startsOf(asked)[0], at(DAY, 10)).groupCounts, []);
  const named = mk(true);
  assert.equal(spotsFor(named, startsOf(named)[0], at(DAY, 10)).named, true);
  assert.equal(spotsFor(flexible(), startsOf(flexible())[0], at(DAY, 10)), null);
});

test('the spot word is the floor plan\'s; the label\'s only when the plan has none', () => {
  const rows = hourly(DAY, 8, 12);
  const mk = (spotWord) => build([container([period(DAY, rows, at(DAY, 12), { starts: starts(rows, at(DAY, 12), 2) })],
    { consumption_mode: 'shared', floor_plan: { ...plan(false), spot_word: spotWord } })]).containers[0];
  const seat = mk('seat');
  assert.equal(spotsFor(seat, startsOf(seat)[0], at(DAY, 10)).count, '3 of 3 free');
  assert.equal(spotsFor(seat, startsOf(seat)[0], at(DAY, 10)).word, 'seat');
  const none = mk('');
  assert.equal(spotsFor(none, startsOf(none)[0], at(DAY, 10)).count, '3 of 3 free');
  assert.equal(spotsFor(none, startsOf(none)[0], at(DAY, 10)).word, 'spot');
});

test('picks: none or exactly the places asked for; a new pick over the limit drops the oldest', () => {
  assert.equal(picksOk([], 2), true);
  assert.equal(picksOk([1], 2), false);
  assert.equal(picksOk([1, 3], 2), true);
  assert.deepEqual(togglePick([], 1, 2), [1]);
  assert.deepEqual(togglePick([1], 3, 2), [1, 3]);
  assert.deepEqual(togglePick([1, 3], 2, 2), [3, 2]);
  assert.deepEqual(togglePick([1, 3], 3, 2), [1]);
});

test('price query and booking: places only for a shared container, spots and group when chosen', () => {
  const one = flexible();
  const shared = flexible({ consumption_mode: 'shared' });
  const filter = { category: 4, quantity: 3, group: null };
  assert.equal(quantityOf(one, filter), 1);
  assert.equal(quantityOf(shared, filter), 3);
  const query = priceQuery({ container: one, filter, start: at(DAY, 8), end: at(DAY, 10), picks: [] });
  assert.deepEqual(query, { container: 7, category: 4, quantity: 1, start: at(DAY, 8), end: at(DAY, 10) });
  assert.deepEqual(bookingBody(query, 500), { ...query, total: 500 });
  const picked = priceQuery({ container: shared, filter: { ...filter, group: 3 }, start: at(DAY, 8), end: at(DAY, 10), picks: [1, 2, 3] });
  assert.deepEqual(picked.spots, [1, 2, 3]);
  assert.equal(picked.group, 3);
  assert.deepEqual(bookingBody(picked, 900).spots, [1, 2, 3]);
  assert.equal(bookingBody(picked, 900).group, 3);
});

test('money is in whole kroner when whole, with the minor unit read as 1/100', () => {
  assert.match(moneyWords(25000, 'NOK'), /250/);
  assert.match(moneyWords(25050, 'NOK'), /250,50/);
});

test('price lines: by length "Up to 2 h" / "Next 1 h"; by time of day as hours', () => {
  const price = {
    available: true, currency: 'NOK', tiers_go_by: 'length', total: 40000,
    lines: [
      { up_to: 120, paid_per: 60, times_paid: 2, amount: 10000, total: 20000, group: null },
      { up_to: 180, paid_per: 60, times_paid: 1, amount: 20000, total: 20000, group: null },
    ],
  };
  const view = priceView(price);
  assert.deepEqual(view.lines.map((l) => l.what), ['Up to 2 h', 'Next 1 h']);
  assert.match(view.lines[0].how, /^2 × .* per started 1 h$/);
  assert.equal(view.lines[1].how, null);
  assert.match(view.total, /400/);
  const free = priceView({ ...price, total: 0, lines: [] });
  assert.equal(free.total, null);
  const byTime = priceView({ ...price, tiers_go_by: 'time of day', lines: [{ up_to: 960, paid_per: 60, times_paid: 1, amount: 1, total: 1, group: null }] });
  assert.equal(byTime.lines[0].what, '00:00–16:00');
});

test('priceView: a line by time of day is the booked part of its tier, over midnight too', () => {
  const tier = (up_to) => ({ up_to, paid_per: 240, times_paid: 1, amount: 50000, total: 50000, group: null });
  const day = (hour, minute = 0, date = '2026-11-20') => `${date}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00+01:00`;
  const price = { tiers_go_by: 'time of day', currency: 'NOK', total: 50000, lines: [tier(1020)] };
  assert.deepEqual(priceView(price, { start: day(10), end: day(15) }).lines.map((l) => l.what), ['10:00–15:00']);
  const two = { ...price, lines: [tier(1020), tier(1320)] };
  assert.deepEqual(priceView(two, { start: day(16), end: day(19) }).lines.map((l) => l.what), ['16:00–17:00', '17:00–19:00']);
  const night = { ...price, lines: [tier(1440), tier(1020)] };
  assert.deepEqual(priceView(night, { start: day(22), end: day(2, 0, '2026-11-21') }).lines.map((l) => l.what), ['22:00–00:00', '00:00–02:00']);
  // A fixed night's tiers run on past midnight (pricing.py: up to its period_end).
  const fixed = { ...price, lines: [tier(1440), tier(2040)] };
  assert.deepEqual(priceView(fixed, { start: day(22), end: day(10, 0, '2026-11-21') }).lines.map((l) => l.what), ['22:00–00:00', '00:00–10:00']);
});

test('quoteOf: a refused time carries the server\'s text', () => {
  assert.deepEqual(quoteOf({ kind: 'refused', status: 200, code: 'taken', text: 'Someone just booked that.' }),
    { kind: 'refused', text: 'Someone just booked that.' });
  assert.deepEqual(quoteOf({ kind: 'refused', text: null }), { kind: 'refused', text: refusedWords() });
  assert.deepEqual(quoteOf({ kind: 'offline' }), { kind: 'offline' });
  assert.equal(quoteOf({ ok: true, data: { total: 1 } }).kind, 'priced');
});

function kept() {
  const store = createStore();
  store.put(keyOf({ a: 1 }, DAY, 31), 'old');
  return store;
}

test('add: success clears the calendar store and hands back the cart', async () => {
  const store = kept();
  const calls = [];
  const add = async (csrf, booking) => { calls.push({ csrf, booking }); return { ok: true, data: { count: 1 } }; };
  const done = await submitAdd({ csrf: 'tok', booking: { container: 7, total: 500 }, add, store });
  assert.deepEqual(done, { kind: 'added', cart: { count: 1 } });
  assert.equal(store.size, 0);
  assert.deepEqual(calls, [{ csrf: 'tok', booking: { container: 7, total: 500 } }]);
});

test('add: a refusal shows the server\'s text and keeps the store', async () => {
  const store = kept();
  const add = async () => ({ kind: 'refused', status: 409, code: 'taken', text: 'That time was just taken.' });
  assert.deepEqual(await submitAdd({ csrf: 't', booking: {}, add, store }), { kind: 'refused', code: 'taken', text: 'That time was just taken.' });
  assert.equal(store.size, 1);
  const bare = async () => ({ kind: 'refused', status: 409, text: null });
  assert.deepEqual(await submitAdd({ csrf: 't', booking: {}, add: bare, store }), { kind: 'refused', code: undefined, text: refusedWords() });
});

test('add: signed out, offline and forbidden are passed on, nothing cleared', async () => {
  const store = kept();
  for (const kind of ['signed-out', 'offline', 'forbidden']) {
    const done = await submitAdd({ csrf: 't', booking: {}, add: async () => ({ kind }), store });
    assert.deepEqual(done, { kind });
  }
  assert.equal(store.size, 1);
});

test('a refused add (price-changed, taken) shows the server\'s text, keeps the store and asks the price again', async () => {
  for (const text of ['The price has changed to 600 kr.', 'That time was just taken.']) {
    const store = kept();
    const add = async () => ({ kind: 'refused', status: 409, code: 'x', text, body: { total: 60000 } });
    const done = await submitAdd({ csrf: 't', booking: {}, add, store });
    assert.deepEqual(afterFailure(done), { text, requote: true, held: false });
    assert.equal(store.size, 1);
  }
  assert.deepEqual(afterFailure({ kind: 'offline' }), { text: refusedWords(), requote: true, held: false });
});

test('a refused add while an order waits for payment says so and is marked held', async () => {
  const add = async () => ({ kind: 'refused', status: 409, code: 'order-held', text: 'a booking is waiting for payment', body: {} });
  const done = await submitAdd({ csrf: 't', booking: {}, add, store: kept() });
  assert.deepEqual(afterFailure(done), { text: 'a booking is waiting for payment', requote: true, held: true });
});

test('a container with no choice has no button', () => {
  const rows = hourly(DAY, 8, 12);
  const none = build([container([period(DAY, rows, at(DAY, 12), { starts: [] })])]).containers[0];
  assert.equal(canBook(none), false);
  assert.equal(canBook(flexible()), true);
});

test('cart lines match their container by local date: clock change and a night across midnight', () => {
  const change = '2026-10-25';
  const c = { id: 7, periods: [] };
  const cart = { bookings: [
    // 02:00 happens twice on 2026-10-25; the second is at +01:00.
    { id: 'a', container: { id: 7 }, start: '2026-10-25T02:00:00+01:00', end: '2026-10-25T04:00:00+01:00' },
    { id: 'b', container: { id: 7 }, start: '2026-10-25T01:00:00+02:00', end: '2026-10-25T02:00:00+01:00' },
    { id: 'n', container: { id: 7 }, start: '2026-10-24T17:00:00+02:00', end: '2026-10-25T10:00:00+01:00' },
  ] };
  assert.deepEqual(cartBlocks(c, change, cart).map((b) => b.key), ['cart|a', 'cart|b']);
  const night = cartBlocks(c, '2026-10-24', cart);
  assert.deepEqual(night.map((b) => [b.key, b.hours]), [['cart|n', '17:00–10:00 next day']]);
  assert.deepEqual(cartBlocks(c, '2026-10-26', cart), []);
});

import { priceRows } from './add.js';
import { label } from './labels.js';

const tierLine = (up_to, paid_per, times_paid, amount) => ({ up_to, paid_per, times_paid, amount, total: amount * times_paid, group: null });
const sheet = (hour, to) => ({ start: `2026-10-26T${hour}:00+01:00`, end: `2026-10-26T${to}:00+01:00` });

test('price lines: each line has its hours, its tier and how it is paid, where that differs from the hours used', () => {
  const byLength = { currency: 'NOK', tiers_go_by: 'length', lines: [tierLine(240, 240, 1, 25000), tierLine(480, 60, 1, 10000)] };
  assert.deepEqual(priceRows(byLength, sheet('10:00', '15:00')), [
    { span: '10:00–14:00', hours: '4 h', note: 'Up to 4 h', total: '250 kr' },
    { span: '14:00–15:00', hours: '1 h', note: 'After 4 h', total: '100 kr' },
  ]);
  // Paid per started 4 h while 3 h are used: the line says so.
  const part = { currency: 'NOK', tiers_go_by: 'length', lines: [tierLine(240, 240, 1, 25000)] };
  assert.deepEqual(priceRows(part, sheet('10:00', '13:00')), [
    { span: '10:00–13:00', hours: '3 h', note: 'Up to 4 h · 250 kr per started 4 h', total: '250 kr' },
  ]);
  // Paid twice over: "2 × 100 kr per started 2 h".
  const twice = { currency: 'NOK', tiers_go_by: 'length', lines: [tierLine(240, 120, 2, 10000)] };
  assert.equal(priceRows(twice, sheet('10:00', '14:00'))[0].note, 'Up to 4 h · 2 × 100 kr per started 2 h');
});

test('price lines by time of day: the clock span of each tier inside the booking', () => {
  const byClock = { currency: 'NOK', tiers_go_by: 'time of day', lines: [tierLine(1020, 240, 1, 25000), tierLine(1440, 240, 1, 40000)] };
  assert.deepEqual(priceRows(byClock, sheet('16:00', '19:00')), [
    { span: '16:00–17:00', hours: '1 h', note: '250 kr per started 4 h', total: '250 kr' },
    { span: '17:00–19:00', hours: '2 h', note: '400 kr per started 4 h', total: '400 kr' },
  ]);
});

test('priceView carries the rows only for a priced booking', () => {
  const price = { currency: 'NOK', tiers_go_by: 'length', total: 25000, lines: [tierLine(240, 240, 1, 25000)] };
  assert.equal(priceView(price).rows, null);
  assert.equal(priceView(price, sheet('10:00', '14:00')).rows.length, 1);
  assert.equal(label('add.total-for', { length: '4 h' }), 'Total for 4 h');
});

// the end that stops early says why, only where the strip shows nothing.
const choiceAt = (one, from, to) => ({ start: startsOf(one).find((row) => row.at === at(DAY, from)), end: at(DAY, to) });

test('end stop: closed time ahead says "Closed from"', () => {
  const rows = hourly(DAY, 8, 16);
  const one = build([container([period(DAY, rows, at(DAY, 16), { closed_rows: [4, 5], starts: starts(rows, at(DAY, 16), 2) })])]).containers[0];
  assert.deepEqual(endStop(one, choiceAt(one, 8, 12)), { kind: 'closed', at: at(DAY, 12) });
  assert.equal(endStopWords(one, choiceAt(one, 8, 12)), 'Closed from 12:00');
  // Not the longest end the start allows: nothing.
  assert.equal(endStop(one, choiceAt(one, 8, 10)), null);
});

test('end stop: a gap in the hours is closed time', () => {
  const rows = [...hourly(DAY, 8, 12), ...hourly(DAY, 14, 16)];
  const ends = [...hourly(DAY, 9, 12), at(DAY, 12), ...hourly(DAY, 15, 16), at(DAY, 16)];
  const one = build([container([{ ...period(DAY, rows, at(DAY, 16), { starts: starts(rows, at(DAY, 16), 2) }), row_ends: ends }])]).containers[0];
  assert.equal(endStopWords(one, choiceAt(one, 8, 12)), 'Closed from 12:00');
});

test('end stop: the longest length with free time ahead says "At most"', () => {
  const one = flexible();
  assert.deepEqual(endStop(one, choiceAt(one, 8, 12)), { kind: 'maximum', minutes: 240 });
  assert.equal(endStopWords(one, choiceAt(one, 8, 12)), 'At most 4 h');
  // The longest the member can pick, not the container's raw maximum.
  assert.equal(endStopWords(flexible({ maximum_length: 270 }), choiceAt(flexible({ maximum_length: 270 }), 8, 12)), 'At most 4 h');
  // Shorter than the longest: nothing.
  assert.equal(endStop(one, choiceAt(one, 8, 11)), null);
});

test('end stop: nothing at the day\'s end, nor for a booking, an event or a block', () => {
  const one = flexible();
  assert.equal(endStop(one, choiceAt(one, 12, 16)), null);
  assert.equal(endStop(one, choiceAt(one, 14, 16)), null);
  const rows = hourly(DAY, 8, 16);
  for (const [state, mine] of [['confirmed', false], ['held', false], ['confirmed', true], ['event', false], ['block', false]]) {
    const taken = [{ spot: null, first: 4, last: 7, state, mine }];
    const two = build([container([period(DAY, rows, at(DAY, 16), { taken, starts: starts(rows, at(DAY, 16), 2) })])]).containers[0];
    assert.equal(endStop(two, choiceAt(two, 8, 12)), null, state);
  }
});

test('end stop: a night or another fixed container has none', () => {
  const rows = [at(DAY, 17)];
  const night = build([container([period(DAY, rows, '2026-10-21T10:00:00+02:00', {
    starts: [{ row: 0, end: '2026-10-21T10:00:00+02:00', spots: [], free: [] }] })], { period_type: 'fixed' })]).containers[0];
  assert.equal(endStop(night, firstChoice(night)), null);
  const fixed = build([container([period(DAY, hourly(DAY, 8, 16), at(DAY, 16), {
    starts: [{ row: 0, end: at(DAY, 10), spots: [], free: [] }, { row: 4, end: at(DAY, 12), spots: [], free: [] }] })], { period_type: 'fixed' })]).containers[0];
  assert.equal(endStopWords(fixed, firstChoice(fixed)), null);
});

test('end stop: hours of the category say "Not as", a closed row says "Closed", also when both', () => {
  const rows = hourly(DAY, 8, 16);
  const hours = (spans) => ({ category_hours: [{ category: 1, spans }] });
  const as = (one) => buildCalendar({ worked_out_at: '2026-10-19T08:00:00+02:00', containers: [one] }, { start: DAY, days: 1, category: 1 }).dates[0].containers[0];
  const inside = [[at(DAY, 8), at(DAY, 12)]];
  const base = (extra) => as(container([period(DAY, rows, at(DAY, 16), { starts: starts(rows, at(DAY, 16), 2), ...extra })]));
  const byHours = base(hours(inside));
  assert.deepEqual(endStop(byHours, choiceAt(byHours, 8, 12)), { kind: 'category', at: at(DAY, 12) });
  assert.equal(endStopWords(byHours, choiceAt(byHours, 8, 12), 'Work'), 'Not as Work from 12:00');
  const both = base({ ...hours(inside), closed_rows: [4, 5] });
  assert.equal(endStopWords(both, choiceAt(both, 8, 12), 'Work'), 'Closed from 12:00');
  assert.equal(both.periods[0].rows[4].closed, true);
  assert.equal(byHours.periods[0].rows[4].closed, false);
});

test('end stop: a period that starts where this one ends gives no line; nothing there is closed', () => {
  const first = hourly(DAY, 8, 12);
  const second = hourly(DAY, 12, 16);
  const touching = build([container([
    period(DAY, first, at(DAY, 12), { starts: starts(first, at(DAY, 12)) }),
    period(DAY, second, at(DAY, 16), { starts: starts(second, at(DAY, 16)) }),
  ])]).containers[0];
  assert.equal(endStop(touching, choiceAt(touching, 8, 12)), null);
  assert.equal(endStopWords(touching, choiceAt(touching, 8, 12)), null);
  const apart = build([container([
    period(DAY, first, at(DAY, 12), { starts: starts(first, at(DAY, 12)) }),
    period(DAY, hourly(DAY, 13, 16), at(DAY, 16), { starts: [] }),
  ])]).containers[0];
  assert.equal(endStopWords(apart, choiceAt(apart, 8, 12)), 'Closed from 12:00');
  // Over midnight: the first period ends at 00:00, the next date's starts there.
  const next = '2026-10-21';
  const late = hourly(DAY, 20, 24);
  const midnight = build([container([
    period(DAY, late, `${next}T00:00:00+02:00`, { starts: starts(late, `${next}T00:00:00+02:00`) }),
    period(next, hourly(next, 0, 4), at(next, 4), { starts: starts(hourly(next, 0, 4), at(next, 4)) }),
  ])]).containers[0];
  assert.equal(endStop(midnight, { start: startsOf(midnight)[0], end: `${next}T00:00:00+02:00` }), null);
});
