// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildCalendar } from './model.js';
import {
  dayLines, dayRuns, legendSeen, lineSpan, nightMark,
} from './dayline.js';

const DAY = '2026-11-19';
const NEXT = '2026-11-20';
const at = (hour, day = DAY) => `${day}T${String(hour).padStart(2, '0')}:00:00+01:00`;
const NOW = `${DAY}T06:00:00+01:00`;

// A period of hourly rows from `from` to `to`; `taken` and `starts` by row.
function period(from, to, { taken = [], starts, day = DAY, end = at(to, day), minimum = 1 } = {}) {
  const rows = Array.from({ length: to - from }, (_, i) => at(from + i, day));
  const open = starts || rows.map((_, row) => row);
  const clean = open.filter((row) => !taken.some((span) => span.first <= row && row <= span.last));
  return {
    day, rows, end, row_ends: [...rows.slice(1), end], closed_rows: [], taken,
    starts: clean.map((row) => ({ row, end: row + minimum < rows.length ? rows[row + minimum] : end, spots: [], free: [] })),
    category_hours: [],
  };
}
function offer(id, name, periods, periodType = 'flexible', asset = { id: 1, name: 'The Hall' }) {
  return {
    id, number: id, asset, type: { id, name }, categories: [1], period_type: periodType,
    floor_plan: null, interval: 60, minimum_length: 60, maximum_length: 720, periods,
  };
}
const booked = (first, last, extra = {}) => ({ spot: null, first, last, state: 'confirmed', mine: false, name: 'Sam Holt', ...extra });
const hall = (taken = []) => offer(1, 'Rent the hall', [period(10, 22, { taken })]);
// One fixed period from 22:00 to 10:00 the next day: a night.
const night = (taken = []) => offer(2, 'Overnight stay', [period(22, 23, { taken, end: at(10, NEXT) })], 'fixed');
const entryOf = (containers, day = DAY) => buildCalendar({ worked_out_at: NOW, containers }, { start: day, days: 1 }).dates[0];

test('the line is the day\'s hours where they fall: free, booked 16-21, free again', () => {
  const runs = dayRuns(entryOf([hall([booked(6, 10)]), night()]));
  assert.deepEqual(runs, [
    { state: 'free', from: 600, to: 960 },
    { state: 'other', from: 960, to: 1260 },
    { state: 'free', from: 1260, to: 1320 },
  ]);
});

test('the night is not on the line: the line ends where the day offers end', () => {
  const runs = dayRuns(entryOf([hall(), night([booked(0, 0)])]));
  assert.deepEqual(runs, [{ state: 'free', from: 600, to: 1320 }]);
});

test('yours and an event keep their own states on the line', () => {
  const runs = dayRuns(entryOf([hall([booked(0, 5, { mine: true }), booked(8, 9, { state: 'event', event: { id: 4, title: 'Open house' } })])]));
  assert.deepEqual(runs.map((run) => [run.state, run.from, run.to]), [
    ['yours', 600, 960], ['free', 960, 1080], ['event', 1080, 1200], ['free', 1200, 1320],
  ]);
});

test('the moon: someone else\'s night, yours, none for a free night', () => {
  assert.equal(nightMark(entryOf([hall(), night([booked(0, 0)])])), 'other');
  assert.equal(nightMark(entryOf([hall(), night([booked(0, 0, { state: 'held' })])])), 'other');
  assert.equal(nightMark(entryOf([hall(), night([booked(0, 0, { mine: true })])])), 'yours');
  assert.equal(nightMark(entryOf([hall(), night()])), null);
  assert.equal(nightMark(entryOf([hall([booked(0, 11)])])), null);
});

test('a night in the cart is the cart\'s moon; one someone took first is booked', () => {
  const cart = { bookings: [{ id: 9, container: { id: 2 }, start: at(22), end: at(10, NEXT) }] };
  assert.equal(nightMark(entryOf([hall(), night()]), cart), 'cart');
  assert.equal(nightMark(entryOf([hall(), night([booked(0, 0)])]), cart), 'other');
});

test('a fixed offer\'s day periods stay on the line and only its night is a moon', () => {
  const both = offer(3, 'Sessions', [period(10, 14), period(22, 23, { end: at(10, NEXT), taken: [booked(0, 0)] })], 'fixed');
  const entry = entryOf([both]);
  assert.deepEqual(dayRuns(entry), [{ state: 'free', from: 600, to: 840 }]);
  assert.equal(nightMark(entry), 'other');
});

test('the drawn days\' lines share one span, from the earliest hour to the latest', () => {
  const early = entryOf([offer(5, 'Morning', [period(8, 12, { day: '2026-11-21' })])], '2026-11-21');
  // A passed day carries no line and does not stretch the span.
  const passed = entryOf([offer(6, 'Dawn', [period(6, 23, { day: '2026-11-18' })])], '2026-11-18');
  assert.equal(passed.summary.state, 'past');
  const lines = dayLines([entryOf([hall()]), null, early, passed]);
  assert.deepEqual([...lines.keys()], [DAY, '2026-11-21']);
  assert.deepEqual(lineSpan(lines), { from: 480, to: 1320 });
  assert.equal(lineSpan(new Map()), null);
});

test('with two assets on the page, yours shows over the other\'s free hour, and free over its booking', () => {
  const kitchen = { id: 2, name: 'Kitchen' };
  const yours = entryOf([hall([booked(0, 11, { mine: true })]), offer(3, 'Rent the kitchen', [period(10, 22)], 'flexible', kitchen)]);
  assert.deepEqual(dayRuns(yours), [{ state: 'yours', from: 600, to: 1320 }]);
  const taken = entryOf([hall([booked(0, 11)]), offer(3, 'Rent the kitchen', [period(10, 22)], 'flexible', kitchen)]);
  assert.deepEqual(dayRuns(taken), [{ state: 'free', from: 600, to: 1320 }]);
});

test('the legend names the cart and the night only when one is drawn', () => {
  const seen = (entries, cart = null) => legendSeen(entries, dayLines(entries, cart));
  const plain = seen([entryOf([hall(), night()])]);
  assert.equal(plain.has('cart'), false);
  assert.equal(plain.has('night'), false);
  const cart = { bookings: [{ id: 9, container: { id: 1 }, start: at(12), end: at(14) }] };
  const hours = entryOf([hall(), night()]);
  // The server offers no start in the hours a cart holds.
  for (const start of hours.containers[0].periods[0].starts) if (start.at >= at(12) && start.at < at(14)) start.at = null;
  hours.containers[0].periods[0].starts = hours.containers[0].periods[0].starts.filter((start) => start.at);
  assert.equal(seen([hours], cart).has('cart'), true);
  assert.equal(seen([null, entryOf([hall(), night([booked(0, 0)])])]).has('night'), true);
  const nightCart = { bookings: [{ id: 9, container: { id: 2 }, start: at(22), end: at(10, NEXT) }] };
  assert.equal(seen([entryOf([hall(), night()])], nightCart).has('cart'), true);
  // A cart time someone took first is drawn as booked, so the cart is not named.
  assert.equal(seen([entryOf([hall([booked(2, 3)])])], cart).has('cart'), false);
  // A passed day's night names nothing.
  const passed = entryOf([offer(2, 'Overnight stay', [period(22, 23, { day: '2026-11-18', end: at(10, DAY), taken: [booked(0, 0)] })], 'fixed')], '2026-11-18');
  assert.equal(seen([passed]).has('night'), false);
});
