// SPDX-License-Identifier: AGPL-3.0-only
// What the Month's day card and the add sheet's time slider are made of (part
// 3.96), from the day's container as `buildCalendar` made it (model.js). Pure: no
// DOM, no fetch. The starts and prices are the server's, never worked out here.
import {
  endsFor, lengthWords, moneyWords, startsOf,
} from './add.js';
import { label } from './labels.js';
import {
  assetHours, clockOf, firmest, minutesIn, stateKey,
} from './stack.js';
import {
  anyTaken, bookingBlocks, cartBlocks, dateWords, freeRanges, hoursWords,
} from './words.js';

const HOUR = 60;
const MINUTE = 60000;

const gcd = (a, b) => (b ? gcd(b, a % b) : Math.abs(a));

// --- The slider --------------------------------------------------------------

// The slider of a flexible container for the choice made (`{start, end}` as
// add.js has it): the free run that holds the start, in minutes from midnight of
// `date`; the step every start and end lies on; the hour marks; the thumbs'
// values. Null for a fixed container or a start in no free run.
export function sliderModel(container, date, choice) {
  if (!choice || container.periodType === 'fixed') return null;
  const at = Date.parse(choice.start.at);
  const range = freeRanges(container)
    .find(([from, to]) => Date.parse(from) <= at && at < Date.parse(to));
  if (!range) return null;
  const from = minutesIn(date, range[0]);
  const to = minutesIn(date, range[1]);
  const inRun = startsOf(container).map((one) => minutesIn(date, one.at)).filter((minute) => minute >= from && minute < to);
  const step = [container.interval, container.minimum, ...inRun.map((minute) => minute - from)].reduce(gcd, 0) || container.interval;
  const marks = [];
  for (let minute = from; minute <= to; minute += container.interval) {
    marks.push({ value: minute, label: (minute - from) % (2 * HOUR) === 0 || minute === to ? clockOf(minute).slice(0, 2) : '' });
  }
  const start = minutesIn(date, choice.start.at);
  const end = minutesIn(date, choice.end);
  return { from, to, step, marks, value: [start, end], minutes: end - start };
}

// Of the minutes that may be chosen, the one a thumb moved to `target` takes: the
// next one in the direction of the move, or the last one that way when none lies
// beyond. A thumb never rests on a time the server does not offer.
function nextOf(candidates, target, current) {
  const sorted = [...new Set(candidates)].sort((a, b) => a - b);
  if (target === current) return current;
  if (target > current) return sorted.find((minute) => minute >= target) ?? sorted[sorted.length - 1];
  return [...sorted].reverse().find((minute) => minute <= target) ?? sorted[0];
}

// The choice after `thumb` (0 start, 1 end) was moved to `value[thumb]`. The
// other thumb stays. A start must be one the server offers and must reach the
// end kept; an end must be one the start offers (the container's interval,
// minimum and maximum inside the free time). Only these can be chosen.
export function slideTo(container, date, choice, value, thumb) {
  const model = sliderModel(container, date, choice);
  if (!model) return choice;
  const [start, end] = model.value;
  if (thumb === 0) {
    const starts = startsOf(container).filter((one) => minutesIn(date, one.at) >= model.from && minutesIn(date, one.at) < model.to);
    const reaching = starts.filter((one) => endsFor(container, one).some((row) => minutesIn(date, row.end) === end));
    const minute = nextOf(reaching.map((one) => minutesIn(date, one.at)), value[0], start);
    const found = reaching.find((one) => minutesIn(date, one.at) === minute);
    return found ? { start: found, end: choice.end } : choice;
  }
  const ends = endsFor(container, choice.start);
  const minute = nextOf(ends.map((row) => minutesIn(date, row.end)), value[1], end);
  const found = ends.find((row) => minutesIn(date, row.end) === minute);
  return found ? { start: choice.start, end: found.end } : choice;
}

// --- The price on a card -----------------------------------------------------

// One tier set's price for a booking from `start` to `end` (minutes), as
// randirandi/core/pricing.py `_parts` charges it: by length, each tier takes
// its part and each started "paid per" is paid; by time of day, the minutes
// inside each tier's clock span. Null when the tiers do not cover it.
export function tierParts(tiers, goesBy, start, end) {
  const parts = [];
  let previous = 0;
  const length = end - start;
  for (const tier of tiers) {
    const minutes = goesBy === 'length'
      ? Math.min(length, tier.up_to) - previous
      : Math.min(end, tier.up_to) - Math.max(start, previous);
    if (minutes > 0) parts.push({ tier, minutes });
    previous = tier.up_to;
  }
  const reach = goesBy === 'length' ? length : end;
  return reach > previous ? null : parts;
}

export const partTotal = ({ tier, minutes }) => Math.ceil(minutes / tier.paid_per) * tier.amount;

function tiersPrice(tiers, goesBy, start, end) {
  const parts = tierParts(tiers, goesBy, start, end);
  return parts === null ? null : parts.reduce((sum, part) => sum + partTotal(part), 0);
}

const clockMinute = (moment) => Number(moment.slice(11, 13)) * HOUR + Number(moment.slice(14, 16));

// The minutes a booking is priced for: a fixed period's on the clock (its
// nominal length, also on a clock-change date, as pricing.py has it); a
// flexible booking's actual minutes.
function pricedMinutes(container, start, end) {
  if (container.periodType !== 'fixed') return (Date.parse(end) - Date.parse(start)) / MINUTE;
  const days = (Date.parse(end.slice(0, 10)) - Date.parse(start.slice(0, 10))) / (24 * HOUR * MINUTE);
  return days * 24 * HOUR + clockMinute(end) - clockMinute(start);
}

// The tier parts of one booking by the calendar's tiers, with the money's currency
// and how many times each is paid; null where the server alone can tell.
function worked(container, category, quantity, start, end, group, picks) {
  const prices = container.withPayment ? container.prices : null;
  const row = prices?.categories.find((one) => Number(one.category) === Number(category));
  if (!row) return null;
  let set = { tiers: row.tiers, goesBy: row.tiers_go_by };
  if (row.groups.length) {
    if (picks.length || group == null) return null;
    const own = row.groups.find((one) => Number(one.group) === Number(group));
    if (own) set = { tiers: own.tiers, goesBy: own.tiers_go_by };
  }
  const from = clockMinute(start);
  const parts = tierParts(set.tiers, set.goesBy, from, from + pricedMinutes(container, start, end));
  if (parts === null) return null;
  return {
    parts, goesBy: set.goesBy, currency: prices.currency,
    times: quantity > 0 && row.billing_basis === 'per_capacity_unit' ? quantity : 1,
  };
}

// What the calendar's tiers give one booking from `start` to `end`, shown while
// the server's price is on its way. Null when the server alone can tell: no
// payment or no price for the category, or groups of spots that may have their
// own price and no group is chosen.
export function estimate(container, category, quantity, start, end, group = null, picks = []) {
  const found = worked(container, category, quantity, start, end, group, picks);
  return found && found.parts.reduce((sum, part) => sum + partTotal(part), 0) * found.times;
}

// The same as the server's price answer (`readPrice`), its lines and total, from
// the calendar's tiers, so the sheet shows the lines of a new choice at once
// and the server's answer is the check (part 3.997). Null where `estimate` is
// null, and for a price per place for more than one place, whose lines only
// the server words.
export function estimateLines(container, category, quantity, start, end, group = null, picks = []) {
  const found = worked(container, category, quantity, start, end, group, picks);
  if (!found || found.times !== 1) return null;
  const lines = found.parts.map((part) => ({
    up_to: part.tier.up_to,
    paid_per: part.tier.paid_per,
    times_paid: Math.ceil(part.minutes / part.tier.paid_per),
    amount: part.tier.amount,
    total: partTotal(part),
    group: null,
  }));
  return {
    available: true, currency: found.currency, tiers_go_by: found.goesBy, lines, total: lines.reduce((sum, line) => sum + line.total, 0),
  };
}

// The bookings a card's price is worked out for, `{at, minutes}`: a fixed
// container's periods, each whole from every start the server offers (the
// period itself when none is); a flexible one's shortest booking from every start
// the server offers (from every hour of its time when none is).
function bookingsOf(container) {
  const found = [];
  if (container.periodType === 'fixed') {
    for (const period of container.periods) {
      const starts = period.starts.map((start) => ({ at: period.rows[start.row].at, end: start.end }));
      if (!starts.length) starts.push({ at: period.from, end: period.to });
      for (const { at, end } of starts) found.push({ at, minutes: pricedMinutes(container, at, end) });
    }
    return found;
  }
  const offered = startsOf(container);
  if (offered.length) {
    for (const start of offered) {
      const [shortest] = endsFor(container, start);
      if (shortest) found.push({ at: start.at, minutes: shortest.minutes });
    }
    return found;
  }
  for (const period of container.periods) {
    for (const row of period.rows) {
      if (Date.parse(row.at) + container.minimum * MINUTE <= Date.parse(period.to)) found.push({ at: row.at, minutes: container.minimum });
    }
  }
  return found;
}

// The cheapest price of the container for a category, in the smallest unit of
// the currency, and whether it is "from": a flexible container, or prices that
// differ between starts or between groups of spots. Null when the container takes
// no payment or has no price for the category. `quantity`: the places asked for
// (a price per place is paid for each).
export function fromPrice(container, category, quantity = 1) {
  const prices = container.withPayment ? container.prices : null;
  const row = prices?.categories.find((one) => Number(one.category) === Number(category));
  if (!row) return null;
  const sets = [{ tiers: row.tiers, goesBy: row.tiers_go_by }, ...row.groups.map((group) => ({ tiers: group.tiers, goesBy: group.tiers_go_by }))];
  const times = quantity > 0 && row.billing_basis === 'per_capacity_unit' ? quantity : 1;
  const amounts = [];
  for (const { at, minutes } of bookingsOf(container)) {
    const start = clockMinute(at);
    const end = start + minutes;
    for (const set of sets) {
      const amount = tiersPrice(set.tiers, set.goesBy, start, end);
      if (amount !== null) amounts.push(amount * times);
    }
  }
  if (!amounts.length) return null;
  const low = Math.min(...amounts);
  return { amount: low, from: container.periodType !== 'fixed' || amounts.some((amount) => amount !== low) };
}

// The words on the card's right: "from 500 kr", "500 kr", "Free of charge";
// nothing for a container without payment (or without a price for the category).
export function priceWords(container, category, quantity = 1) {
  const found = fromPrice(container, category, quantity);
  if (!found) return null;
  if (found.amount === 0) return label('cart.free-of-charge');
  const money = moneyWords(found.amount, container.prices.currency);
  return found.from ? label('offer.price-from', { price: money }) : money;
}

// The words of one booking's price from `start` to `end` ("250 kr", "No charge"),
// from the calendar's prices, so a list of offers asks the server nothing; the
// container's "from" words where only the server can tell (groups, no group
// chosen); nothing without payment.
export function bookingPriceWords(container, category, quantity, start, end, group = null) {
  const amount = estimate(container, category, quantity, start, end, group);
  if (amount === null) return priceWords(container, category, quantity);
  return amount ? moneyWords(amount, container.prices.currency) : label('price.free');
}

// --- The hour strip and the line ---------------------------------------------

// A flexible container's hours on the day, one cell each (the stack's own hours
// of one container): its state (`free`, `other`, `held`, `yours`, `cart`,
// `blank`), whether its hour is written under it (every second hour), the start
// a tap on a free cell opens the sheet at, and its spoken name. Null for a
// fixed container. `offer`: the name its cells are spoken with.
export function strip(container, date, cart = null, offer = container.type.name) {
  if (container.periodType === 'fixed') return null;
  const starts = startsOf(container);
  return assetHours(date, [container], cart).map((hour, index) => {
    const first = hour.state === 'free'
      ? starts.find((one) => minutesIn(date, one.at) >= hour.minute && minutesIn(date, one.at) < hour.minute + HOUR)
      : null;
    return {
      ...hour,
      tick: index % 2 === 0,
      from: first ? first.at : null,
      name: hour.word ? label('strip.cell', { offer, date: dateWords(date), from: hour.label, to: hour.end, state: hour.word }) : null,
    };
  });
}

// The strip's hours as the blocks the card draws (part 3.99898): the hours of
// one booking join into one block, which says its word (the member's name,
// "Booked", "Your booking", "In your cart", an event's word) when it spans two
// hours or more. Two different bookings side by side stay two blocks (a named
// booking is told by its name and hours). Free, blank and closed hours never
// join: a free hour stays one button, closed and blank say nothing. `start` is
// the block's first grid column (from 1), `span` its hours, `pick` the start a
// tap on a free hour opens the sheet at, `booked` what a named booking's sheet
// says. `name` speaks the whole span (from, to, word).
const ALONE = new Set(['free', 'blank', 'closed']);

export function joinHours(cells, offer, date) {
  const runs = [];
  cells.forEach((cell, index) => {
    const key = ALONE.has(cell.state) ? null : `${cell.state}|${cell.word}|${cell.booked ? cell.booked.hours : ''}|${cell.booking ?? ''}`;
    const last = runs[runs.length - 1];
    if (key && last && last.key === key) last.cells.push(cell);
    else runs.push({ key, start: index + 1, cells: [cell] });
  });
  return runs.map(({ start, cells: own }) => {
    const [first] = own;
    const last = own[own.length - 1];
    const long = own.length >= 2;
    return {
      key: first.minute,
      state: first.state,
      start,
      span: own.length,
      from: first.label,
      to: last.end,
      word: long && !ALONE.has(first.state) ? first.word : null,
      name: long
        ? label('strip.cell', { offer, date: dateWords(date), from: first.label, to: last.end, state: first.word })
        : first.name,
      pick: first.from,
      booked: first.booked ?? null,
    };
  });
}

// The block under a fixed container's line (the night; part 3.99898): the word
// of the booking that takes it (a name opens its sheet through `booked`), or,
// when the night is free to book, the free block with no word (the line already
// says Available). Null when nothing is booked and nothing can be: a closed or
// passed night says nothing. `free`: whether the card offers the booking.
export function nightBlock(container, date, cart, free, offer = container.type.name) {
  // The period the card's line speaks of (`offerLine`): the first open one, else the first.
  const periods = [...container.periods].sort((a, c) => Date.parse(a.from) - Date.parse(c.from));
  if (!periods.length) return null;
  const open = periods.find((one) => one.starts.length > 0);
  const period = open ?? periods[0];
  const speak = (from, to, word) => label('strip.cell', { offer, date: dateWords(date), from: from.slice(11, 16), to: to.slice(11, 16), state: word });
  const blocks = [...bookingBlocks(container), ...cartBlocks(container, date, cart)]
    .filter((one) => one.kind !== 'closed'
      && Date.parse(one.from) < Date.parse(period.to) && Date.parse(period.from) < Date.parse(one.to));
  const shown = (block) => ({
    state: block.kind, word: block.word, booked: block.booked ?? null, free: false,
    name: speak(block.from, block.to, block.word),
  });
  // The member's own (yours, in the cart, lost) first: an older calendar answer
  // may still offer a start in a night the cart now holds.
  const own = blocks.filter((one) => one.kind === 'yours' || one.kind === 'cart' || one.kind === 'lost');
  if (own.length) return shown(own.find((one) => one.kind === firmest(own.map((row) => row.kind))));
  if (open && free) {
    return {
      state: 'free', word: null, booked: null, free: true,
      name: speak(period.from, period.to, label('state.free')),
    };
  }
  const block = blocks.find((one) => one.kind === firmest(blocks.map((row) => row.kind)));
  return block ? shown(block) : null;
}

// The minutes the server offers a start in, joined.
export const freeMinutes = (container) =>
  freeRanges(container).reduce((sum, [from, to]) => sum + (Date.parse(to) - Date.parse(from)) / MINUTE, 0);

// The line under the name: "10:00–22:00 · 12 h free" for a flexible container,
// "22:00–10:00 next day · Available" for a fixed one; where nothing is free the
// word of what takes it, or "Passed". `blocks`: the day list's bookings.
export function offerLine(container, blocks = []) {
  const periods = [...container.periods].sort((a, b) => Date.parse(a.from) - Date.parse(b.from));
  const whole = (list) => hoursWords(list[0].from, list[list.length - 1].to);
  if (container.periodType === 'fixed') {
    const open = periods.filter((period) => period.starts.length > 0);
    if (open.length) return label('offer.line', { hours: whole(open.slice(0, 1)), status: label('state.free') });
    const kind = firmest(blocks.map((block) => block.kind));
    const none = anyTaken(container) ? 'free.nothing' : 'free.passed';
    return label('offer.line', { hours: whole(periods.slice(0, 1)), status: label(kind ? stateKey(kind) : none) });
  }
  const minutes = freeMinutes(container);
  const status = minutes > 0
    ? label('offer.length-free', { length: lengthWords(minutes) })
    : label(blocks.length || anyTaken(container) ? 'free.nothing' : 'free.passed');
  return label('offer.line', { hours: whole(periods), status });
}

// --- The Month's grid --------------------------------------------------------

// The dates with something in the member's cart (the day each booking starts).
export const cartDays = (cart) => new Set((cart?.bookings || []).map((row) => row.start.slice(0, 10)));

// What marks a date on the Month: `mine` (a booking of the member's: the solid
// pill, also when the cart holds something that day), `cart` (a dashed ring), or
// none.
export function dateMark(summary, date, days) {
  if (summary?.mine) return 'mine';
  return summary?.cart || days.has(date) ? 'cart' : null;
}
