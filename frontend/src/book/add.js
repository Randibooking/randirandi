// SPDX-License-Identifier: AGPL-3.0-only
// What the add sheet offers and sends (part 3.6), from the day's container as
// `buildCalendar` made it (model.js). Pure: no DOM, no fetch. The starts are the
// server's own; the lengths are the container's interval, minimum and maximum
// inside the free time those starts cover. The server has the last word: a time
// it refuses shows its text.
import { label, plural } from './labels.js';
import { freeRanges, hhmm } from './words.js';

const MINUTE = 60000;
const minutesBetween = (from, to) => (Date.parse(to) - Date.parse(from)) / MINUTE;

// The words for a booking that could not be added (`add.refused`), as a function
// because the label set is read when the page starts.
export const refusedWords = () => label('add.refused');

export function lengthWords(minutes) {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return [hours && plural('length.hours', hours), rest && label('length.minutes', { count: rest })]
    .filter(Boolean).join(' ') || label('length.minutes', { count: 0 });
}

// A night: one fixed period that ends on a later date than it starts.
export const isNight = (container) =>
  container.periodType === 'fixed'
  && container.periods.some((period) => period.to.slice(0, 10) > period.day);

// Each bookable start of the container in the day, earliest first.
export function startsOf(container) {
  const found = new Map();
  for (const period of container.periods) {
    for (const start of period.starts) {
      if (!found.has(start.at)) found.set(start.at, { at: start.at, end: start.end, row: start.row, period });
    }
  }
  return [...found.values()].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
}

// The ends a start can have: a fixed one has its own; a flexible one every
// whole interval from the minimum to the maximum length that stays inside the
// free time. [{end, minutes}] shortest first.
export function endsFor(container, start) {
  if (container.periodType === 'fixed') return [{ end: start.end, minutes: minutesBetween(start.at, start.end) }];
  const range = freeRanges(container)
    .find(([from, to]) => Date.parse(from) <= Date.parse(start.at) && Date.parse(start.at) < Date.parse(to));
  if (!range) return [];
  const limit = Date.parse(range[1]);
  const ends = [];
  for (let index = start.row; index < start.period.rows.length; index += 1) {
    const { end } = start.period.rows[index];
    if (Date.parse(end) > limit) break;
    const minutes = minutesBetween(start.at, end);
    if (minutes > container.maximum) break;
    if (minutes >= container.minimum && (minutes - container.minimum) % container.interval === 0) {
      ends.push({ end, minutes });
    }
  }
  return ends;
}

// Why the end stops early . Only for a flexible container
// whose chosen end is the latest its start allows and lies before the end of
// the day's last row. `{kind: 'closed', at}`: the next hour is closed time
// (blank on the strip); `{kind: 'category', at}`: the hour is outside the
// chosen category's hours only; `{kind: 'maximum', minutes}`: free time lies ahead and
// the container's longest length stops the end. Null where the strip already
// shows the reason (another booking, an event, a block, the member's own) and
// where the end is the day's end or the choice is not the longest.
export function endStop(container, choice) {
  if (container.periodType === 'fixed') return null;
  const ends = endsFor(container, choice.start);
  const latest = ends[ends.length - 1];
  if (!latest || latest.end !== choice.end) return null;
  const rows = container.periods.flatMap((period) => period.rows);
  const dayEnd = Math.max(...rows.map((row) => Date.parse(row.end)));
  if (!(Date.parse(choice.end) < dayEnd)) return null;
  const { period } = choice.start;
  const index = period.rows.findIndex((row) => row.at === choice.end);
  if (index < 0) {
    // Another period of the container starts right here: open time, no false reason.
    if (rows.some((row) => row.at === choice.end)) return null;
    return { kind: 'closed', at: choice.end };
  }
  if (period.rows[index].closed) return { kind: 'closed', at: choice.end };
  // Blank only by the category's hours: closed to this category, open to others.
  if (period.rows[index].blank) return { kind: 'category', at: choice.end };
  const open = period.columns.some((column) => column.kinds[index] === 'free');
  if (open && latest.minutes + container.interval > container.maximum) {
    return { kind: 'maximum', minutes: latest.minutes };
  }
  return null;
}

// The quiet line under the end: "Closed from 16:00", "At most 4 h"; null for none.
export function endStopWords(container, choice, category = '') {
  const stop = endStop(container, choice);
  if (!stop) return null;
  if (stop.kind === 'closed') return label('add.closed-from', { time: hhmm(stop.at) });
  if (stop.kind === 'category') return label('add.not-as-from', { category, time: hhmm(stop.at) });
  return label('add.at-most', { length: lengthWords(stop.minutes) });
}

// Three steps of the container's interval: how long the first proposal is
// when the free time and the longest booking allow it.
const PROPOSED_STEPS = 3;

// The proposed end of `ends` (shortest first): the shortest booking, longer
// when it fits, up to three intervals (the operator's demo, engine.js
// `proposal`). A shared container's longer booking needs the same spots free
// all the way, which only the server's start (its shortest) is known to have.
function proposedEnd(container, ends) {
  if (container.shared) return ends[0];
  const wanted = Math.max(container.minimum, PROPOSED_STEPS * container.interval);
  const fitting = ends.filter((row) => row.minutes <= wanted);
  return fitting.length ? fitting[fitting.length - 1] : ends[0];
}

// The start to open on: `from` when the member tapped an hour, else the first,
// with the proposed length.
export function firstChoice(container, from = null) {
  const starts = startsOf(container);
  const start = starts.find((row) => row.at === from) || starts[0];
  if (!start) return null;
  const ends = endsFor(container, start);
  return ends.length ? { start, end: proposedEnd(container, ends).end } : null;
}

// A new start keeps the length when the new start allows it, else the proposed one.
export function moveStart(container, start, kept) {
  const ends = endsFor(container, start);
  const same = ends.find((row) => row.minutes === kept);
  const chosen = same || (ends.length ? proposedEnd(container, ends) : null);
  return chosen ? { start, end: chosen.end } : null;
}

// The free spots from `start` to `end`: every column of the period whose cells
// are free all the way. Null for a container without a floor plan.
export function spotsFor(container, start, end) {
  if (!container.plan) return null;
  const { rows, columns } = start.period;
  let last = start.row;
  for (let index = start.row; index < rows.length; index += 1) {
    if (Date.parse(rows[index].end) <= Date.parse(end)) last = index;
  }
  const byNumber = new Map(container.plan.spots.map((spot) => [spot.number, spot]));
  const free = columns
    .filter((column) => column.spot !== null
      && column.kinds.slice(start.row, last + 1).every((kind) => kind === 'free'))
    .map((column) => byNumber.get(column.spot))
    .filter(Boolean);
  const named = free.some((spot) => spot.name);
  // The floor plan's own word ("seat", "bed"); the label's only when it has none.
  const word = container.plan.spotWord || label('spot.word');
  const groups = container.plan.groups || [];
  const inGroup = (spots, group) => spots.filter((spot) => (spot.group ?? null) === group.id).length;
  const alone = groups.length === 1 && inGroup(container.plan.spots, groups[0]) === container.plan.spots.length;
  return {
    named,
    free,
    total: container.plan.spots.length,
    groups,
    // Each group's count, for spots without names: groups always show
    // . One group that holds every spot (the one asked to keep
    // together) is the count itself.
    groupCounts: alone ? [] : groups.map((group) => ({
      id: group.id,
      words: label('add.group-free', { group: group.name, free: inGroup(free, group), total: inGroup(container.plan.spots, group) }),
    })),
    count: label('add.spots-free', { free: free.length, total: container.plan.spots.length }),
    word,
  };
}

// Spots chosen one by one must be as many as the places asked for, or none (the
// server takes the lowest free).
export const picksOk = (picks, quantity) => picks.length === 0 || picks.length === quantity;

export const quantityOf = (container, filter) => (container.shared ? filter.quantity : 1);

export function togglePick(picks, number, quantity) {
  if (picks.includes(number)) return picks.filter((one) => one !== number);
  return picks.length >= quantity ? [...picks.slice(1), number] : [...picks, number];
}

// The price question, `readPrice`'s query.
export function priceQuery({ container, filter, start, end, picks }) {
  const query = {
    container: container.id,
    category: filter.category,
    quantity: quantityOf(container, filter),
    start,
    end,
  };
  if (picks.length) query.spots = picks;
  if (filter.group != null) query.group = filter.group;
  return query;
}

// The booking `addToCart` posts: the price question and the total shown.
export function bookingBody(query, total) {
  const { spots, group, ...rest } = query;
  const body = { ...rest, total };
  if (spots) body.spots = spots;
  if (group !== undefined) body.group = group;
  return body;
}

// --- Money and price lines --------------------------------------------------

export function moneyWords(amount, currency) {
  const digits = new Intl.NumberFormat('nb-NO', { style: 'currency', currency }).resolvedOptions().maximumFractionDigits;
  const value = amount / 10 ** digits;
  const places = Number.isInteger(value) ? 0 : digits;
  return new Intl.NumberFormat('nb-NO', {
    style: 'currency', currency, minimumFractionDigits: places, maximumFractionDigits: places,
  }).format(value).replace(/[  ]/g, ' ');
}

const clock = (minutes) => `${String(Math.floor(minutes / 60) % 24).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;

const wallMinute = (moment) => Number(moment.slice(11, 13)) * 60 + Number(moment.slice(14, 16));
const DAY = 24 * 60;

// `readPrice`'s answer as lines: "Up to 2 h", "Next 1 h" for tiers by length,
// "08:00–16:00" for tiers by time of day (the demo's `lineText`). With the
// `booking` priced (`{start, end}`), a time-of-day line is the booked part of
// its tier, "10:00–15:00", not the tier's whole span ; over midnight it goes on the next day.
export function priceView(price, booking = null) {
  const before = new Map();
  const from = booking ? wallMinute(booking.start) : null;
  const to = booking
    ? from + (Date.parse(booking.end.slice(0, 10)) - Date.parse(booking.start.slice(0, 10))) / 60000 + wallMinute(booking.end) - wallMinute(booking.start)
    : null;
  const lines = price.lines.map((line) => {
    const key = line.group ?? null;
    let what;
    if (price.tiers_go_by === 'length') {
      const previous = before.get(key) ?? 0;
      what = previous === 0
        ? label('price.first', { length: lengthWords(line.up_to) })
        : label('price.next', { length: lengthWords(line.up_to - previous) });
      before.set(key, line.up_to);
    } else {
      const previous = before.get(key) ?? (booking ? from : 0);
      const end = booking ? Math.min(previous - (previous % DAY) + line.up_to, to) : line.up_to;
      what = `${clock(previous)}–${clock(end)}`;
      before.set(key, end);
    }
    const how = line.times_paid > 1
      ? label('price.how', {
        times: line.times_paid, amount: moneyWords(line.amount, price.currency), length: lengthWords(line.paid_per),
      })
      : null;
    return { what, how, total: moneyWords(line.total, price.currency) };
  });
  return {
    lines,
    rows: booking ? priceRows(price, booking) : null,
    amount: price.total,
    total: price.total ? moneyWords(price.total, price.currency) : null,
  };
}

// --- The two calls ----------------------------------------------------------

// A price read as the sheet needs it: priced, refused with the server's text,
// or an error that is not a refusal.
export function quoteOf(result) {
  if (result.ok) return { kind: 'priced', price: result.data };
  if (result.kind === 'refused') return { kind: 'refused', text: result.text || refusedWords() };
  return { kind: result.kind };
}

// Add the booking. Success clears the kept calendars (every one is old now) and
// hands back the cart the server answered; a refusal carries the server's text.
export async function submitAdd({ csrf, booking, add, store }) {
  const result = await add(csrf, booking);
  if (result.ok) {
    store.clear();
    return { kind: 'added', cart: result.data };
  }
  if (result.kind === 'refused') return { kind: 'refused', code: result.code, text: result.text || refusedWords() };
  return { kind: result.kind };
}

// --- The button on a container ----------------------------------------------

// The demo's three words: "Choose a time" for a flexible container, "Book this
// night" for a night, "Book" for any other fixed one.
export function buttonWords(container) {
  if (container.periodType !== 'fixed') return label('button.choose-time');
  return label(isNight(container) ? 'button.book-night' : 'button.book');
}

// After a failed add: the text to show (the server's own for a refusal) and
// that the price is read again, so the next try sends today's total.
// `held`: an order of the member's is waiting for payment, so the cart takes
// nothing until it is paid or released; the sheet links to its payment page.
export const afterFailure = (done) => ({
  text: done.kind === 'refused' ? done.text : refusedWords(), requote: true, held: done.code === 'order-held',
});

export const canBook = (container) => firstChoice(container) !== null;

// The price's lines with the time each pays for and how ("we need more
// details on pricing"): "10:00–14:00 · 4 h", its tier ("Up to 4 h", "After
// 4 h"), and "kr 100 per started 4 h" where what is paid differs from what is
// used. By time of day a line's hours are its clock span.
export function priceRows(price, booking) {
  const from = wallMinute(booking.start);
  const length = Math.round((Date.parse(booking.end) - Date.parse(booking.start)) / 60000);
  const before = new Map();
  return price.lines.map((line) => {
    const key = line.group ?? null;
    let start;
    let end;
    let what = null;
    if (price.tiers_go_by === 'length') {
      const previous = before.get(key) ?? 0;
      start = from + previous;
      end = from + Math.min(line.up_to, length);
      what = previous === 0
        ? label('price.first', { length: lengthWords(line.up_to) })
        : label('price.after', { length: lengthWords(previous) });
      before.set(key, line.up_to);
    } else {
      const previous = before.get(key) ?? from;
      start = previous;
      end = Math.min(previous - (previous % DAY) + line.up_to, from + length);
      before.set(key, end);
    }
    const used = end - start;
    const values = { amount: moneyWords(line.amount, price.currency), length: lengthWords(line.paid_per) };
    let how = null;
    if (line.times_paid > 1) how = label('price.how', { times: line.times_paid, ...values });
    else if (line.paid_per * line.times_paid !== used) how = label('price.per', values);
    const note = what && how ? label('price.row-how', { what, how }) : (what || how);
    return { span: `${clock(start)}–${clock(end)}`, hours: lengthWords(used), note, total: moneyWords(line.total, price.currency) };
  });
}
