// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { firstChoice } from './add.js';
import {
  bookingPriceWords, cartDays, dateMark, estimate, estimateLines, fromPrice, joinHours, nightBlock, offerLine, priceWords, sliderModel, slideTo, strip,
} from './day.js';
import { buildCalendar } from './model.js';
import { isoWeek, numberedWeeks } from './month.js';
import { dayCards } from './stack.js';
import { tileName } from './words.js';

const DAY = '2026-11-12';
const at = (hour, minute = 0, day = DAY) => `${day}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00+01:00`;
const NOW = `${DAY}T06:00:00+01:00`;

const tiers = (...rows) => rows.map(([up_to, paid_per, amount]) => ({ up_to, paid_per, amount }));
const prices = (...categories) => ({ currency: 'NOK', categories });
const byLength = (category, rows, extra = {}) => ({
  category, billing_basis: 'per_booking', tiers_go_by: 'length', tiers: tiers(...rows), groups: [], ...extra,
});

// A flexible hall 10:00-22:00, hourly rows. `taken` are row spans; a start is
// every free row whose whole minimum length is free (the server's own rule).
function hall({
  id = 1, name = 'Hall', from = 10, to = 22, taken = [], minimum = 60, maximum = 240, interval = 60, price = null, asset = 1, kinds = {},
} = {}) {
  const rows = Array.from({ length: to - from }, (_, i) => at(from + i));
  const free = (row) => !taken.some(([a, b]) => a <= row && row <= b);
  const starts = rows.map((_, row) => row)
    .filter((row) => Array.from({ length: minimum / 60 }, (_, k) => row + k).every((r) => r < rows.length && free(r)))
    .map((row) => ({ row, end: at(from + row + minimum / 60), spots: [], free: [] }));
  return {
    id, number: id, asset: { id: asset, name: 'Main hall' }, type: { id, name }, categories: [1], period_type: 'flexible',
    consumption_mode: 'exclusive', with_payment: Boolean(price), prices: price, floor_plan: null,
    interval, minimum_length: minimum, maximum_length: maximum,
    periods: [{
      day: DAY, rows, end: at(to), row_ends: [...rows.slice(1), at(to)], closed_rows: [],
      taken: taken.map(([first, last]) => ({ spot: null, first, last, state: kinds[first] || 'confirmed', mine: kinds[first] === 'yours' })),
      starts, category_hours: [],
    }],
  };
}

// A fixed night 22:00 to 10:00 next day.
function night({ id = 5, price = null, booked = false } = {}) {
  const next = '2026-11-13';
  return {
    id, number: id, asset: { id: 2, name: 'Cabin' }, type: { id, name: 'Night' }, categories: [1], period_type: 'fixed',
    consumption_mode: 'exclusive', with_payment: Boolean(price), prices: price, floor_plan: null,
    interval: 60, minimum_length: 720, maximum_length: 720,
    periods: [{
      day: DAY, rows: [at(22)], end: at(10, 0, next), row_ends: [at(10, 0, next)], closed_rows: [],
      taken: booked ? [{ spot: null, first: 0, last: 0, state: 'confirmed', mine: false }] : [],
      starts: booked ? [] : [{ row: 0, end: at(10, 0, next), spots: [], free: [] }], category_hours: [],
    }],
  };
}

const entryOf = (containers) => buildCalendar({ worked_out_at: NOW, containers }, { start: DAY, days: 1 }).dates[0];
const built = (container) => entryOf([container]).containers[0];

// --- The slider ---------------------------------------------------------------

// Free 10-16, someone's hour 16-17, free 17-22; a booking is 1 to 3 hours.
const split = () => built(hall({ taken: [[6, 6]], maximum: 180 }));
const minute = (hour) => hour * 60;

test('slider: the run that holds the start, its step, hour marks and labels every second hour', () => {
  const container = split();
  const model = sliderModel(container, DAY, firstChoice(container, at(11)));
  assert.deepEqual([model.from, model.to, model.step], [minute(10), minute(16), 60]);
  assert.deepEqual(model.value, [minute(11), minute(14)]);
  assert.equal(model.minutes, 180);
  assert.deepEqual(model.marks.map((mark) => mark.value), [600, 660, 720, 780, 840, 900, 960]);
  assert.deepEqual(model.marks.map((mark) => mark.label), ['10', '', '12', '', '14', '', '16']);
  const later = sliderModel(container, DAY, firstChoice(container, at(18)));
  assert.deepEqual([later.from, later.to], [minute(17), minute(22)]);
});

test('slider: none for a fixed container', () => {
  const container = built(night());
  assert.equal(sliderModel(container, DAY, firstChoice(container)), null);
});

test('slider: the end moves in steps, up to the container\'s maximum and no further', () => {
  const container = split();
  const choice = firstChoice(container, at(11));
  const one = slideTo(container, DAY, choice, [minute(11), minute(13)], 1);
  assert.equal(one.end, at(13));
  const far = slideTo(container, DAY, one, [minute(11), minute(15)], 1);
  // 3 h is the maximum: 14:00.
  assert.equal(far.end, at(14));
  assert.equal(far.start.at, at(11));
});

test('slider: the end never goes below the minimum or into the booking', () => {
  const container = split();
  const choice = slideTo(container, DAY, firstChoice(container, at(13)), [minute(13), minute(15)], 1);
  assert.equal(choice.end, at(15));
  const down = slideTo(container, DAY, choice, [minute(13), minute(12)], 1);
  assert.equal(down.end, at(14));
  const past = slideTo(container, DAY, firstChoice(container, at(14)), [minute(14), minute(17)], 1);
  // The free run ends at 16:00.
  assert.equal(past.end, at(16));
});

test('slider: the start moves only to starts the server offers that still reach the end', () => {
  const container = split();
  const choice = slideTo(container, DAY, firstChoice(container, at(11)), [minute(11), minute(13)], 1);
  assert.equal(choice.end, at(13));
  const left = slideTo(container, DAY, choice, [minute(10), minute(13)], 0);
  assert.equal(left.start.at, at(10));
  assert.equal(left.end, at(13));
  // Nothing starts before 10:00: the thumb stays on the first start.
  const edge = slideTo(container, DAY, left, [minute(9), minute(13)], 0);
  assert.equal(edge.start.at, at(10));
  // A start must leave the minimum length before the end kept: 12:00 is the last.
  const right = slideTo(container, DAY, left, [minute(14), minute(13)], 0);
  assert.equal(right.start.at, at(12));
  assert.equal(right.end, at(13));
  // 10:00 to 14:00 is 4 h, over the 3 h maximum: the start stops at 11:00.
  const long = slideTo(container, DAY, choice, [minute(10), minute(14)], 1);
  assert.equal(slideTo(container, DAY, long, [minute(10), minute(14)], 0).start.at, at(11));
});

test('slider: a time the server offers no start for cannot be chosen', () => {
  const container = built(hall({ taken: [[3, 3]], maximum: 180 }));
  // 13:00-14:00 is taken: no start at 13:00, so a start thumb moved to 13:00 goes on to 14:00.
  const choice = slideTo(container, DAY, firstChoice(container, at(10)), [minute(10), minute(11)], 1);
  assert.deepEqual(sliderModel(container, DAY, choice).value, [minute(10), minute(11)]);
  const model = sliderModel(container, DAY, firstChoice(container, at(14)));
  assert.equal(model.from, minute(14));
});

test('slider: the step is the grid every start and end lies on', () => {
  const container = built(hall({ minimum: 60, interval: 60, maximum: 180 }));
  assert.equal(sliderModel(container, DAY, firstChoice(container))?.step, 60);
  const wide = built(hall({ minimum: 120, interval: 60, maximum: 240 }));
  assert.equal(sliderModel(wide, DAY, firstChoice(wide))?.step, 60);
});

// --- The hour strip -----------------------------------------------------------

test('strip: one cell per hour of the offer\'s time, every second hour written', () => {
  const cells = strip(split(), DAY);
  assert.equal(cells.length, 12);
  assert.deepEqual(cells.map((cell) => cell.tick).slice(0, 4), [true, false, true, false]);
  assert.equal(cells[0].label, '10:00');
});

test('strip: free, booked, someone paying, yours and in the cart, each as its state', () => {
  const container = built(hall({ taken: [[1, 1], [2, 2], [3, 3]], kinds: { 1: 'confirmed', 2: 'held', 3: 'yours' } }));
  // The server offers no start in the hour the cart holds.
  container.periods[0].starts = container.periods[0].starts.filter((start) => start.row !== 5);
  const cart = { bookings: [{ id: 'a', container: { id: 1 }, start: at(15), end: at(16) }] };
  const cells = strip(container, DAY, cart);
  assert.deepEqual(cells.map((cell) => cell.state).slice(0, 7), ['free', 'other', 'held', 'yours', 'free', 'cart', 'free']);
  assert.equal(cells[1].name, 'Hall, Thursday 12 November, 11:00–12:00, Booked');
  assert.equal(cells[5].name, 'Hall, Thursday 12 November, 15:00–16:00, In your cart');
});

test('strip: another member\'s booking is named in its cells and carries its sheet', () => {
  const raw = hall({ taken: [[1, 2], [3, 3]], kinds: { 1: 'confirmed', 3: 'yours' } });
  raw.periods[0].taken[0].name = 'Ann Berg';
  const cells = strip(built(raw), DAY);
  assert.equal(cells[1].name, 'Hall, Thursday 12 November, 11:00–12:00, Ann Berg');
  assert.deepEqual(cells[2].booked, { name: 'Ann Berg', place: 'Main hall · Hall', date: DAY, hours: '11:00–13:00' });
  assert.equal(cells[3].booked, undefined);
  assert.equal(cells[0].booked, undefined);
});

test('strip: a tap on a free cell opens the sheet at that hour\'s start; others open nothing', () => {
  const cells = strip(split(), DAY);
  assert.equal(cells[0].from, at(10));
  assert.equal(cells[6].state, 'other');
  assert.equal(cells[6].from, null);
});

test('strip: closed time is blank, with no name', () => {
  const container = hall({ taken: [] });
  container.periods[0].closed_rows = [0, 1];
  container.periods[0].starts = container.periods[0].starts.filter((start) => start.row > 1);
  const cells = strip(built(container), DAY);
  assert.deepEqual(cells.slice(0, 3).map((cell) => cell.state), ['blank', 'blank', 'free']);
  assert.equal(cells[0].name, null);
});

test('strip: an hour with no server start in it is not free, though no booking is on it', () => {
  // A 2 h minimum: the last hour of the day cannot start a booking.
  const cells = strip(built(hall({ minimum: 120, maximum: 240 })), DAY);
  assert.equal(cells[cells.length - 1].state, 'blank');
  assert.equal(cells[cells.length - 2].state, 'free');
});

test('strip: none for a fixed container', () => {
  assert.equal(strip(built(night()), DAY), null);
});

// --- The joined hours and the night's block (part 3.99898) ---------------------

const joined = (container, cart = null) => joinHours(strip(container, DAY, cart), 'Hall', DAY);

test('joinHours: the hours of one booking join into one block that says its name', () => {
  const raw = hall({ taken: [[1, 3]], kinds: { 1: 'confirmed' } });
  raw.periods[0].taken[0].name = 'Ann Berg';
  const blocks = joined(built(raw));
  assert.deepEqual(blocks.map((block) => [block.state, block.start, block.span]), [['free', 1, 1], ['other', 2, 3], ...blocks.slice(2).map((block) => [block.state, block.start, block.span])]);
  assert.equal(blocks[1].word, 'Ann Berg');
  assert.equal(blocks[1].name, 'Hall, Thursday 12 November, 11:00–14:00, Ann Berg');
  assert.equal(blocks[1].booked.hours, '11:00–14:00');
});

test('joinHours: two different bookings side by side stay two blocks', () => {
  const raw = hall({ taken: [[1, 2], [3, 4]], kinds: { 1: 'confirmed', 3: 'confirmed' } });
  raw.periods[0].taken[0].name = 'Ann Berg';
  raw.periods[0].taken[1].name = 'Bo Dahl';
  const blocks = joined(built(raw)).filter((block) => block.state === 'other');
  assert.deepEqual(blocks.map((block) => [block.word, block.start, block.span]), [['Ann Berg', 2, 2], ['Bo Dahl', 4, 2]]);
});

test('joinHours: free hours never join, each stays one button with its start', () => {
  const blocks = joined(built(hall({ taken: [[3, 3]] })));
  const free = blocks.filter((block) => block.state === 'free');
  assert.equal(free.length, 11);
  assert.ok(free.every((block) => block.span === 1 && block.word === null && block.pick));
  assert.equal(free[0].pick, at(10));
});

test('joinHours: the word only at two hours or more', () => {
  const one = joined(built(hall({ taken: [[2, 2]] }))).find((block) => block.state === 'other');
  assert.equal(one.span, 1);
  assert.equal(one.word, null);
  assert.equal(one.name, 'Hall, Thursday 12 November, 12:00–13:00, Booked');
  const two = joined(built(hall({ taken: [[2, 3]] }))).find((block) => block.state === 'other');
  assert.equal(two.span, 2);
  assert.equal(two.word, 'Booked');
});

test('joinHours: your booking and your cart say their words; closed and blank say nothing', () => {
  const container = hall({ taken: [[1, 2]], kinds: { 1: 'yours' } });
  container.periods[0].closed_rows = [8, 9];
  container.periods[0].starts = container.periods[0].starts.filter((start) => start.row < 8 || start.row > 9);
  const cart = { bookings: [{ id: 'a', container: { id: 1 }, start: at(15), end: at(17) }] };
  container.periods[0].starts = container.periods[0].starts.filter((start) => start.row !== 5 && start.row !== 6);
  const blocks = joined(built(container), cart);
  assert.equal(blocks.find((block) => block.state === 'yours').word, 'Your booking');
  assert.equal(blocks.find((block) => block.state === 'cart').word, 'In your cart');
  assert.ok(blocks.filter((block) => block.state === 'blank').every((block) => block.word === null && block.span === 1));
});

const withEvents = (...events) => {
  const raw = hall({ taken: events.map(([a, b]) => [a, b]) });
  events.forEach(([, , event], index) => { Object.assign(raw.periods[0].taken[index], { state: 'event', mine: false, ...(event && { event }) }); });
  return raw;
};

test('joinHours: an event\'s hours say its title', () => {
  const blocks = joined(built(withEvents([0, 3, { id: 7, title: 'Open house' }])));
  const event = blocks.find((block) => block.state === 'event');
  assert.deepEqual([event.span, event.word], [4, 'Open house']);
  assert.equal(event.name, 'Hall, Thursday 12 November, 10:00–14:00, Open house');
});

test('joinHours: two events side by side stay two blocks', () => {
  const blocks = joined(built(withEvents([0, 1, { id: 7, title: 'Open house' }], [2, 3, { id: 8, title: 'Quiz night' }])))
    .filter((block) => block.state === 'event');
  assert.deepEqual(blocks.map((block) => [block.word, block.start, block.span]), [['Open house', 1, 2], ['Quiz night', 3, 2]]);
});

test('joinHours: an event with no title says Event', () => {
  const event = joined(built(withEvents([0, 1]))).find((block) => block.state === 'event');
  assert.deepEqual([event.span, event.word], [2, 'Event']);
  const blank = joined(built(withEvents([0, 1, { id: 7, title: '' }]))).find((block) => block.state === 'event');
  assert.equal(blank.word, 'Event');
});

test('nightBlock: another member\'s booking says its name and opens its sheet', () => {
  const raw = night({ booked: true });
  raw.periods[0].taken[0].name = 'Ann Berg';
  const block = nightBlock(built(raw), DAY, null, false, 'Night');
  assert.equal(block.state, 'other');
  assert.equal(block.word, 'Ann Berg');
  assert.equal(block.booked.name, 'Ann Berg');
  assert.equal(block.name, 'Night, Thursday 12 November, 22:00–10:00, Ann Berg');
});

test('nightBlock: an unnamed booking says Booked and opens nothing', () => {
  const block = nightBlock(built(night({ booked: true })), DAY, null, false);
  assert.equal(block.word, 'Booked');
  assert.equal(block.booked, null);
});

test('nightBlock: your own booking says Your booking', () => {
  const raw = night({ booked: true });
  raw.periods[0].taken[0] = { ...raw.periods[0].taken[0], state: 'yours', mine: true };
  const block = nightBlock(built(raw), DAY, null, false);
  assert.equal(block.state, 'yours');
  assert.equal(block.word, 'Your booking');
});

test('nightBlock: a free night is the free block with no word, spoken as Available', () => {
  const block = nightBlock(built(night()), DAY, null, true);
  assert.deepEqual([block.state, block.word, block.free], ['free', null, true]);
  assert.equal(block.name, 'Night, Thursday 12 November, 22:00–10:00, Available');
});

test('nightBlock: nothing when nothing is booked and nothing can be', () => {
  assert.equal(nightBlock(built(night()), DAY, null, false), null);
});

const NEXT = '2026-11-13';
const nightCart = { bookings: [{ id: 'c', container: { id: 5 }, start: at(22), end: at(10, 0, NEXT) }] };
const taken = (state, extra = {}) => ({ spot: null, first: 0, last: 0, state, mine: false, ...extra });
const nightWith = (patch) => { const raw = night({ booked: true }); Object.assign(raw.periods[0], patch); return raw; };

test('nightBlock: someone paying says Booked', () => {
  const block = nightBlock(built(nightWith({ taken: [taken('held')] })), DAY, null, false);
  assert.deepEqual([block.state, block.word, block.booked], ['held', 'Booked', null]);
});

test('nightBlock: an event says its title', () => {
  const block = nightBlock(built(nightWith({ taken: [taken('event', { event: { id: 7, title: 'Quiz night' } })] })), DAY, null, false);
  assert.deepEqual([block.state, block.word], ['event', 'Quiz night']);
});

test('nightBlock: an event with no title says Event, in the word and the name', () => {
  const block = nightBlock(built(nightWith({ taken: [taken('event', { event: { id: 7, title: '' } })] })), DAY, null, false);
  assert.equal(block.word, 'Event');
  assert.ok(block.name.endsWith(', Event'));
});

test('nightBlock: your cart says In your cart; taken first, it is No longer available', () => {
  const free = nightWith({ taken: [], starts: [] });
  assert.deepEqual([nightBlock(built(free), DAY, nightCart, false).state, nightBlock(built(free), DAY, nightCart, false).word], ['cart', 'In your cart']);
  const lost = nightBlock(built(nightWith({ taken: [taken('confirmed')] })), DAY, nightCart, false);
  assert.deepEqual([lost.state, lost.word], ['lost', 'No longer available']);
});

test('nightBlock: a passed night with a booking still shows the booking', () => {
  const block = nightBlock(built(nightWith({ taken: [taken('confirmed')], starts: [] })), DAY, null, false);
  assert.equal(block.word, 'Booked');
});

test('nightBlock: nothing for a night with no booking, no start and no period', () => {
  assert.equal(nightBlock(built(nightWith({ taken: [], starts: [] })), DAY, null, false), null);
  const bare = built(night());
  bare.periods = [];
  assert.equal(nightBlock(bare, DAY, null, true), null);
});

test('nightBlock: starts still offered, a night in your cart or yours is not drawn free', () => {
  const cart = nightBlock(built(night()), DAY, nightCart, true);
  assert.deepEqual([cart.state, cart.word, cart.free], ['cart', 'In your cart', false]);
  const raw = night();
  raw.periods[0].taken = [taken('confirmed', { mine: true })];
  const yours = nightBlock(built(raw), DAY, null, true);
  assert.deepEqual([yours.state, yours.word, yours.free], ['yours', 'Your booking', false]);
});

// Two periods: 14:00-18:00 (first) and 22:00-10:00.
const twoPeriods = (first, second) => {
  const raw = night({ booked: true });
  const one = {
    day: DAY, rows: [at(14)], end: at(18), row_ends: [at(18)], closed_rows: [], category_hours: [],
    taken: first.taken, starts: first.starts,
  };
  Object.assign(raw.periods[0], second);
  raw.periods.unshift(one);
  return raw;
};
const open = (hour, end, day = DAY) => [{ row: 0, end: at(end, 0, day), spots: [], free: [] }];

test('nightBlock: with the first period taken and a later one open, it is free for the open period', () => {
  const raw = twoPeriods({ taken: [taken('confirmed')], starts: [] }, { taken: [], starts: open(22, 10, NEXT) });
  const block = nightBlock(built(raw), DAY, null, true);
  assert.deepEqual([block.state, block.word], ['free', null]);
  assert.equal(block.name, 'Night, Thursday 12 November, 22:00–10:00, Available');
});

test('nightBlock: with no period open, it describes the first period\'s booking', () => {
  const raw = twoPeriods({ taken: [taken('confirmed', { name: 'Ann Berg' })], starts: [] }, { taken: [taken('held')], starts: [] });
  const block = nightBlock(built(raw), DAY, null, false);
  assert.deepEqual([block.state, block.word], ['other', 'Ann Berg']);
  assert.equal(block.name, 'Night, Thursday 12 November, 14:00–18:00, Ann Berg');
});

test('joinHours: two unnamed bookings that touch stay two blocks, one booking over hours joins', () => {
  const blocks = joined(built(hall({ taken: [[1, 2], [3, 4], [6, 8]] }))).filter((block) => block.state === 'other');
  assert.deepEqual(blocks.map((block) => [block.start, block.span, block.word]), [[2, 2, 'Booked'], [4, 2, 'Booked'], [7, 3, 'Booked']]);
});

test('joinHours: two events with one title that touch stay two blocks', () => {
  const raw = hall({ taken: [[1, 2], [3, 4]], kinds: { 1: 'event', 3: 'event' } });
  raw.periods[0].taken[0].event = { id: 1, title: 'Quiz' };
  raw.periods[0].taken[1].event = { id: 2, title: 'Quiz' };
  const blocks = joined(built(raw)).filter((block) => block.state === 'event');
  assert.deepEqual(blocks.map((block) => [block.start, block.span, block.word]), [[2, 2, 'Quiz'], [4, 2, 'Quiz']]);
});

// --- The line -----------------------------------------------------------------

test('line: a flexible offer says its hours and how long it is free', () => {
  assert.equal(offerLine(built(hall())), '10:00–22:00 · 12 h free');
  assert.equal(offerLine(split()), '10:00–22:00 · 11 h free');
});

test('line: a fixed offer says its hours and Available; booked, the word of the booking', () => {
  assert.equal(offerLine(built(night())), '22:00–10:00 next day · Available');
  const taken = built(night({ booked: true }));
  assert.equal(offerLine(taken, [{ kind: 'other' }]), '22:00–10:00 next day · Booked');
  assert.equal(offerLine(taken, [{ kind: 'other' }, { kind: 'yours' }]), '22:00–10:00 next day · Your booking');
  const passed = night();
  passed.periods[0].starts = [];
  assert.equal(offerLine(built(passed), []), '22:00–10:00 next day · Passed');
});

test('line: a shared session with seats left but too few for the asked number says Nothing free', () => {
  const session = night();
  session.floor_plan = { name: 'Screen', spot_word: 'seat', groups: [], spots: [1, 2].map((number) => ({ id: number, number, name: '', group: null })) };
  session.periods[0].taken = [{ spot: 1, first: 0, last: 0, state: 'confirmed', mine: false }];
  session.periods[0].starts = [];
  assert.equal(offerLine(built(session), []), '22:00–10:00 next day · Nothing free');
});

test('line: a flexible offer with nothing free says so', () => {
  const full = built(hall({ taken: [[0, 11]] }));
  assert.equal(offerLine(full, [{ kind: 'other' }]), '10:00–22:00 · Nothing free');
});

// --- The price ----------------------------------------------------------------

test('price: a flexible offer is "from" its cheapest shortest booking', () => {
  // 1 h costs 150 kr (15000), each next started hour 100 kr.
  const container = built(hall({ price: prices(byLength(1, [[60, 60, 15000], [240, 60, 10000]])) }));
  assert.deepEqual(fromPrice(container, 1), { amount: 15000, from: true });
  assert.equal(priceWords(container, 1), 'from 150 kr');
});

test('price: a fixed offer is its exact price', () => {
  const container = built(night({ price: prices(byLength(1, [[720, 720, 50000]])) }));
  assert.deepEqual(fromPrice(container, 1), { amount: 50000, from: false });
  assert.equal(priceWords(container, 1), '500 kr');
});

test('price: the chosen category\'s own price; a category without one has none', () => {
  const container = built(night({ price: prices(byLength(1, [[720, 720, 50000]]), byLength(2, [[720, 720, 30000]])) }));
  assert.equal(priceWords(container, 2), '300 kr');
  assert.equal(priceWords(container, '2'), '300 kr');
  assert.equal(priceWords(container, 3), null);
});

test('price: free of charge when the price is nothing', () => {
  const container = built(night({ price: prices(byLength(1, [[720, 720, 0]])) }));
  assert.equal(priceWords(container, 1), 'Free of charge');
});

test('price: nothing for a container without payment', () => {
  assert.equal(priceWords(built(hall()), 1), null);
  assert.equal(priceWords(built(night()), 1), null);
});

test('price: by time of day the cheapest start counts', () => {
  // 10:00-16:00 costs 100 kr an hour, 16:00-24:00 200 kr.
  const container = built(hall({ price: prices({
    category: 1, billing_basis: 'per_booking', tiers_go_by: 'time of day', groups: [], tiers: tiers([960, 60, 10000], [1440, 60, 20000]),
  }) }));
  assert.deepEqual(fromPrice(container, 1), { amount: 10000, from: true });
});

test('price: only the starts the server offers are priced', () => {
  const container = built(hall({
    taken: [[0, 5]],
    price: prices({
      category: 1, billing_basis: 'per_booking', tiers_go_by: 'time of day', groups: [], tiers: tiers([960, 60, 10000], [1440, 60, 20000]),
    }),
  }));
  // 10:00-16:00 is taken: the cheapest start left is at 16:00.
  assert.deepEqual(fromPrice(container, 1), { amount: 20000, from: true });
});

test('price: a price per place is paid for each place asked', () => {
  const container = built(night({ price: prices(byLength(1, [[720, 720, 20000]], { billing_basis: 'per_capacity_unit' })) }));
  assert.deepEqual(fromPrice(container, 1, 3), { amount: 60000, from: false });
});

test('price: the cheapest of a spot group\'s price and the normal one, "from"', () => {
  const container = built(night({ price: prices(byLength(1, [[720, 720, 20000]], {
    billing_basis: 'per_capacity_unit', groups: [{ group: 7, tiers_go_by: 'length', tiers: tiers([720, 720, 15000]) }],
  })) }));
  assert.deepEqual(fromPrice(container, 1), { amount: 15000, from: true });
});

test('price: tiers that do not reach the booking give no price', () => {
  const container = built(night({ price: prices(byLength(1, [[360, 360, 10000]])) }));
  assert.equal(fromPrice(container, 1), null);
});

// --- The day in time order ----------------------------------------------------

test('order: by when the time starts that day, then the organization\'s order of types', () => {
  const evening = hall({ id: 1, name: 'Hire', from: 18, to: 22, asset: 3 });
  const morning = hall({ id: 2, name: 'Lunch', from: 8, to: 12, asset: 4 });
  const same = hall({ id: 3, name: 'Sauna', from: 8, to: 12, asset: 5 });
  const entry = entryOf([evening, morning, same]);
  assert.deepEqual(dayCards(entry).map((card) => card.container.id), [2, 3, 1]);
  assert.deepEqual(dayCards(entry, null, ['Sauna', 'Lunch', 'Hire']).map((card) => card.container.id), [3, 2, 1]);
  // A type outside the order goes after those in it.
  assert.deepEqual(dayCards(entry, null, ['Sauna']).map((card) => card.container.id), [3, 2, 1]);
});

test('order: the night after the day\'s hours when it starts later, the stacked asset by its first hour', () => {
  const entry = entryOf([night({ id: 9 }), hall({ id: 1, name: 'Hire' }), hall({ id: 2, name: 'Tap', from: 12, to: 20 })]);
  const cards = dayCards(entry);
  assert.deepEqual(cards.map((card) => card.key), ['asset-1', 'container-9']);
  assert.deepEqual(cards[0].containers.map((row) => row.id), [1, 2]);
});

// --- The cart on the Month ----------------------------------------------------

test('cart mark: the days with something in the cart', () => {
  const cart = { bookings: [{ start: at(15) }, { start: at(10, 0, '2026-11-14') }, { start: at(18) }] };
  assert.deepEqual([...cartDays(cart)].sort(), ['2026-11-12', '2026-11-14']);
  assert.deepEqual([...cartDays(null)], []);
});

test('cart mark: a ring for the cart, the pill for yours, the pill when both', () => {
  const days = new Set(['2026-11-12']);
  assert.equal(dateMark({ mine: false }, '2026-11-12', days), 'cart');
  assert.equal(dateMark({ mine: false }, '2026-11-13', days), null);
  // An order waiting for payment marks its date before the cart answer comes.
  assert.equal(dateMark({ mine: false, cart: true }, '2026-11-13', days), 'cart');
  assert.equal(dateMark({ mine: true }, '2026-11-13', days), 'mine');
  assert.equal(dateMark({ mine: true }, '2026-11-12', days), 'mine');
  assert.match(tileName('2026-11-12', { state: 'open', share: 0.5, mine: false }, '2026-11-01', { inCart: true }), /in your cart$/);
});

// --- Week numbers -------------------------------------------------------------

test('week numbers: ISO weeks, across the year\'s ends', () => {
  assert.equal(isoWeek('2026-10-19'), 43);
  assert.equal(isoWeek('2026-10-25'), 43);
  assert.equal(isoWeek('2026-01-01'), 1);
  assert.equal(isoWeek('2021-01-03'), 53);
  assert.equal(isoWeek('2024-12-30'), 1);
  assert.equal(isoWeek('2026-12-31'), 53);
});

test('week numbers: a number for each row of the month', () => {
  const weeks = numberedWeeks('2026-10');
  assert.deepEqual(weeks.map((row) => row.week), [40, 41, 42, 43, 44]);
  assert.equal(weeks[0].dates[3], '2026-10-01');
  assert.deepEqual(numberedWeeks('2027-01').map((row) => row.week), [53, 1, 2, 3, 4]);
});

// --- The total shown while the server's price is asked ------------------------

test('estimate: the tiers price the chosen hours at once', () => {
  // The first 4 h cost 250 kr, each next started hour 50 kr.
  const container = built(hall({ price: prices(byLength(1, [[240, 240, 25000], [720, 60, 5000]])) }));
  assert.equal(estimate(container, 1, 1, at(14), at(17)), 25000);
  assert.equal(estimate(container, 1, 1, at(12), at(18)), 35000);
});

test('estimate: none when only the server can tell', () => {
  const plain = built(hall({ price: prices(byLength(1, [[720, 60, 5000]])) }));
  assert.equal(estimate(plain, 2, 1, at(14), at(15)), null);
  assert.equal(estimate(built(hall()), 1, 1, at(14), at(15)), null);
  const grouped = built(hall({ price: prices(byLength(1, [[720, 60, 5000]], {
    groups: [{ group: 7, tiers_go_by: 'length', tiers: tiers([720, 60, 8000]) }],
  })) }));
  assert.equal(estimate(grouped, 1, 1, at(14), at(15)), null);
  assert.equal(estimate(grouped, 1, 1, at(14), at(15), 7), 8000);
  assert.equal(estimate(grouped, 1, 1, at(14), at(15), 7, [3]), null);
});

test('estimate: a fixed night is priced on the clock, also when the clock goes back', () => {
  // 22:00 to 10:00 is 12 h on the clock and 13 h of time on 25 October.
  const container = built(night({ price: prices(byLength(1, [[720, 60, 5000]])) }));
  assert.equal(estimate(container, 1, 1, '2026-10-24T22:00:00+02:00', '2026-10-25T10:00:00+01:00'), 60000);
  assert.equal(estimate(container, 1, 1, at(22), at(10, 0, '2026-11-13')), 60000);
});

test('booking price words: the booking\'s own price; "from" where only the server can tell; none without payment', () => {
  const plain = built(night({ price: prices(byLength(1, [[720, 720, 50000]])) }));
  assert.equal(bookingPriceWords(plain, 1, 1, at(22), at(10, 0, '2026-11-13')), '500 kr');
  const free = built(night({ price: prices(byLength(1, [[720, 720, 0]])) }));
  assert.equal(bookingPriceWords(free, 1, 1, at(22), at(10, 0, '2026-11-13')), 'No charge');
  const grouped = built(hall({ price: prices(byLength(1, [[720, 60, 5000]], {
    groups: [{ group: 7, tiers_go_by: 'length', tiers: tiers([720, 60, 8000]) }],
  })) }));
  assert.equal(bookingPriceWords(grouped, 1, 1, at(14), at(15)), 'from 50 kr');
  assert.equal(bookingPriceWords(grouped, 1, 1, at(14), at(15), 7), '80 kr');
  assert.equal(bookingPriceWords(built(hall()), 1, 1, at(14), at(15)), null);
});

test('estimate: a price per place is paid for each place', () => {
  const container = built(hall({ price: prices(byLength(1, [[720, 60, 5000]], { billing_basis: 'per_capacity_unit' })) }));
  assert.equal(estimate(container, 1, 3, at(14), at(16)), 30000);
});

test('estimateLines: the server\'s lines from the calendar\'s tiers, and their sum is the estimate', () => {
  const container = built(hall({ price: prices(byLength(1, [[240, 240, 25000], [720, 60, 5000]])) }));
  const found = estimateLines(container, 1, 1, at(12), at(18));
  assert.deepEqual(found.lines, [
    { up_to: 240, paid_per: 240, times_paid: 1, amount: 25000, total: 25000, group: null },
    { up_to: 720, paid_per: 60, times_paid: 2, amount: 5000, total: 10000, group: null },
  ]);
  assert.equal(found.total, estimate(container, 1, 1, at(12), at(18)));
  assert.equal(found.total, 35000);
  assert.equal(found.tiers_go_by, 'length');
  assert.equal(found.currency, container.prices.currency);
  // Inside the first tier only the first line.
  assert.equal(estimateLines(container, 1, 1, at(14), at(17)).lines.length, 1);
});

test('estimateLines: null where estimate is null, and for a price per place of several places', () => {
  assert.equal(estimateLines(built(hall()), 1, 1, at(14), at(15)), null);
  const plain = built(hall({ price: prices(byLength(1, [[720, 60, 5000]])) }));
  assert.equal(estimateLines(plain, 2, 1, at(14), at(15)), null);
  const perPlace = built(hall({ price: prices(byLength(1, [[720, 60, 5000]], { billing_basis: 'per_capacity_unit' })) }));
  assert.equal(estimateLines(perPlace, 1, 3, at(14), at(16)), null);
  assert.equal(estimateLines(perPlace, 1, 1, at(14), at(16)).total, 10000);
});

test('estimateLines: a part hour is paid as a started hour, as pricing.py _parts and _lines do', () => {
  // 90 minutes at 60 per started hour: 2 times; the first 4 h at one price, the rest by started hour.
  const container = built(hall({ price: prices(byLength(1, [[240, 240, 25000], [720, 60, 5000]])) }));
  const found = estimateLines(container, 1, 1, at(12), at(18, 30));
  assert.deepEqual(found.lines.map((line) => [line.times_paid, line.total]), [[1, 25000], [3, 15000]]);
  const part = estimateLines(container, 1, 1, at(10), at(11, 30));
  assert.deepEqual(part.lines.map((line) => [line.times_paid, line.total]), [[1, 25000]]);
  const short = built(hall({ price: prices(byLength(1, [[720, 60, 5000]])) }));
  const ninety = estimateLines(short, 1, 1, at(10), at(11, 30));
  assert.deepEqual(ninety.lines.map((line) => [line.times_paid, line.total]), [[2, 10000]]);
  assert.equal(ninety.total, estimate(short, 1, 1, at(10), at(11, 30)));
});
