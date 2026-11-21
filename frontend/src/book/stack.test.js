// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildCalendar } from './model.js';
import {
  assetBlocks, assetHours, bookable, dayCards, hourName, offersAt, stackingAssets,
} from './stack.js';
import { dayLayout, slotAction, slotProposal, slotWords, weekLanes } from './wide.js';

const DAY = '2026-11-12';
const at = (hour, day = DAY) => `${day}T${String(hour).padStart(2, '0')}:00:00+01:00`;
const NOW = `${DAY}T06:00:00+01:00`;

// A brewing room's offers on one asset: `taken` and `starts` are by row (hours from `from`).
function offer(id, name, { asset = 1, from = 10, to = 22, taken = [], starts, periodType = 'flexible', minimum = 60, maximum = 300 } = {}) {
  const rows = Array.from({ length: to - from }, (_, i) => at(from + i));
  const open = starts || rows.map((_, row) => row);
  const clean = open.filter((row) => !taken.some((span) => span.first <= row && row <= span.last));
  return {
    id, number: id, asset: { id: asset, name: asset === 1 ? 'Brewing room' : 'Cellar' }, type: { id, name },
    categories: [1], period_type: periodType, floor_plan: null, interval: 60, minimum_length: minimum, maximum_length: maximum,
    periods: [{
      day: DAY, rows, end: at(to), row_ends: [...rows.slice(1), at(to)], closed_rows: [], taken,
      starts: clean.map((row) => ({ row, end: at(from + row + (periodType === 'flexible' ? minimum / 60 : 1)), spots: [], free: [] })),
      category_hours: [],
    }],
  };
}
const entryOf = (containers) => buildCalendar({ worked_out_at: NOW, containers }, { start: DAY, days: 1 }).dates[0];
const booked = (first, last, extra = {}) => ({ spot: null, first, last, state: 'confirmed', mine: false, ...extra });

// The drawing's room: a 5 h brewing session and a 1 h tapping session, one asset, 10-22,
// someone's session 12-17 (rows 2-6) shown taken on both.
const brewTaken = [booked(2, 6)];
const brewing = (taken = brewTaken) => offer(1, 'Brewing session', {
  taken, periodType: 'fixed', starts: [0, 1, 2, 3, 4, 5, 6, 7], minimum: 300, maximum: 300,
});
const tapping = (taken = brewTaken) => offer(2, 'Tapping session', { taken, periodType: 'fixed', minimum: 60, maximum: 60 });

test('an asset stacks on a day when it has more than one day-time container', () => {
  const entry = entryOf([brewing(), tapping(), offer(3, 'Cellar tour', { asset: 2 })]);
  const stacks = stackingAssets(entry);
  assert.deepEqual(stacks.map((one) => [one.asset.id, one.containers.map((row) => row.id)]), [[1, [1, 2]]]);
  assert.deepEqual(stackingAssets(entryOf([brewing(), offer(3, 'Cellar tour', { asset: 2 })])), []);
});

test('a night container does not count towards a stack', () => {
  const nightly = offer(4, 'Night in the room', { from: 20, to: 21, periodType: 'fixed' });
  nightly.periods[0].end = '2026-11-13T10:00:00+01:00';
  nightly.periods[0].row_ends = ['2026-11-13T10:00:00+01:00'];
  assert.deepEqual(stackingAssets(entryOf([brewing(), nightly])), []);
});

test('the asset\'s hours: free, booked on any container, blank when nothing is open', () => {
  const entry = entryOf([brewing(), tapping()]);
  const [{ containers }] = stackingAssets(entry);
  const hours = assetHours(DAY, containers);
  assert.equal(hours.length, 12);
  assert.deepEqual(hours.slice(0, 3).map((hour) => [hour.label, hour.state, hour.word]),
    [['10:00', 'free', 'Available'], ['11:00', 'free', 'Available'], ['12:00', 'other', 'Booked']]);
  assert.deepEqual(hours.slice(2, 7).map((hour) => hour.state), ['other', 'other', 'other', 'other', 'other']);
  assert.equal(hours[7].state, 'free');
  // Closed hours on every container are blank, not free.
  const part = entryOf([offer(5, 'Tapping', { from: 12, to: 16 }), offer(6, 'Brewing', { from: 10, to: 12 })]);
  assert.equal(assetHours(DAY, part.containers).length, 6);
  const gap = offer(7, 'Tap', { from: 10, to: 12 });
  gap.periods[0].closed_rows = [0];
  const dark = offer(8, 'Brew', { from: 10, to: 12 });
  dark.periods[0].closed_rows = [0];
  for (const one of [gap, dark]) one.periods[0].starts = one.periods[0].starts.filter((start) => start.row !== 0);
  const built = entryOf([gap, dark]);
  const closedHours = assetHours(DAY, built.containers);
  assert.equal(closedHours[0].state, 'blank');
});

test('someone paying, yours and your cart show as their own kinds', () => {
  const taken = [booked(0, 0, { state: 'held' }), booked(1, 1, { mine: true }), booked(2, 2, { state: 'block' })];
  const entry = entryOf([brewing(taken), tapping(taken)]);
  const [{ containers }] = stackingAssets(entry);
  // The server offers no start in the hour the cart holds.
  for (const one of containers) one.periods[0].starts = one.periods[0].starts.filter((start) => start.row !== 5);
  const cart = { bookings: [{ id: 9, container: { id: 2 }, start: at(15), end: at(16) }] };
  const hours = assetHours(DAY, containers, cart);
  assert.deepEqual(hours.slice(0, 3).map((hour) => [hour.state, hour.word]),
    [['held', 'Booked'], ['yours', 'Your booking'], ['closed', 'Closed']]);
  assert.deepEqual(hours[5], { minute: 900, label: '15:00', end: '16:00', state: 'cart', word: 'In your cart', booking: 'cart|9' });
});

test('a booking is one block for the asset, not one per container', () => {
  const entry = entryOf([brewing(), tapping()]);
  const [{ containers }] = stackingAssets(entry);
  assert.deepEqual(assetBlocks(DAY, containers).map((block) => [block.kind, block.hours]), [['other', '12:00–17:00']]);
});

test('at 17:00 the tapping session starts and the brewing session starts too, with its first choice', () => {
  const entry = entryOf([brewing(), tapping()]);
  const [{ containers }] = stackingAssets(entry);
  const offers = offersAt(containers, DAY, 17 * 60);
  assert.deepEqual(bookable(offers).map((row) => row.container.id), [1, 2]);
  assert.equal(offers[0].choice.start.at, at(17));
  assert.equal(offers[1].choice.end, at(18));
});

test('why an offer cannot start: needs its minimum, next start, or none', () => {
  // Brewing flexible, minimum 5 h, free 17-22 only from the server's starts 17.
  const flexible = offer(1, 'Brewing session', { minimum: 300, taken: brewTaken, starts: [7] });
  const tap = tapping();
  const entry = entryOf([flexible, tap]);
  const [{ containers }] = stackingAssets(entry);
  const at19 = offersAt(containers, DAY, 19 * 60);
  assert.equal(at19[0].choice, null);
  assert.equal(at19[0].reason, 'Needs 5 h free from 19:00');
  assert.equal(at19[1].choice.start.at, at(19));
  // A fixed offer with a later start only: next start.
  const sessions = offer(3, 'Evening session', { periodType: 'fixed', starts: [9], minimum: 120, maximum: 120 });
  const set = entryOf([tapping([]), sessions]);
  const found = offersAt(stackingAssets(set)[0].containers, DAY, 12 * 60);
  assert.equal(found[1].reason, 'Next start 19:00');
  // Nothing after: no start.
  const late = offersAt(stackingAssets(set)[0].containers, DAY, 20 * 60);
  assert.equal(late[1].reason, 'No start from 20:00');
});

test('the day panel has one card for a stacking asset, other containers as they were', () => {
  const entry = entryOf([brewing(), offer(3, 'Cellar tour', { asset: 2 }), tapping()]);
  const cards = dayCards(entry);
  assert.deepEqual(cards.map((card) => [card.kind, card.key]), [['asset', 'asset-1'], ['container', 'container-3']]);
  assert.equal(cards[0].hours.length, 12);
  assert.equal(cards[1].item.name, 'Cellar · Cellar tour');
  assert.equal(dayCards(entryOf([brewing()]))[0].kind, 'container');
});

test('the hour\'s spoken name has the asset, the date and the hours', () => {
  const hour = { label: '17:00', end: '18:00' };
  assert.equal(hourName(DAY, { name: 'Brewing room' }, hour), 'Brewing room, Thursday 12 November, free 17:00–18:00');
});

test('wide lanes are one per asset, a stacking asset is one lane of free hours', () => {
  const entry = entryOf([brewing(), tapping(), offer(3, 'Cellar tour', { asset: 2 })]);
  const lanes = weekLanes([entry]);
  assert.deepEqual(lanes, [{ id: 1, name: 'Brewing room' }, { id: 2, name: 'Cellar' }]);
  const layout = dayLayout(entry, null, lanes.map((lane) => lane.id));
  assert.equal(layout.lanes, 2);
  const room = layout.slots.filter((slot) => slot.lane === 0);
  assert.deepEqual(room.map((slot) => slot.start), [600, 660, 1020, 1080, 1140, 1200, 1260]);
  assert.deepEqual(layout.blocks.filter((block) => block.lane === 0).map((block) => [block.kind, block.hours, block.title]),
    [['other', '12:00–17:00', 'Brewing room 12:00–17:00']]);
});

test('a click on an hour with one offer is the add sheet; with several it is the sheet of offers', () => {
  const entry = entryOf([brewing(), tapping()]);
  const layout = dayLayout(entry);
  const slotAt = (minute) => layout.slots.find((slot) => slot.start === minute);
  // 20:00: brewing needs 5 h and does not fit; only the tapping session starts.
  const one = slotAt(20 * 60);
  assert.equal(bookable(one.offers).length, 1);
  assert.deepEqual(slotProposal(DAY, one), { words: 'Book 20:00–21:00', minutes: 60 });
  assert.equal(slotAction(DAY, one).add.container.id, 2);
  assert.equal(slotAction(DAY, one).add.from, at(20));
  assert.equal(slotWords(DAY, one, true), 'Book Brewing room Tapping session, Thursday 12 November, 20:00–21:00');
  // 17:00: both start.
  const two = slotAt(17 * 60);
  assert.deepEqual(slotProposal(DAY, two), { words: 'From 17:00', minutes: 60 });
  const action = slotAction(DAY, two);
  assert.deepEqual([action.stack.minute, action.stack.containers.map((row) => row.id)], [1020, [1, 2]]);
  assert.equal(slotWords(DAY, two, true), 'From 17:00, Thursday 12 November, Brewing room, 17:00–18:00');
});

test('an untaken open hour with no start is blank; so is a passed hour', () => {
  // Hours 10-11 have no start (the server offers none: passed), 12 is booked, 13 has none (too short), 14+ start.
  const room = (id, name) => offer(id, name, { from: 10, to: 16, taken: [booked(2, 2)], starts: [3, 4, 5] });
  const entry = entryOf([room(1, 'Brewing'), room(2, 'Tapping')]);
  const hours = assetHours(DAY, entry.containers);
  assert.deepEqual(hours.map((hour) => hour.state), ['blank', 'blank', 'other', 'free', 'free', 'free']);
});

test('overlapping blocks of two containers are one block per stretch, firmest first', () => {
  const hall = offer(1, 'Hall', { from: 10, to: 22, taken: [booked(9, 10, { mine: true })] });
  const sauna = offer(2, 'Sauna', { from: 10, to: 22, taken: [booked(8, 11), booked(0, 0, { state: 'held' })] });
  const entry = entryOf([hall, sauna]);
  const blocks = assetBlocks(DAY, entry.containers);
  assert.deepEqual(blocks.map((block) => [block.kind, block.hours]), [
    ['held', '10:00–11:00'], ['other', '18:00–19:00'], ['yours', '19:00–21:00'], ['other', '21:00–22:00'],
  ]);
  const cart = { bookings: [{ id: 3, container: { id: 1 }, start: at(13), end: at(15) }] };
  const taken = entryOf([offer(1, 'Hall', { from: 10, to: 22 }), offer(2, 'Sauna', { from: 10, to: 22, taken: [booked(4, 4, { state: 'held' })] })]);
  assert.deepEqual(assetBlocks(DAY, taken.containers, cart).map((block) => [block.kind, block.hours]), [['lost', '13:00–15:00']]);
});

test('an hour with a start at :30 is free even when its first half is booked', () => {
  const half = (id) => {
    const rows = [at(10), `${DAY}T10:30:00+01:00`];
    return {
      ...offer(id, `Slot ${id}`, { from: 10, to: 11 }),
      periods: [{
        day: DAY, rows, end: at(11), row_ends: [rows[1], at(11)], closed_rows: [],
        taken: [booked(0, 0)], category_hours: [], starts: [{ row: 1, end: at(11), spots: [], free: [] }],
      }],
    };
  };
  const entry = entryOf([half(1), half(2)]);
  assert.deepEqual(assetHours(DAY, entry.containers).map((hour) => hour.state), ['free']);
  const none = entryOf([half(1), half(2)]);
  for (const one of none.containers) one.periods[0].starts = [];
  assert.deepEqual(assetHours(DAY, none.containers).map((hour) => hour.state), ['other']);
});

test('a flexible offer without a minimum length falls back to no start', () => {
  const flexible = offer(1, 'Open session', { starts: [7], minimum: 300 });
  const entry = entryOf([flexible, tapping()]);
  const [{ containers }] = stackingAssets(entry);
  containers[0].minimum = undefined;
  assert.equal(offersAt(containers, DAY, 19 * 60)[0].reason, 'No start from 19:00');
});

test('a free hour is named by the hours a click proposes', () => {
  const entry = entryOf([offer(3, 'Cellar tour', { asset: 2 })]);
  const slot = dayLayout(entry).slots.find((one) => one.start === 14 * 60);
  assert.equal(slotProposal(DAY, slot).words, 'Book 14:00–17:00');
  assert.equal(slotWords(DAY, slot, false), 'Book Thursday 12 November, 14:00–17:00');
});

test('a cart time inside another container\'s booking of the asset is taken for you, once', () => {
  const entry = entryOf([brewing(), tapping([])]);
  const [{ containers }] = stackingAssets(entry);
  const cart = { bookings: [{ id: 9, container: { id: 2 }, start: at(13), end: at(14) }] };
  assert.deepEqual(assetBlocks(DAY, containers, cart).map((block) => [block.kind, block.word, block.hours]),
    [['other', 'Booked', '12:00–13:00'], ['lost', 'No longer available', '13:00–14:00'], ['other', 'Booked', '14:00–17:00']]);
  // A cart time reaching past the booked stretch is lost whole.
  const over = { bookings: [{ id: 9, container: { id: 2 }, start: at(16), end: at(18) }] };
  assert.deepEqual(assetBlocks(DAY, containers, over).map((block) => [block.kind, block.hours]),
    [['other', '12:00–16:00'], ['lost', '16:00–18:00']]);
  // The server offers no start in the hour the cart holds.
  for (const one of containers) one.periods[0].starts = one.periods[0].starts.filter((start) => start.row !== 3);
  assert.equal(assetHours(DAY, containers, cart)[3].state, 'lost');
});

test('the asset\'s hours: another member\'s booking says its name; yours, a block and two bookings do not', () => {
  const named = (first, last, name) => booked(first, last, { name });
  const taken = [
    named(0, 1, 'Ann Berg'), booked(2, 2, { mine: true }), { spot: null, first: 3, last: 3, state: 'block', mine: false },
    named(4, 4, 'Tor Dahl'), named(5, 5, 'Eva Moe'),
  ];
  // Two bookings in one hour: the second starts half an hour into row 5.
  const entry = entryOf([offer(1, 'Brewing session', { taken, starts: [] }), offer(2, 'Tapping session', { taken, starts: [] })]);
  const [{ containers }] = stackingAssets(entry);
  const hours = assetHours(DAY, containers);
  assert.deepEqual(hours.slice(0, 6).map((hour) => [hour.state, hour.word]), [
    ['other', 'Ann Berg'], ['other', 'Ann Berg'], ['yours', 'Your booking'], ['closed', 'Closed'], ['other', 'Tor Dahl'], ['other', 'Eva Moe'],
  ]);
  assert.deepEqual(hours[0].booked, { name: 'Ann Berg', place: 'Brewing room', date: DAY, hours: '10:00–12:00' });
  assert.deepEqual(hours.slice(2, 4).map((hour) => hour.booked), [undefined, undefined]);
});
