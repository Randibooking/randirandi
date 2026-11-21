// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildCalendar } from './model.js';
import { setLabels } from './labels.js';
import { joinAnswers, kindsSeen } from './month.js';
import {
  bookingBlocks, closedReason, closedWords, contactHref, dateWords, dayList, emptyDay, freeRanges, hhmm, hoursWords, isEmptyDay, monthTitle, tileName,
} from './words.js';

const at = (day, hour, offset = '+02:00') => `${day}T${String(hour).padStart(2, '0')}:00:00${offset}`;
const hourly = (day, from, to) => Array.from({ length: to - from }, (_, i) => at(day, from + i));

function container(periods, extra = {}) {
  return {
    id: 1, number: 1, asset: { id: 1, name: 'Court' }, type: { id: 1, name: 'Play' },
    categories: [1], period_type: 'flexible', floor_plan: null, periods, ...extra,
  };
}

function period(day, rows, end, extra = {}) {
  return {
    day, rows, end, row_ends: [...rows.slice(1), end], closed_rows: [], taken: [], category_hours: [],
    starts: [{ row: 0, end: rows[1] ?? end, spots: [], free: [] }], ...extra,
  };
}

const DAY = '2026-10-20';
const built = (containers, now = '2026-10-19T08:00:00+02:00') =>
  buildCalendar({ worked_out_at: now, containers }, { start: DAY, days: 1 });

test('words: dates, months and hours as the server sent them', () => {
  assert.equal(dateWords('2026-10-20'), 'Tuesday 20 October');
  assert.equal(dateWords('2026-11-01'), 'Sunday 1 November');
  // The pattern is a label: Norwegian puts a dot after the day.
  setLabels({ 'date.words': '{weekday} {day}. {month}', 'weekdays.sun': 'søndag', 'months.11': 'november' });
  assert.equal(dateWords('2026-11-01'), 'søndag 1. november');
  setLabels({});
  assert.equal(monthTitle('2026-10'), 'October 2026');
  assert.equal(hhmm('2026-10-20T16:00:00+02:00'), '16:00');
  assert.equal(hoursWords(at(DAY, 16), at(DAY, 17)), '16:00–17:00');
  assert.equal(hoursWords(at(DAY, 17), at('2026-10-21', 8)), '17:00–08:00 next day');
});

test('the tile is named by its date and what is left', () => {
  assert.equal(tileName(DAY, { state: 'open', share: 0.5, mine: false }, '2026-10-19'), 'Tuesday 20 October, 50 percent left');
  assert.equal(tileName(DAY, { state: 'open', share: 1, mine: true }, DAY), 'Tuesday 20 October, today, 100 percent left, yours');
  // The moon by the date is spoken: someone else's night.
  assert.equal(tileName(DAY, { state: 'open', share: 0.5, mine: false }, '2026-10-19', { nightBooked: true }), 'Tuesday 20 October, 50 percent left, night booked');
  // The line's free hours are spoken run by run in place of the share (the
  // line itself is hidden from a screen reader); without any, the share stays.
  const runs = [{ state: 'free', from: 600, to: 960 }, { state: 'other', from: 960, to: 1260 }, { state: 'free', from: 1260, to: 1320 }];
  assert.equal(tileName(DAY, { state: 'open', share: 0.58, mine: false }, '2026-10-19', { runs }), 'Tuesday 20 October, free 10:00–16:00, free 21:00–22:00');
  assert.equal(tileName(DAY, { state: 'open', share: 0.5, mine: false }, '2026-10-19', { runs: [{ state: 'other', from: 600, to: 1320 }] }), 'Tuesday 20 October, 50 percent left');
  assert.equal(tileName(DAY, { state: 'full', share: 0, mine: false }, '2026-10-19', { runs: [{ state: 'other', from: 600, to: 1320 }] }), 'Tuesday 20 October, full');
  assert.equal(tileName(DAY, { state: 'full', share: 0, mine: false }, '2026-10-19'), 'Tuesday 20 October, full');
  assert.equal(tileName(DAY, { state: 'closed', share: 0, mine: false }, '2026-10-19'), 'Tuesday 20 October, Nothing to book');
  assert.equal(tileName(DAY, { state: 'past', share: 0, mine: false }, '2026-10-21'), 'Tuesday 20 October, passed');
});

// A start the server offers: its row, and where it ends.
const start = (rows, row, end) => ({ row, end, spots: [], free: [] });

test('free time is what the starts cover, joined where they touch', () => {
  const rows = hourly(DAY, 8, 14);
  const starts = [start(rows, 0, at(DAY, 9)), start(rows, 1, at(DAY, 10)), start(rows, 4, at(DAY, 14))];
  const result = built([container([period(DAY, rows, at(DAY, 14), { starts })])]);
  assert.deepEqual(freeRanges(result.dates[0].containers[0]), [[at(DAY, 8), at(DAY, 10)], [at(DAY, 12), at(DAY, 14)]]);
  assert.deepEqual(dayList(result.dates[0])[0].lines, ['Available 08:00–10:00, 12:00–14:00']);
});

test('a 1-hour gap under a 2-hour minimum is no free time: no start, no Available line', () => {
  const rows = hourly(DAY, 8, 14);
  const taken = [{ spot: null, first: 0, last: 0, state: 'confirmed', mine: false }, { spot: null, first: 2, last: 5, state: 'confirmed', mine: false }];
  const result = built([container([period(DAY, rows, at(DAY, 14), { taken, starts: [] })])]);
  assert.deepEqual(freeRanges(result.dates[0].containers[0]), []);
  assert.deepEqual(dayList(result.dates[0])[0].lines, ['Nothing free']);
});

test('the quantity the server could not seat leaves no start, so no Available line', () => {
  const rows = hourly(DAY, 8, 10);
  const plan = { name: 'Hall', spot_word: 'table', groups: [], spots: [{ id: 1, number: 1, name: 'A', group: null }, { id: 2, number: 2, name: 'B', group: null }] };
  const taken = [{ spot: 1, first: 0, last: 1, state: 'confirmed', mine: false }];
  // Two seats asked: one spot is free, so the server sends no start.
  const result = built([container([period(DAY, rows, at(DAY, 10), { taken, starts: [] })], { floor_plan: plan })]);
  assert.deepEqual(dayList(result.dates[0])[0].lines, ['Nothing free']);
  const one = built([container([period(DAY, rows, at(DAY, 10), { taken, starts: [start(rows, 0, at(DAY, 10))] })], { floor_plan: plan })]);
  assert.deepEqual(dayList(one.dates[0])[0].lines, ['Available 08:00–10:00']);
});

test('booked and nothing free; passed', () => {
  const rows = hourly(DAY, 8, 14);
  const full = [{ spot: null, first: 0, last: 5, state: 'confirmed', mine: false }];
  const none = built([container([period(DAY, rows, at(DAY, 14), { taken: full, starts: [] })])]);
  assert.deepEqual(dayList(none.dates[0])[0].lines, ['Nothing free']);
  const over = built([container([period(DAY, rows, at(DAY, 14), { starts: [] })])], at(DAY, 15));
  assert.deepEqual(dayList(over.dates[0])[0].lines, ['Passed']);
});

test('a fixed container reads per period, and only a period with a start is Available', () => {
  const day = (hour, end, extra) => period(DAY, [at(DAY, hour)], at(DAY, end), extra);
  const rows = (hour) => [at(DAY, hour)];
  const sauna = (second) => container(
    [day(17, 19, { starts: [start(rows(17), 0, at(DAY, 19))] }), day(19, 21, second)], { period_type: 'fixed' },
  );
  const free = built([sauna({ starts: [start(rows(19), 0, at(DAY, 21))] })]);
  assert.deepEqual(dayList(free.dates[0])[0].lines, ['17:00–19:00 · Available', '19:00–21:00 · Available']);
  const taken = [{ spot: null, first: 0, last: 0, state: 'confirmed', mine: false }];
  const part = built([sauna({ taken, starts: [] })]);
  const [item] = dayList(part.dates[0]);
  assert.deepEqual(item.lines, ['17:00–19:00 · Available']);
  assert.deepEqual(item.blocks.map((b) => [b.word, b.hours]), [['Booked', '19:00–21:00']]);
  const none = built([container([day(17, 19, { starts: [] })], { period_type: 'fixed' })]);
  assert.deepEqual(dayList(none.dates[0])[0].lines, ['Passed']);
});

test('a free night reads its hours; booked, it is a block', () => {
  const rows = [at(DAY, 17)];
  const night = (taken = [], starts = [start(rows, 0, at('2026-10-21', 8))]) =>
    container([period(DAY, rows, at('2026-10-21', 8), { taken, starts })], { period_type: 'fixed' });
  const free = built([night()]);
  const [item] = dayList(free.dates[0]);
  assert.deepEqual(item.lines, ['17:00–08:00 next day · Available']);
  assert.equal(item.night, true);
  const booked = built([night([{ spot: null, first: 0, last: 0, state: 'confirmed', mine: true }], [])]);
  const [taken] = dayList(booked.dates[0]);
  assert.deepEqual(taken.lines, ['Nothing free']);
  assert.deepEqual(taken.blocks.map((b) => [b.kind, b.word, b.hours]), [['yours', 'Your booking', '17:00–08:00 next day']]);
});

test('bookings are blocks: someone else, someone paying, yours; a shared time once', () => {
  const rows = hourly(DAY, 8, 16);
  const taken = [
    { spot: 1, first: 0, last: 1, state: 'confirmed', mine: false },
    { spot: 2, first: 0, last: 1, state: 'confirmed', mine: false },
    { spot: 1, first: 3, last: 3, state: 'held', mine: false },
    { spot: 2, first: 3, last: 3, state: 'confirmed', mine: false },
    { spot: 2, first: 4, last: 5, state: 'confirmed', mine: true },
    { spot: 1, first: 7, last: 7, state: 'block', mine: false },
    { spot: 2, first: 7, last: 7, state: 'confirmed', mine: false },
  ];
  const plan = { name: 'Hall', spot_word: 'table', groups: [], spots: [{ id: 1, number: 1, name: 'A', group: null }, { id: 2, number: 2, name: 'B', group: null }] };
  const result = built([container([period(DAY, rows, at(DAY, 16), { taken })], { floor_plan: plan })]);
  const blocks = bookingBlocks(result.dates[0].containers[0]);
  assert.deepEqual(blocks.map((b) => [b.word, b.hours]), [
    ['Booked', '08:00–10:00'], ['Booked', '11:00–12:00'], ['Your booking', '12:00–14:00'], ['Booked', '15:00–16:00'],
  ]);
  assert.deepEqual(blocks.map((b) => b.kind), ['other', 'held', 'yours', 'other']);
});

test('other members\' spots are a block only when no spot is left; yours always', () => {
  const rows = hourly(DAY, 8, 12);
  const others = (spot, first, last, state = 'confirmed') => ({ spot, first, last, state, mine: false });
  const taken = [
    others(1, 0, 1), others(2, 0, 0, 'held'),
    { spot: 1, first: 2, last: 2, state: 'confirmed', mine: true }, others(2, 2, 2), others(3, 2, 2),
    others(1, 3, 3), others(2, 3, 3), others(3, 3, 3),
  ];
  const spots = [1, 2, 3].map((number) => ({ id: number, number, name: '', group: null }));
  const plan = { name: 'Screen', spot_word: 'seat', groups: [], spots };
  const result = built([container([period(DAY, rows, at(DAY, 12), { taken })], { floor_plan: plan })]);
  const entry = result.dates[0].containers[0];
  assert.deepEqual(bookingBlocks(entry).map((b) => [b.kind, b.hours]), [['yours', '10:00–11:00'], ['other', '11:00–12:00']]);
  assert.deepEqual(dayList(result.dates[0])[0].blocks.map((b) => b.word), ['Your booking', 'Booked']);
});

test('the asset leads the name only when the day has several assets', () => {
  const one = built([container([period(DAY, hourly(DAY, 8, 10), at(DAY, 10))])]);
  assert.equal(dayList(one.dates[0])[0].name, 'Play');
  const two = built([
    container([period(DAY, hourly(DAY, 8, 10), at(DAY, 10))]),
    container([period(DAY, hourly(DAY, 8, 10), at(DAY, 10))], { id: 2, number: 2, asset: { id: 2, name: 'Pool' } }),
  ]);
  assert.deepEqual(dayList(two.dates[0]).map((item) => item.name), ['Court · Play', 'Pool · Play']);
});

test('a day with nothing to book is empty', () => {
  const none = built([]);
  assert.equal(isEmptyDay(none.dates[0]), true);
  const closed = built([container([period(DAY, hourly(DAY, 8, 10), at(DAY, 10), { closed_rows: [0, 1] })])]);
  assert.equal(isEmptyDay(closed.dates[0]), true);
  const open = built([container([period(DAY, hourly(DAY, 8, 10), at(DAY, 10))])]);
  assert.equal(isEmptyDay(open.dates[0]), false);
});

test('an empty day names its date and loosens only the choice that left it empty, as the demo\'s whyEmpty', () => {
  const two = [{ id: 1, name: 'work hire' }, { id: 2, name: 'private hire' }];
  const types = [{ id: 5, name: 'Party' }, { id: 6, name: 'Meeting' }];
  const filter = (extra = {}) => ({ categories: two, category: 1, types: [], type: null, ...extra });
  // The server names the member's other categories with time on a date.
  const answer = (others) => ({ worked_out_at: '2026-10-19T08:00:00+02:00', containers: [], other_categories: others });
  const day = buildCalendar(answer([{ day: DAY, categories: [2] }]), { start: DAY, days: 1, category: 1 }).dates[0];
  assert.equal(isEmptyDay(day), true);
  assert.deepEqual(day.others, [2]);
  assert.deepEqual(emptyDay(day, filter()), {
    line: 'No work hire on Tuesday 20 October.', button: { words: 'Show private hire', patch: { category: 2 } },
  });
  // Another category first, keeping the type; all types only when none has time.
  assert.deepEqual(emptyDay(day, filter({ types, type: 5 })), {
    line: 'No work hire · Party on Tuesday 20 October.', button: { words: 'Show private hire', patch: { category: 2 } },
  });
  const none = buildCalendar(answer([]), { start: DAY, days: 1, category: 1 }).dates[0];
  assert.deepEqual(none.others, []);
  assert.deepEqual(emptyDay(none, filter({ types, type: 5 })), {
    line: 'No Party on Tuesday 20 October.', button: { words: 'Show all', patch: { type: null } },
  });
  assert.deepEqual(emptyDay(none, filter()), { line: 'Nothing to book on Tuesday 20 October.', button: null });
  // An answer without the list (an older page's) offers nothing.
  assert.deepEqual(built([]).dates[0].others, []);
});

test('the cart\'s bookings show in their container on their date as "In your cart"', () => {
  const rows = hourly(DAY, 8, 14);
  const result = built([container([period(DAY, rows, at(DAY, 14))])]);
  const cart = { bookings: [
    { id: 'a', container: { id: 1 }, start: at(DAY, 10), end: at(DAY, 12) },
    { id: 'b', container: { id: 2 }, start: at(DAY, 10), end: at(DAY, 12) },
    { id: 'c', container: { id: 1 }, start: at('2026-10-21', 10), end: at('2026-10-21', 12) },
  ] };
  const [item] = dayList(result.dates[0], cart);
  assert.deepEqual(item.blocks.map((b) => [b.kind, b.word, b.hours]), [['cart', 'In your cart', '10:00–12:00']]);
  assert.deepEqual(dayList(result.dates[0])[0].blocks, []);
});

test('a cart time inside a booked stretch is listed once, as lost, the booked row cut around it', () => {
  const rows = hourly(DAY, 8, 14);
  const taken = [{ spot: null, first: 1, last: 4, state: 'confirmed', mine: false }];
  const result = built([container([period(DAY, rows, at(DAY, 14), { taken })])]);
  const cart = { bookings: [{ id: 'a', container: { id: 1 }, start: at(DAY, 10), end: at(DAY, 11) }] };
  const [item] = dayList(result.dates[0], cart);
  assert.deepEqual(item.blocks.map((b) => [b.kind, b.hours]), [['other', '09:00–10:00'], ['lost', '10:00–11:00'], ['other', '11:00–13:00']]);
});

import { dayName } from './words.js';

test('a column and a chip say Today on today, the weekday otherwise', () => {
  assert.equal(dayName('2026-10-25', '2026-10-25'), 'Today');
  assert.equal(dayName('2026-10-26', '2026-10-25'), 'Mon');
});

test('another member\'s booking is named; your own, a block and an erased member\'s are not', () => {
  const rows = hourly(DAY, 8, 14);
  const taken = [
    { spot: null, first: 0, last: 1, state: 'confirmed', mine: false, name: 'Ann Berg' },
    { spot: null, first: 2, last: 2, state: 'held', mine: false, name: 'Tor Dahl' },
    { spot: null, first: 3, last: 3, state: 'confirmed', mine: true },
    { spot: null, first: 4, last: 4, state: 'block', mine: false },
    { spot: null, first: 5, last: 5, state: 'confirmed', mine: false },
  ];
  const entry = built([container([period(DAY, rows, at(DAY, 14), { taken })])]).dates[0].containers[0];
  const blocks = bookingBlocks(entry);
  assert.deepEqual(blocks.map((b) => [b.word, b.hours]), [
    ['Ann Berg', '08:00–10:00'], ['Tor Dahl', '10:00–11:00'], ['Your booking', '11:00–12:00'], ['Closed', '12:00–13:00'], ['Booked', '13:00–14:00'],
  ]);
  assert.deepEqual(blocks[0].booked, { name: 'Ann Berg', place: 'Court · Play', date: DAY, hours: '08:00–10:00' });
  assert.deepEqual(blocks.slice(2).map((b) => b.booked), [undefined, undefined, undefined]);
});

test('bookings one after the other stay two blocks, each with its name', () => {
  const rows = hourly(DAY, 8, 12);
  const taken = [
    { spot: null, first: 0, last: 0, state: 'confirmed', mine: false, name: 'Ann Berg' },
    { spot: null, first: 1, last: 1, state: 'confirmed', mine: false, name: 'Ann Berg' },
    { spot: null, first: 2, last: 3, state: 'confirmed', mine: false, name: 'Tor Dahl' },
  ];
  const entry = built([container([period(DAY, rows, at(DAY, 12), { taken })])]).dates[0].containers[0];
  assert.deepEqual(bookingBlocks(entry).map((b) => [b.word, b.hours]), [
    ['Ann Berg', '08:00–09:00'], ['Ann Berg', '09:00–10:00'], ['Tor Dahl', '10:00–12:00'],
  ]);
});

test('a shared container\'s full time is named only when one member holds every spot', () => {
  const rows = hourly(DAY, 8, 10);
  const spots = [1, 2].map((number) => ({ id: number, number, name: '', group: null }));
  const plan = { name: 'Screen', spot_word: 'seat', groups: [], spots };
  const taken = [
    { spot: 1, first: 0, last: 0, state: 'confirmed', mine: false, name: 'Ann Berg' },
    { spot: 2, first: 0, last: 0, state: 'confirmed', mine: false, name: 'Ann Berg' },
    { spot: 1, first: 1, last: 1, state: 'confirmed', mine: false, name: 'Ann Berg' },
    { spot: 2, first: 1, last: 1, state: 'confirmed', mine: false, name: 'Tor Dahl' },
  ];
  const entry = built([container([period(DAY, rows, at(DAY, 10), { taken })], { floor_plan: plan })]).dates[0].containers[0];
  assert.deepEqual(bookingBlocks(entry).map((b) => [b.word, b.booked?.name]), [['Ann Berg', 'Ann Berg'], ['Booked', undefined]]);
});

test('the sheet\'s mail link: the organization\'s address, a subject that names the booking', () => {
  const href = contactHref({ name: 'Ann Berg', place: 'Court · Play', date: DAY, hours: '08:00–10:00' }, 'board@example.org');
  assert.ok(href.startsWith('mailto:board@example.org?subject='));
  assert.equal(decodeURIComponent(href.split('subject=')[1]), 'Contact details for Ann Berg: Court · Play, Tuesday 20 October, 08:00–10:00');
});

// An answer with no container on the day, the assets asked for and the dates they are closed on.
const closedAnswer = (assets, closedDays) => buildCalendar(
  { worked_out_at: '2026-10-19T08:00:00+02:00', containers: [], assets, closed_days: closedDays },
  { start: DAY, days: 1 },
).dates[0];
const closedRow = (asset, reason, by = 'closure') => ({ asset, day: DAY, by, reason });
const plain = { categories: [], category: null, types: [], type: null };

test('a closed day says Closed, with its closure\'s reason, and never Nothing to book', () => {
  const holiday = closedAnswer([1], [closedRow(1, 'holiday')]);
  assert.deepEqual(holiday.summary.closed, { reason: 'holiday' });
  assert.deepEqual(emptyDay(holiday, plain), { line: 'Closed: Holiday', button: null });
  assert.equal(tileName(DAY, holiday.summary, '2026-10-19'), 'Tuesday 20 October, Closed: Holiday');
  assert.equal(closedWords({ reason: 'repair' }), 'Closed: Repair');
  // A closure with no reason, and a day the weekly availability does not open: Closed.
  for (const day of [closedAnswer([1], [closedRow(1, '')]), closedAnswer([1], [closedRow(1, '', 'weekly')])]) {
    assert.deepEqual(emptyDay(day, plain), { line: 'Closed', button: null });
    assert.equal(tileName(DAY, day.summary, '2026-10-19'), 'Tuesday 20 October, Closed');
  }
  // The Filter loosens nothing on a closed day.
  const filter = { categories: [{ id: 1, name: 'work hire' }, { id: 2, name: 'private hire' }], category: 1, types: [{ id: 5, name: 'Party' }], type: 5 };
  assert.deepEqual(emptyDay(holiday, filter), { line: 'Closed: Holiday', button: null });
  // The organization's words for a reason, and a reason it has no word for.
  setLabels({ 'closure.holiday': 'Public holiday' });
  assert.equal(emptyDay(holiday, plain).line, 'Closed: Public holiday');
  setLabels({});
  assert.equal(closedWords({ reason: 'unheard-of' }), 'Closed');
});

test('an empty day that is not closed still says Nothing to book', () => {
  const open = closedAnswer([1], []);
  assert.equal(open.summary.closed, undefined);
  assert.equal(isEmptyDay(open), true);
  assert.deepEqual(emptyDay(open, plain), { line: 'Nothing to book on Tuesday 20 October.', button: null });
  assert.equal(tileName(DAY, open.summary, '2026-10-19'), 'Tuesday 20 October, Nothing to book');
  // An older answer, without the lists, is never closed.
  assert.equal(built([]).dates[0].summary.closed, undefined);
});

test('a place of several assets is closed on a day only when every asset is', () => {
  assert.equal(closedAnswer([1, 2], [closedRow(1, 'holiday')]).summary.closed, undefined);
  assert.deepEqual(closedAnswer([1, 2], [closedRow(1, 'holiday'), closedRow(2, '', 'weekly')]).summary.closed, { reason: 'holiday' });
  assert.deepEqual(closedAnswer([1, 2], [closedRow(1, 'holiday'), closedRow(2, 'repair')]).summary.closed, { reason: '' });
  // Rows of another date say nothing about this one.
  assert.equal(closedAnswer([1], [{ asset: 1, day: '2026-10-21', by: 'closure', reason: 'holiday' }]).summary.closed, undefined);
});

test('the halves of a month answer join their closed days, and a closed day is a closed time in the legend', () => {
  const half = (day, reason) => ({
    worked_out_at: '2026-10-19T08:00:00+02:00', containers: [], assets: [1], closed_days: [{ asset: 1, day, by: 'closure', reason }],
  });
  const joined = joinAnswers(half('2026-10-20', 'holiday'), half('2026-10-28', ''));
  assert.deepEqual(joined.closed_days.map((row) => row.day), ['2026-10-20', '2026-10-28']);
  assert.deepEqual(joined.assets, [1]);
  assert.equal(kindsSeen(buildCalendar(joined, { start: DAY, days: 1 }).dates).has('block'), true);
  assert.equal(kindsSeen(closedAnswer([1], []) ? [closedAnswer([1], [])] : []).has('block'), false);
});

test('a closed day says Closed first even when the answer still carries a container for it', () => {
  const rows = hourly(DAY, 8, 10);
  const answer = {
    worked_out_at: '2026-10-19T08:00:00+02:00',
    containers: [container([period(DAY, rows, at(DAY, 10), { taken: [{ spot: null, first: 0, last: 1, state: 'confirmed', mine: true }] })])],
    assets: [1],
    closed_days: [{ asset: 1, day: DAY, by: 'closure', reason: 'holiday' }],
  };
  const day = buildCalendar(answer, { start: DAY, days: 1 }).dates[0];
  assert.equal(day.containers.length, 1);
  assert.equal(isEmptyDay(day), false);
  assert.deepEqual(emptyDay(day, plain), { line: 'Closed: Holiday', button: null });
  assert.equal(tileName(DAY, day.summary, '2026-10-19'), 'Tuesday 20 October, Closed: Holiday, yours');
});

test('the reason alone is the word where the Closed look is already shown', () => {
  assert.equal(closedReason({ reason: 'holiday' }), 'Holiday');
  assert.equal(closedReason({ reason: '' }), '');
  assert.equal(closedReason({ reason: 'unheard-of' }), '');
});
