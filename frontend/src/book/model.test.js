// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { addDays, askWithLength, buildCalendar } from './model.js';
import { label } from './labels.js';
import { bookingBlocks, cartBlocks } from './words.js';

const hourly = (day, from, to, offset = '+02:00') =>
  Array.from({ length: to - from }, (_, i) => `${day}T${String(from + i).padStart(2, '0')}:00:00${offset}`);

function container(periods, extra = {}) {
  return {
    id: 1, number: 1, asset: { id: 1, name: 'Court' }, type: { id: 1, name: 'Play' },
    categories: [1, 2], period_type: 'flexible', floor_plan: null, periods, ...extra,
  };
}

function period(day, rows, end, extra = {}) {
  const starts = extra.starts
    ?? [{ row: 0, end: rows[1] ?? end, spots: [], free: [] }];
  return {
    day, rows, end, row_ends: [...rows.slice(1), end], closed_rows: [],
    taken: [], category_hours: [], ...extra, starts,
  };
}

const answerOf = (containers, at = '2026-10-19T08:00:00+02:00') => ({ worked_out_at: at, containers });
const ask = { start: '2026-10-20', days: 1 };

test('addDays crosses month ends', () => {
  assert.equal(addDays('2026-10-31', 1), '2026-11-01');
  assert.equal(addDays('2026-10-20', 0), '2026-10-20');
});

test('taken and free stretches say whose; another member\'s booking carries only the name', () => {
  const day = '2026-10-20';
  const taken = [
    { spot: null, first: 1, last: 2, state: 'confirmed', mine: true },
    { spot: null, first: 3, last: 3, state: 'held', mine: false },
    { spot: null, first: 4, last: 4, state: 'confirmed', mine: false, name: 'Ann Berg' },
    { spot: null, first: 5, last: 5, state: 'block', mine: false },
  ];
  const result = buildCalendar(
    answerOf([container([period(day, hourly(day, 8, 16), `${day}T16:00:00+02:00`, { taken })])]),
    ask,
  );
  const stretches = result.dates[0].containers[0].periods[0].columns[0].stretches;
  assert.deepEqual(stretches.map((s) => [s.kind, s.minutes]), [
    ['free', 60], ['yours', 120], ['held', 60], ['other', 60], ['block', 60], ['free', 120],
  ]);
  const column = result.dates[0].containers[0].periods[0].columns[0];
  assert.deepEqual(column.names.filter(Boolean), ['Ann Berg']);
  assert.equal(JSON.stringify(result).includes('@'), false);
  assert.equal(result.dates[0].summary.mine, true);
});

test('the member\'s own booking still waiting for payment is the cart\'s, not yours', () => {
  const day = '2026-10-20';
  const taken = [{ spot: null, first: 1, last: 2, state: 'held', mine: true }];
  const result = buildCalendar(
    answerOf([container([period(day, hourly(day, 8, 16), `${day}T16:00:00+02:00`, { taken })])]),
    ask,
  );
  const stretches = result.dates[0].containers[0].periods[0].columns[0].stretches;
  assert.deepEqual(stretches.map((s) => [s.kind, s.minutes]), [['free', 60], ['cart', 120], ['free', 300]]);
  assert.equal(result.dates[0].summary.mine, false);
  assert.equal(result.dates[0].summary.cart, true);
  // Drawn from the calendar, once: the cart answer's row for it is left out.
  const built = result.dates[0].containers[0];
  assert.deepEqual(bookingBlocks(built).map((b) => [b.kind, b.word]), [['cart', label('state.cart')]]);
  const row = { id: 'r', container: { id: built.id }, start: stretches[1].from, end: stretches[1].to };
  assert.deepEqual(cartBlocks(built, day, { bookings: [row] }), []);
});

test('shared container keeps one column per spot', () => {
  const day = '2026-10-20';
  const plan = { name: 'Hall', spot_word: 'table', groups: [], spots: [{ id: 5, number: 1, name: 'A', group: null }, { id: 6, number: 2, name: 'B', group: null }] };
  const taken = [{ spot: 2, first: 0, last: 3, state: 'confirmed', mine: false }];
  const result = buildCalendar(
    answerOf([container([period(day, hourly(day, 8, 12), `${day}T12:00:00+02:00`, { taken })], { floor_plan: plan })]),
    ask,
  );
  const columns = result.dates[0].containers[0].periods[0].columns;
  assert.deepEqual(columns.map((c) => [c.spot, c.stretches.map((s) => s.kind)]), [[1, ['free']], [2, ['other']]]);
  assert.equal(result.dates[0].summary.share, 0.5);
  assert.equal(result.dates[0].summary.state, 'open');
});

test('month summary: share left, full, closed, past', () => {
  const day = '2026-10-20';
  const full = [{ spot: null, first: 0, last: 3, state: 'confirmed', mine: false }];
  const half = [{ spot: null, first: 0, last: 1, state: 'confirmed', mine: false }];
  const rows = hourly(day, 8, 12);
  const end = `${day}T12:00:00+02:00`;
  const make = (taken) => buildCalendar(answerOf([container([period(day, rows, end, { taken })])]), ask).dates[0].summary;
  assert.deepEqual(make(half), { share: 0.5, mine: false, cart: false, state: 'open' });
  assert.deepEqual(make(full), { share: 0, mine: false, cart: false, state: 'full' });
  const empty = buildCalendar(answerOf([]), { start: day, days: 2 });
  assert.deepEqual(empty.dates.map((d) => d.summary.state), ['closed', 'closed']);
  const past = buildCalendar(answerOf([container([period(day, rows, end)])], '2026-10-22T09:00:00+02:00'), ask);
  assert.equal(past.dates[0].summary.state, 'past');
});

test('rows already over do not count toward what is left', () => {
  const day = '2026-10-20';
  const taken = [{ spot: null, first: 0, last: 1, state: 'confirmed', mine: false }];
  const result = buildCalendar(
    answerOf([container([period(day, hourly(day, 8, 12), `${day}T12:00:00+02:00`, { taken })])], `${day}T10:00:00+02:00`),
    ask,
  );
  assert.equal(result.dates[0].summary.share, 1);
});

test('time outside the category hours is blank, not free', () => {
  const day = '2026-10-20';
  const category_hours = [{ category: 2, spans: [[`${day}T10:00:00+02:00`, `${day}T12:00:00+02:00`]] }];
  const base = [container([period(day, hourly(day, 8, 14), `${day}T14:00:00+02:00`, { category_hours })])];
  const kinds = (category) =>
    buildCalendar(answerOf(base), { ...ask, category }).dates[0].containers[0].periods[0].columns[0].stretches
      .map((s) => [s.kind, s.minutes]);
  assert.deepEqual(kinds(2), [['blank', 120], ['free', 120], ['blank', 120]]);
  assert.deepEqual(kinds(1), [['free', 360]]);
  assert.equal(buildCalendar(answerOf(base), { ...ask, category: 2 }).dates[0].summary.share, 1);
});

test('a start the server did not send is not made up', () => {
  const day = '2026-10-20';
  const result = buildCalendar(answerOf([container([period(day, hourly(day, 8, 12), `${day}T12:00:00+02:00`, { starts: [] })])]), ask);
  assert.deepEqual(result.dates[0].containers[0].periods[0].starts, []);
});

test('autumn: the repeated hour stays two distinct rows', () => {
  const day = '2026-10-25';
  const rows = [`${day}T01:00:00+02:00`, `${day}T02:00:00+02:00`, `${day}T02:00:00+01:00`, `${day}T03:00:00+01:00`];
  const end = `${day}T04:00:00+01:00`;
  const taken = [{ spot: null, first: 1, last: 2, state: 'confirmed', mine: false }];
  const result = buildCalendar(
    answerOf([container([period(day, rows, end, { taken })])]),
    { start: day, days: 1 },
  );
  const built = result.dates[0].containers[0].periods[0];
  assert.deepEqual(built.rows.map((r) => [r.at, r.end]), [
    [rows[0], rows[1]], [rows[1], rows[2]], [rows[2], rows[3]], [rows[3], end],
  ]);
  const stretches = built.columns[0].stretches;
  assert.deepEqual(stretches.map((s) => [s.kind, s.from, s.to, s.minutes]), [
    ['free', rows[0], rows[1], 60],
    ['other', rows[1], rows[3], 120],
    ['free', rows[3], end, 60],
  ]);
  assert.notEqual(built.rows[1].at, built.rows[2].at);
});

test('a period that starts before the query dates belongs to its own date', () => {
  const day = '2026-10-19';
  const result = buildCalendar(
    answerOf([container([period(day, hourly(day, 22, 24), '2026-10-20T02:00:00+02:00')])]),
    ask,
  );
  assert.equal(result.dates[0].containers.length, 0);
});

// The exact field set of booking_views._calendar.
function serverContainer(periods) {
  return {
    id: 7, number: 2,
    asset: { id: 3, name: 'Court', maximum_occupancy: 4 },
    type: { id: 1, name: 'Play' },
    categories: [1, 2], period_type: 'flexible', consumption_mode: 'shared',
    with_payment: true,
    prices: {
      currency: 'NOK',
      categories: [{ category: 1, billing_basis: 'time', tiers_go_by: 'length', tiers: [{ up_to: 480, paid_per: 60, amount: 10000 }], groups: [] }],
    },
    interval: 60, minimum_length: 60, maximum_length: 480,
    floor_plan: {
      name: 'Hall', spot_word: 'table',
      groups: [{ id: 1, number: 1, name: 'North', word: 'section' }],
      spots: [{ id: 11, number: 1, name: 'A', group: 1 }, { id: 12, number: 2, name: 'B', group: null }],
    },
    max_quantity: 2,
    periods,
  };
}

const at = (day, hour, offset = '+02:00') => `${day}T${String(hour).padStart(2, '0')}:00:00${offset}`;

test('overlap case, server-shaped: 8-hour answer on Tuesday with 15-18 taken', () => {
  const day = '2026-10-20';
  const rows = hourly(day, 6, 22);
  const taken = [
    { spot: 1, first: 9, last: 11, state: 'confirmed', mine: false },
    { spot: 2, first: 9, last: 11, state: 'confirmed', mine: false },
  ];
  // Rows 0 and 1 (06:00, 07:00) are the only 8-hour starts the server leaves.
  const starts = [0, 1].map((row) => ({
    row, end: at(day, 14 + row),
    spots: [1], free: [{ group: 1, spots: [1] }, { group: null, spots: [2] }],
  }));
  const answer = answerOf([serverContainer([{
    day, rows, end: at('2026-10-21', 2), row_ends: [...rows.slice(1), at(day, 22)].map((v, i, all) => (i === all.length - 1 ? at('2026-10-21', 2) : v)),
    closed_rows: [], taken, starts,
    category_hours: [{ category: 2, spans: [[at(day, 6), at(day, 22)]] }],
  }])]);
  const built = buildCalendar(answer, { ...ask, category: 1 }).dates[0].containers[0].periods[0];
  assert.deepEqual(built.starts.map((s) => s.at), [at(day, 6), at(day, 7)]);
  const busy = built.columns.flatMap((c) => c.stretches).filter((s) => s.kind === 'other');
  assert.equal(busy.length, 2);
  for (const start of built.starts) {
    for (const span of busy) {
      const overlaps = Date.parse(start.at) < Date.parse(span.to) && Date.parse(span.from) < Date.parse(start.end);
      assert.equal(overlaps, false);
    }
  }
  assert.equal(built.to, at('2026-10-21', 2));
});

test('spring clock change: the skipped 02:00 hour is no row, the 01:00 row lasts one hour', () => {
  const day = '2026-03-29';
  const rows = [at(day, 0, '+01:00'), at(day, 1, '+01:00'), at(day, 3, '+02:00'), at(day, 4, '+02:00')];
  const end = at(day, 5, '+02:00');
  const result = buildCalendar(
    answerOf([container([period(day, rows, end)])], '2026-03-20T08:00:00+01:00'),
    { start: day, days: 1 },
  );
  const built = result.dates[0].containers[0].periods[0];
  assert.deepEqual(built.rows.map((r) => (Date.parse(r.end) - Date.parse(r.at)) / 60000), [60, 60, 60, 60]);
  assert.equal(built.columns[0].stretches[0].minutes, 240);
});

test('row_ends are used as sent, and closed rows are blank and not counted', () => {
  const day = '2026-10-20';
  const rows = hourly(day, 8, 12);
  const row_ends = [at(day, 9), at(day, 10), at(day, 11, '+02:00'), at(day, 11, '+02:00').replace('11:00', '11:30')];
  const result = buildCalendar(
    answerOf([container([period(day, rows, at(day, 12), { row_ends, closed_rows: [0] })])]),
    ask,
  );
  const stretches = result.dates[0].containers[0].periods[0].columns[0].stretches;
  assert.deepEqual(stretches.map((s) => [s.kind, s.minutes]), [['blank', 60], ['free', 150]]);
  assert.equal(result.dates[0].summary.share, 1);
});

test('a row that has begun is not free time ahead; today fully over is past', () => {
  const day = '2026-10-20';
  const rows = hourly(day, 8, 12);
  const taken = [{ spot: null, first: 3, last: 3, state: 'confirmed', mine: false }];
  const half = buildCalendar(
    answerOf([container([period(day, rows, at(day, 12), { taken })])], at(day, 9, '+02:00').replace('09:00:00', '09:30:00')),
    ask,
  ).dates[0].summary;
  assert.equal(half.share, 0.5 * 1);
  assert.equal(half.state, 'open');
  const over = buildCalendar(
    answerOf([container([period(day, rows, at(day, 12))])], at(day, 13)),
    ask,
  ).dates[0].summary;
  assert.equal(over.state, 'past');
});

test('free time but no start in any period is full, never open', () => {
  const day = '2026-10-20';
  const result = buildCalendar(
    answerOf([container([period(day, hourly(day, 8, 12), at(day, 12), { starts: [] })])]),
    ask,
  );
  assert.equal(result.dates[0].summary.share, 1);
  assert.equal(result.dates[0].summary.state, 'full');
});

test('a stretch breaks where the hours break off, and counts only open time', () => {
  const day = '2026-10-20';
  const rows = hourly(day, 8, 12);
  const row_ends = [at(day, 9), `${day}T09:30:00+02:00`, at(day, 11), at(day, 12)];
  const result = buildCalendar(answerOf([container([period(day, rows, at(day, 12), { row_ends })])]), ask);
  const stretches = result.dates[0].containers[0].periods[0].columns[0].stretches;
  assert.deepEqual(stretches.map((s) => [s.kind, s.from, s.to, s.minutes]), [
    ['free', rows[0], `${day}T09:30:00+02:00`, 90],
    ['free', rows[2], at(day, 12), 120],
  ]);
});

test('a category id held as text blanks the same hours as a number', () => {
  const day = '2026-10-20';
  const category_hours = [{ category: 5, spans: [] }];
  const base = [container([period(day, hourly(day, 8, 10), at(day, 10), { category_hours })])];
  for (const category of [5, '5']) {
    const kinds = buildCalendar(answerOf(base), { ...ask, category }).dates[0].containers[0].periods[0].columns[0].kinds;
    assert.deepEqual(kinds, ['blank', 'blank']);
  }
});

test('the answer carries the server\'s moment as "now", for the screens', () => {
  const result = buildCalendar(answerOf([], '2026-10-19T08:00:00+02:00'), ask);
  assert.equal(result.now, Date.parse('2026-10-19T08:00:00+02:00'));
  assert.equal(result.today, '2026-10-19');
});

test('a container keeps the prices the answer sent, none when it takes no payment', () => {
  const day = '2026-10-20';
  const rows = hourly(day, 8, 10);
  const priced = serverContainer([{
    day, rows, end: at(day, 10), row_ends: [rows[1], at(day, 10)], closed_rows: [], taken: [],
    starts: [], category_hours: [],
  }]);
  const free = { ...priced, id: 8, with_payment: false, prices: null };
  const result = buildCalendar(answerOf([priced, free]), ask);
  const [first, second] = result.dates[0].containers;
  assert.equal(first.prices.currency, 'NOK');
  assert.equal(first.prices.categories[0].tiers[0].amount, 10000);
  assert.equal(second.prices, null);
});

import { kindOf } from './model.js';
import { kindsSeen } from './month.js';

// The server's word for a taken cell (member_calendar.py STATES) to the calendar's.
test('kindOf: one row per server state, mine or not', () => {
  const rows = [
    [{ state: 'confirmed', mine: false }, 'other'],
    [{ state: 'confirmed', mine: true }, 'yours'],
    [{ state: 'held', mine: false }, 'held'],
    [{ state: 'held', mine: true }, 'cart'],
    [{ state: 'block', mine: false }, 'block'],
    [{ state: 'event', mine: false }, 'event'],
    [{ state: 'event', mine: true }, 'event'],
  ];
  for (const [span, kind] of rows) assert.equal(kindOf(span), kind, JSON.stringify(span));
});

test('an event the server sends is an event stretch and a legend entry; none sent, none drawn', () => {
  const day = '2026-10-20';
  const taken = [{ spot: null, first: 2, last: 3, state: 'event', mine: false }];
  const make = (list) => buildCalendar(
    answerOf([container([period(day, hourly(day, 8, 12), `${day}T12:00:00+02:00`, { taken: list })])]), ask);
  const withEvent = make(taken);
  const stretches = withEvent.dates[0].containers[0].periods[0].columns[0].stretches;
  assert.deepEqual(stretches.map((s) => [s.kind, s.minutes]), [['free', 120], ['event', 120]]);
  assert.equal(kindsSeen(withEvent.dates).has('event'), true);
  assert.deepEqual(bookingBlocks(withEvent.dates[0].containers[0]).map((b) => [b.kind, b.word]), [['event', 'Event']]);
  const without = make([]);
  assert.equal(kindsSeen(without.dates).has('event'), false);
  assert.deepEqual(bookingBlocks(without.dates[0].containers[0]), []);
});

test('a block is drawn as Closed, never as Booked', () => {
  const day = '2026-10-20';
  const taken = [{ spot: null, first: 0, last: 1, state: 'block', mine: false }];
  const built = buildCalendar(answerOf([container([period(day, hourly(day, 8, 10), `${day}T10:00:00+02:00`, { taken })])]), ask);
  assert.deepEqual(bookingBlocks(built.dates[0].containers[0]).map((b) => [b.kind, b.word]), [['closed', 'Closed']]);
  assert.equal(kindsSeen(built.dates).has('block'), true);
});

test('an event is worded by its title and opens by its id; two events side by side are two', () => {
  const day = '2026-10-20';
  const lunch = { id: 7, title: 'Neighbours’ lunch' };
  const board = { id: 8, title: 'Board meeting' };
  const taken = [
    { spot: null, first: 1, last: 2, state: 'event', mine: false, event: lunch },
    { spot: null, first: 3, last: 3, state: 'event', mine: false, event: board },
  ];
  const built = buildCalendar(
    answerOf([container([period(day, hourly(day, 8, 14), `${day}T14:00:00+02:00`, { taken })])]), ask);
  const column = built.dates[0].containers[0].periods[0].columns[0];
  assert.deepEqual(column.stretches.filter((s) => s.kind === 'event').map((s) => [s.event.id, s.minutes]), [[7, 120], [8, 60]]);
  const blocks = bookingBlocks(built.dates[0].containers[0]);
  assert.deepEqual(blocks.map((b) => [b.kind, b.word, b.event.id, b.hours]), [
    ['event', 'Neighbours’ lunch', 7, '09:00–11:00'], ['event', 'Board meeting', 8, '11:00–12:00'],
  ]);
  // A booking carries no event, and neither does an event the server sent no title for.
  const plain = buildCalendar(
    answerOf([container([period(day, hourly(day, 8, 14), `${day}T14:00:00+02:00`, { taken: [
      { spot: null, first: 1, last: 1, state: 'confirmed', mine: false }, { spot: null, first: 3, last: 3, state: 'event', mine: false },
    ] })])]), ask);
  const words = bookingBlocks(plain.dates[0].containers[0]);
  assert.deepEqual(words.map((b) => [b.kind, b.word, 'event' in b]), [['other', 'Booked', false], ['event', 'Event', false]]);
});
