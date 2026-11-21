// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { addDays, buildCalendar } from './model.js';
import {
  dayLayout, hourMarks, jumpTo, laneBox, laneNames, minutesIn, nowLine, proposalFor, proposalMinutes, slotName, weekHours, weekLanes, wordLines,
} from './wide.js';

const hourly = (day, from, to) =>
  Array.from({ length: to - from }, (_, i) => `${day}T${String(from + i).padStart(2, '0')}:00:00+02:00`);
const at = (day, hour) => `${day}T${String(hour).padStart(2, '0')}:00:00+02:00`;

function court(id, day, from, to, { taken = [], closed = [], name = 'Play', asset = 'Court' } = {}) {
  const rows = hourly(day, from, to);
  const starts = rows.map((_, row) => ({ row, end: at(day, from + row + 1), spots: [], free: [] }))
    .filter((start) => !taken.some((span) => span.first <= start.row && start.row <= span.last)
      && !closed.includes(start.row));
  return {
    id, number: id, asset: { id, name: asset }, type: { id, name }, categories: [1], period_type: 'flexible',
    floor_plan: null, interval: 60, minimum_length: 60, maximum_length: 180,
    periods: [{
      day, rows, end: at(day, to), row_ends: [...rows.slice(1), at(day, to)], closed_rows: closed, taken, starts, category_hours: [],
    }],
  };
}

function night(id, day, { taken = [] } = {}) {
  const next = addDays(day, 1);
  return {
    id, number: id, asset: { id, name: 'Cabin' }, type: { id, name: 'Stay' }, categories: [1], period_type: 'fixed',
    floor_plan: null, interval: 60, minimum_length: 60, maximum_length: 60,
    periods: [{
      day, rows: [at(day, 20)], end: `${next}T10:00:00+02:00`, row_ends: [`${next}T10:00:00+02:00`], closed_rows: [],
      taken, starts: taken.length ? [] : [{ row: 0, end: `${next}T10:00:00+02:00`, spots: [], free: [] }], category_hours: [],
    }],
  };
}

const NOW = '2026-10-20T11:30:00+02:00';
const entryOf = (containers, date = '2026-10-20') =>
  buildCalendar({ worked_out_at: NOW, containers }, { start: date, days: 1 }).dates[0];

test('minutes count from midnight of the column, over midnight too', () => {
  assert.equal(minutesIn('2026-10-20', '2026-10-20T17:30:00+02:00'), 17 * 60 + 30);
  assert.equal(minutesIn('2026-10-20', '2026-10-21T10:00:00+02:00'), 34 * 60);
  assert.equal(minutesIn('2026-10-31', '2026-11-01T00:00:00+01:00'), 1440);
});

test('the week shows from the earliest start to the latest end, nights left out', () => {
  const mon = entryOf([court(1, '2026-10-20', 9, 17), night(2, '2026-10-20')]);
  const tue = entryOf([court(1, '2026-10-21', 8, 12)], '2026-10-21');
  assert.deepEqual(weekHours([mon, tue]), { from: 480, to: 1020 });
  assert.equal(weekHours([entryOf([night(2, '2026-10-20')])]), null);
  assert.deepEqual(hourMarks({ from: 480, to: 600 }).map((mark) => mark.label), ['08:00', '09:00', '10:00']);
});

test('free time is one slot per hour, the server\'s starts only', () => {
  const entry = entryOf([court(1, '2026-10-20', 9, 13, { closed: [0], taken: [{ spot: null, first: 2, last: 2, state: 'confirmed', mine: false }] })]);
  const layout = dayLayout(entry);
  assert.deepEqual(layout.slots.map((slot) => [slot.start, slot.stop]), [[600, 660], [720, 780]]);
  assert.equal(layout.lanes, 1);
});

test('bookings are blocks with their kind and word; the cart is one too', () => {
  const taken = [
    { spot: null, first: 0, last: 1, state: 'confirmed', mine: true },
    { spot: null, first: 2, last: 2, state: 'held', mine: false },
    { spot: null, first: 3, last: 3, state: 'confirmed', mine: false },
  ];
  const cart = { bookings: [{ id: 7, container: { id: 1 }, start: at('2026-10-20', 14), end: at('2026-10-20', 15) }] };
  const layout = dayLayout(entryOf([court(1, '2026-10-20', 9, 15, { taken })]), cart);
  assert.deepEqual(layout.blocks.map((block) => [block.kind, block.word, block.start, block.stop]), [
    ['yours', 'Your booking', 540, 660], ['held', 'Booked', 660, 720], ['other', 'Booked', 720, 780],
    ['cart', 'In your cart', 840, 900],
  ]);
});

test('in a shared container a taken seat leaves the slot; your own seat over it lets the pointer through', () => {
  const day = '2026-10-20';
  const screen = court(1, day, 13, 16);
  screen.floor_plan = { name: 'Screen', spot_word: 'seat', groups: [], spots: [1, 2].map((number) => ({ id: number, number, name: '', group: null })) };
  screen.periods[0].taken = [
    { spot: 1, first: 0, last: 0, state: 'confirmed', mine: false },
    { spot: 2, first: 1, last: 1, state: 'confirmed', mine: true },
    { spot: 1, first: 2, last: 2, state: 'confirmed', mine: false },
    { spot: 2, first: 2, last: 2, state: 'held', mine: false },
  ];
  screen.periods[0].starts = screen.periods[0].starts.filter((start) => start.row < 2);
  const layout = dayLayout(entryOf([screen], day));
  assert.deepEqual(layout.slots.map((slot) => slot.start), [780, 840]);
  assert.deepEqual(layout.blocks.map((block) => [block.kind, block.start, block.stop, block.over]), [
    ['yours', 840, 900, true], ['held', 900, 960, false],
  ]);
});

test('an asset with a whole-room and a seats container: a sold seat leaves the hour free; full, it is booked', () => {
  const day = '2026-10-20';
  const hire = court(1, day, 17, 20, { name: 'Hire', asset: 'Hall' });
  const seats = court(2, day, 17, 20, { name: 'Seats', asset: 'Hall' });
  seats.asset = hire.asset;
  seats.floor_plan = { name: 'Hall', spot_word: 'seat', groups: [], spots: [1, 2].map((number) => ({ id: number, number, name: '', group: null })) };
  const span = (spot, row, mine = false) => ({ spot, first: row, last: row, state: 'confirmed', mine });
  // 17:00 the member's seat (the whole room then taken for the hire), 18:00
  // another member's seat, 19:00 both seats sold.
  hire.periods[0].taken = [span(null, 0, true), span(null, 1), span(null, 2)];
  hire.periods[0].starts = [];
  seats.periods[0].taken = [span(2, 0, true), span(1, 1), span(1, 2), span(2, 2)];
  seats.periods[0].starts = seats.periods[0].starts.filter((start) => start.row < 2);
  const layout = dayLayout(entryOf([hire, seats], day));
  assert.deepEqual(layout.slots.map((slot) => slot.start), [1020, 1080]);
  assert.deepEqual(layout.blocks.map((block) => [block.kind, block.start, block.stop, block.over, block.title]), [
    ['yours', 1020, 1080, true, 'Hall 17:00–18:00'], ['other', 1140, 1200, false, 'Hall 19:00–20:00'],
  ]);
});

test('a shared night with seats left but too few for the number asked reads Nothing free', () => {
  const day = '2026-10-20';
  const cabin = night(9, day, { taken: [{ spot: 1, first: 0, last: 0, state: 'confirmed', mine: false }] });
  cabin.floor_plan = { name: 'Cabin', spot_word: 'bed', groups: [], spots: [1, 2].map((number) => ({ id: number, number, name: '', group: null })) };
  const layout = dayLayout(entryOf([cabin], day));
  assert.deepEqual(layout.nights.map((one) => [one.state, one.word]), [['other', 'Nothing free']]);
});

test('several containers get a lane each, side by side', () => {
  const entry = entryOf([court(1, '2026-10-20', 9, 12), court(2, '2026-10-20', 10, 12, { name: 'Training', asset: 'Hall' })]);
  const layout = dayLayout(entry);
  assert.equal(layout.lanes, 2);
  assert.deepEqual([...new Set(layout.slots.map((slot) => slot.lane))], [0, 1]);
  assert.deepEqual(laneBox(1, 2), { left: '50%', width: '50%' });
  assert.deepEqual(laneBox(0, 1), { left: '0%', width: '100%' });
  assert.equal(slotName('2026-10-20', entry.containers[1], at('2026-10-20', 10), at('2026-10-20', 11), true),
    'Book Hall Training, Tuesday 20 October, 10:00–11:00');
  assert.equal(slotName('2026-10-20', entry.containers[0], at('2026-10-20', 9), at('2026-10-20', 10), false),
    'Book Tuesday 20 October, 09:00–10:00');
});

test('a night is one slot in the Night row, free, booked or in the cart', () => {
  const free = dayLayout(entryOf([court(1, '2026-10-20', 9, 12), night(2, '2026-10-20')]));
  assert.equal(free.nights.length, 1);
  assert.equal(free.nights[0].state, 'free');
  assert.equal(free.nights[0].at, at('2026-10-20', 20));
  assert.equal(free.lanes, 1);
  assert.equal(free.slots.every((slot) => slot.container.id === 1), true);
  const taken = [{ spot: null, first: 0, last: 0, state: 'confirmed', mine: false }];
  const booked = dayLayout(entryOf([night(2, '2026-10-20', { taken })]));
  assert.deepEqual(booked.nights.map((one) => one.state), ['other']);
  assert.equal(booked.nights[0].hours, '20:00–10:00 next day');
  assert.deepEqual(dayLayout(entryOf([court(1, '2026-10-20', 9, 12)])).nights, []);
});

test('the pointer\'s proposal is the add sheet\'s first choice for that start', () => {
  const entry = entryOf([court(1, '2026-10-20', 9, 15), night(2, '2026-10-20')]);
  const [day, stay] = entry.containers;
  assert.deepEqual(proposalFor(day, at('2026-10-20', 17 - 3)), {
    at: at('2026-10-20', 14), end: at('2026-10-20', 15), words: 'Book 14:00–15:00',
  });
  assert.equal(proposalFor(day, at('2026-10-20', 20)).at, at('2026-10-20', 9));
  assert.deepEqual(proposalFor(stay, at('2026-10-20', 20)), {
    at: at('2026-10-20', 20), end: '2026-10-21T10:00:00+02:00', words: 'Book the night',
  });
});

test('the red line is only on today, inside the shown hours', () => {
  const hours = { from: 480, to: 1020 };
  assert.equal(nowLine('2026-10-20', '2026-10-20', NOW, hours), 690);
  assert.equal(nowLine('2026-10-21', '2026-10-20', NOW, hours), null);
  assert.equal(nowLine('2026-10-20', '2026-10-20', '2026-10-20T07:00:00+02:00', hours), null);
});

test('the jump: the Monday-to-Sunday week of the date, the date chosen, nothing before today', () => {
  assert.deepEqual(jumpTo('2026-11-04', '2026-10-20'), { first: '2026-11-02', chosen: '2026-11-04' });
  assert.deepEqual(jumpTo('2026-11-02', '2026-10-20'), { first: '2026-11-02', chosen: '2026-11-02' });
  assert.deepEqual(jumpTo('2026-10-22', '2026-10-20'), { first: '2026-10-19', chosen: '2026-10-22' });
  assert.deepEqual(jumpTo('2026-10-01', '2026-10-20'), { first: '2026-10-19', chosen: '2026-10-20' });
});

test('lanes come from the whole week: a container missing one day keeps the others\' lanes', () => {
  const mon = entryOf([court(2, '2026-10-20', 10, 12, { name: 'Sauna', asset: 'Spa' })]);
  const tue = entryOf([court(1, '2026-10-21', 9, 12, { name: 'Hall' }), court(2, '2026-10-21', 10, 12, { name: 'Sauna', asset: 'Spa' }), night(3, '2026-10-21')], '2026-10-21');
  const lanes = weekLanes([mon, tue]);
  assert.deepEqual(lanes, [{ id: 1, name: 'Court' }, { id: 2, name: 'Spa' }]);
  const ids = lanes.map((lane) => lane.id);
  assert.deepEqual([...new Set(dayLayout(mon, null, ids).slots.map((slot) => slot.lane))], [1]);
  assert.equal(dayLayout(mon, null, ids).lanes, 2);
  assert.deepEqual([...new Set(dayLayout(tue, null, ids).slots.map((slot) => slot.lane))], [0, 1]);
});

test('lanes are named only when there are several', () => {
  assert.deepEqual(laneNames([{ id: 1, name: 'Hall' }, { id: 2, name: 'Sauna' }]), ['Hall', 'Sauna']);
  assert.deepEqual(laneNames([{ id: 1, name: 'Hall' }]), []);
  assert.deepEqual(laneNames([]), []);
});

test('words wrap by the slot\'s hours: one line for an hour, two for two', () => {
  assert.equal(wordLines(60), 1);
  assert.equal(wordLines(120), 2);
  assert.equal(wordLines(30), 1);
});

test('the proposal\'s span is wall-clock hours on the night the clock goes back', () => {
  const day = '2026-10-25';
  const rows = [`${day}T01:00:00+02:00`, `${day}T02:00:00+02:00`, `${day}T03:00:00+01:00`, `${day}T04:00:00+01:00`];
  const ends = [rows[1], rows[2], rows[3], `${day}T05:00:00+01:00`];
  const box = court(1, day, 1, 2);
  box.periods = [{
    day, rows, end: ends[3], row_ends: ends, closed_rows: [], taken: [], category_hours: [],
    starts: [{ row: 1, end: ends[1], spots: [], free: [] }],
  }];
  const entry = entryOf([box], day);
  const proposal = proposalFor(entry.containers[0], rows[1]);
  assert.equal(Date.parse(proposal.end) - Date.parse(proposal.at), 120 * 60000);
  assert.equal(proposalMinutes(day, proposal), 60);
});

test('a night container gives one Night slot per period, each with its own state', () => {
  const two = night(2, '2026-10-20');
  const [first] = two.periods;
  two.periods.push({ ...first, rows: [at('2026-10-20', 22)], taken: [{ spot: null, first: 0, last: 0, state: 'confirmed', mine: false }], starts: [] });
  const layout = dayLayout(entryOf([two]));
  assert.deepEqual(layout.nights.map((one) => one.state), ['free', 'other']);
  assert.equal(new Set(layout.nights.map((one) => one.key)).size, 2);
});

test('the red line moves with the time since the answer was read', () => {
  const hours = { from: 480, to: 1020 };
  assert.equal(nowLine('2026-10-20', '2026-10-20', NOW, hours, 5 * 60000 + 1000), 695);
  assert.equal(nowLine('2026-10-20', '2026-10-20', NOW, hours, 59999), 690);
});

test('a missing or non-finite elapsed leaves the red line where the answer put it', () => {
  const hours = { from: 480, to: 1020 };
  assert.equal(nowLine('2026-10-20', '2026-10-20', NOW, hours, NaN), 690);
  assert.equal(nowLine('2026-10-20', '2026-10-20', NOW, hours, undefined), 690);
});

test('two assets with one container each are one lane each, named by asset', () => {
  const entry = entryOf([court(1, '2026-10-20', 9, 12, { asset: 'Court' }), court(2, '2026-10-20', 9, 12, { name: 'Sauna', asset: 'Spa' })]);
  const lanes = weekLanes([entry]);
  assert.deepEqual(lanes, [{ id: 1, name: 'Court' }, { id: 2, name: 'Spa' }]);
  assert.deepEqual(laneNames(lanes), ['Court', 'Spa']);
  assert.equal(dayLayout(entry).lanes, 2);
});

test('a cart time inside a stretch someone else booked is one state: taken, "No longer available", the booked block cut around it', () => {
  const taken = [{ spot: null, first: 1, last: 5, state: 'confirmed', mine: false }];
  const cart = { bookings: [{ id: 7, container: { id: 1 }, start: at('2026-10-20', 11), end: at('2026-10-20', 12) }] };
  const layout = dayLayout(entryOf([court(1, '2026-10-20', 9, 15, { taken })]), cart);
  const sorted = [...layout.blocks].sort((a, b) => a.start - b.start);
  assert.deepEqual(sorted.map((block) => [block.kind, block.word, block.start, block.stop]), [
    ['other', 'Booked', 600, 660], ['lost', 'No longer available', 660, 720], ['other', 'Booked', 720, 900],
  ]);
  // Nothing is drawn over anything else.
  sorted.slice(1).forEach((block, index) => assert.ok(block.start >= sorted[index].stop));
  // A cart time outside every booked stretch stays the cart's.
  const apart = { bookings: [{ id: 8, container: { id: 1 }, start: at('2026-10-20', 14), end: at('2026-10-20', 15) }] };
  assert.equal(dayLayout(entryOf([court(1, '2026-10-20', 9, 15, { taken: [{ spot: null, first: 0, last: 1, state: 'held', mine: false }] })]), apart)
    .blocks.at(-1).kind, 'cart');
});

import { freeRuns, hourInRun } from './wide.js';

const slot = (container, lane, start, stop) => ({ key: `${container}|${start}`, container: { id: container }, lane, start, stop });

test('free stretch: touching free hours of one container are one run; a gap, another lane or container breaks it', () => {
  const runs = freeRuns([
    slot(1, 0, 600, 660), slot(1, 0, 660, 720), slot(1, 0, 720, 780), slot(1, 0, 840, 900),
    slot(2, 0, 600, 660), slot(1, 1, 660, 720),
  ]);
  assert.deepEqual(runs.map((run) => [run.owner, run.lane, run.start, run.stop, run.slots.length]), [
    ['c1', 0, 600, 780, 3], ['c1', 0, 840, 900, 1], ['c2', 0, 600, 660, 1], ['c1', 1, 660, 720, 1],
  ]);
});

test('free stretch: a click names the hour under the pointer, the keyboard the first', () => {
  const [run] = freeRuns([slot(1, 0, 600, 660), slot(1, 0, 660, 720), slot(1, 0, 720, 780)]);
  assert.equal(hourInRun(run, 605).start, 600);
  assert.equal(hourInRun(run, 660).start, 660);
  assert.equal(hourInRun(run, 779).start, 720);
  assert.equal(hourInRun(run, 900).start, 600);
  assert.equal(hourInRun(run, null).start, 600);
});

test('an event is a block of the Week with its title and its id', () => {
  const taken = [{ spot: null, first: 1, last: 3, state: 'event', mine: false, event: { id: 5, title: 'Board meeting' } }];
  const layout = dayLayout(entryOf([court(1, '2026-10-20', 9, 15, { taken })]));
  assert.deepEqual(layout.blocks.map((b) => [b.kind, b.word, b.event.id]), [['event', 'Board meeting', 5]]);
});
