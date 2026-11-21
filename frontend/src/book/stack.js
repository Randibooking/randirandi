// SPDX-License-Identifier: AGPL-3.0-only
// The day of an asset whose containers stack :
// the asset's day first, free or booked, hour by hour; a tap on a free hour lists
// every offer of the asset and says why the others cannot start there. Pure: no
// DOM, no fetch. A booking on any container of the asset takes the asset's time and
// the server already shows that time taken on every container of the asset
// (randirandi/core/member_calendar.py), so the hours are read off the containers'
// own answers. Each container's bookable starts are the server's (`period.starts`),
// never worked out here.
import {
  firstChoice, isNight, lengthWords, startsOf,
} from './add.js';
import { label } from './labels.js';
import {
  bookingBlocks, cartBlocks, clockOf, dateWords, dayList, freeRanges, hoursWords, markLost,
} from './words.js';

const HOUR = 60;
const DAY = 1440;

const utc = (date) => {
  const [year, month, day] = date.split('-').map(Number);
  return Date.UTC(year, month - 1, day);
};

// Minutes from midnight of `date` to a moment ("2026-10-20T17:00:00+02:00").
export const minutesIn = (date, moment) =>
  Math.round((utc(moment.slice(0, 10)) - utc(date)) / 86400000) * DAY
  + Number(moment.slice(11, 13)) * HOUR + Number(moment.slice(14, 16));

// "17:00" for a minute from midnight (`words.js`, where the day's spoken name
// needs it too).
export { clockOf };

// The label key of the word on an hour, by what takes it.
const WORDS = {
  other: 'state.booked', held: 'state.held', yours: 'state.yours', cart: 'state.cart', lost: 'state.lost', event: 'state.event', closed: 'state.closed',
};
// Of two bookings over the same time the first of these is shown: yours, in
// your cart, someone paying, booked.
const FIRMNESS = ['yours', 'lost', 'cart', 'held', 'other', 'event', 'closed'];
const firmer = (a, b) => (FIRMNESS.indexOf(a) <= FIRMNESS.indexOf(b) ? a : b);

// The word on a state (`state.booked`, ...), as a label key.
export const stateKey = (kind) => WORDS[kind] ?? null;
// The firmest of some kinds of booking, or null.
export const firmest = (kinds) => (kinds.length ? kinds.reduce(firmer) : null);

const byDay = (entry) => entry.containers.filter((container) => !isNight(container));

// The assets with more than one day-time container in the day, each with those
// containers in the server's order. Nights are left out: they keep their own card.
export function stackingAssets(entry) {
  const found = new Map();
  for (const container of byDay(entry)) {
    if (!found.has(container.asset.id)) found.set(container.asset.id, { asset: container.asset, containers: [] });
    found.get(container.asset.id).containers.push(container);
  }
  return [...found.values()]
    .filter((one) => one.containers.length > 1)
    .map((one) => ({ ...one, containers: one.containers.sort((a, b) => a.number - b.number) }));
}

// One container's state in the hour [from, to): a booking (its kind), free, or
// blank. A row is free when any column is free, a booking when every open column
// is taken.
function stateIn(container, date, from, to) {
  let taken = null;
  let free = false;
  for (const period of container.periods) {
    period.rows.forEach((row, index) => {
      if (!(minutesIn(date, row.at) < to && minutesIn(date, row.end) > from)) return;
      const kinds = period.columns.map((column) => column.kinds[index]).filter((kind) => kind !== 'blank');
      if (kinds.length === 0) return;
      if (kinds.includes('free')) free = true;
      else {
        for (const kind of kinds.map((one) => (one === 'block' ? 'closed' : one))) taken = taken ? firmer(taken, kind) : kind;
      }
    });
  }
  return { taken, free };
}

// The asset's hours as a column: from the earliest start of its day-time periods
// to the latest end, on whole hours. Each hour is a booking (`other`, `held`,
// `yours`, or `cart`), `free` (the asset is not taken and a container has a
// server start inside the hour) or `blank` (nothing can start: closed, passed,
// too short before the next booking).
export function assetHours(date, containers, cart = null) {
  let first = Infinity;
  let last = -Infinity;
  for (const container of containers) {
    for (const period of container.periods) {
      first = Math.min(first, minutesIn(date, period.from));
      last = Math.max(last, minutesIn(date, period.to));
    }
  }
  if (first === Infinity) return [];
  const every = markLost([...containers.flatMap((container) => bookingBlocks(container)),
    ...containers.flatMap((container) => cartBlocks(container, date, cart))]);
  const carts = every
    .filter((block) => block.kind === 'cart' || block.kind === 'lost')
    .map((block) => [minutesIn(date, block.from), minutesIn(date, block.to), block.kind]);
  // Another member's named bookings . The server shows one booking
  // taken on every container of the asset, so the same booking is found once.
  const named = containers.flatMap((container) => bookingBlocks(container)).filter((block) => block.booked);
  const hours = [];
  for (let minute = Math.floor(first / HOUR) * HOUR; minute < Math.ceil(last / HOUR) * HOUR; minute += HOUR) {
    const states = containers.map((container) => stateIn(container, date, minute, minute + HOUR));
    const kinds = states.map((one) => one.taken).filter(Boolean);
    for (const [from, to, kind] of carts) {
      if (from < minute + HOUR && to > minute) kinds.push(kind);
    }
    const starts = containers.some((container) => startsOf(container)
      .some((one) => minutesIn(date, one.at) >= minute && minutesIn(date, one.at) < minute + HOUR));
    // Decided by starts first: an hour with a server start in it is free; it is
    // a booking only when no start is left in it and a booking covers it.
    let state = 'blank';
    if (starts) state = 'free';
    else if (kinds.length) state = kinds.reduce(firmer);
    // An hour taken by exactly one named booking says its member's name and
    // opens its sheet; with two different bookings over it, it says Booked.
    const over = state === 'other' || state === 'held'
      ? [...new Map(named.filter((block) => block.kind === state && minutesIn(date, block.from) < minute + HOUR && minutesIn(date, block.to) > minute)
        .map((block) => [`${block.name}|${block.from}|${block.to}`, block])).values()]
      : [];
    const who = over.length === 1 ? over[0] : null;
    // Which booking covers the hour, when exactly one block of its kind does
    // (the block's own key), so two bookings that touch stay two blocks.
    const covering = [...new Set(every.filter((block) => block.kind === state
      && minutesIn(date, block.from) < minute + HOUR && minutesIn(date, block.to) > minute).map((block) => block.key))];
    // An event's hours say its title when exactly one event covers the hour
    // (two different events side by side stay two blocks); no title, "Event".
    const events = state === 'event'
      ? [...new Map(every.filter((block) => block.kind === 'event' && block.event
        && minutesIn(date, block.from) < minute + HOUR && minutesIn(date, block.to) > minute)
        .map((block) => [block.event.id ?? block.key, block.event])).values()]
      : [];
    const title = events.length === 1 && events[0].title ? events[0].title : null;
    hours.push({
      minute, label: clockOf(minute), end: clockOf(minute + HOUR), state,
      ...(covering.length === 1 && { booking: covering[0] }),
      word: who ? who.name : (title ?? (state === 'free' ? label('state.free') : (WORDS[state] ? label(WORDS[state]) : ''))),
      // A stacked asset's booking is the asset's, not one container's.
      ...(who && { booked: { ...who.booked, ...(containers.length > 1 && { place: containers[0].asset.name }) } }),
    });
  }
  return hours;
}

// The bookings of the asset as blocks: one per stretch, never two over each
// other. Where blocks of several containers overlap, the firmest shows (yours,
// in your cart, someone paying, booked).
export function assetBlocks(date, containers, cart = null) {
  const all = markLost(containers.flatMap((container) =>
    [...bookingBlocks(container), ...cartBlocks(container, date, cart)].map((block) => ({ ...block, container }))));
  const moments = new Map();
  for (const block of all) {
    moments.set(Date.parse(block.from), block.from);
    moments.set(Date.parse(block.to), block.to);
  }
  const edges = [...moments.keys()].sort((a, b) => a - b);
  const out = [];
  for (let index = 0; index + 1 < edges.length; index += 1) {
    const covering = all.filter((block) => Date.parse(block.from) <= edges[index] && Date.parse(block.to) >= edges[index + 1]);
    if (!covering.length) continue;
    const win = covering.reduce((one, two) => (FIRMNESS.indexOf(two.kind) < FIRMNESS.indexOf(one.kind) ? two : one));
    const last = out[out.length - 1];
    if (last && last.source === win.key && Date.parse(last.to) === edges[index]) {
      last.to = moments.get(edges[index + 1]);
    } else {
      out.push({ ...win, source: win.key, from: moments.get(edges[index]), to: moments.get(edges[index + 1]) });
    }
  }
  return out.map(({ source, ...block }) => ({
    ...block, key: `${block.kind}|${block.from}|${block.to}`, hours: hoursWords(block.from, block.to),
    ...(block.booked && containers.length > 1 && { booked: { ...block.booked, place: block.container.asset.name } }),
  }));
}

// Every day-time offer of the asset at the hour starting `minute`. One with a
// server start in that hour: its first choice (the add sheet's own). One without:
// why, in the drawing's words made specific.
export function offersAt(containers, date, minute) {
  const time = clockOf(minute);
  return containers.map((container) => {
    const starts = startsOf(container);
    const here = starts.find((one) => minutesIn(date, one.at) >= minute && minutesIn(date, one.at) < minute + HOUR);
    const choice = here ? firstChoice(container, here.at) : null;
    if (choice) return { container, choice, reason: null };
    const open = container.periodType !== 'fixed'
      && freeRanges(container).some(([from, to]) => minutesIn(date, from) <= minute && minute < minutesIn(date, to));
    if (open && Number.isFinite(container.minimum) && container.minimum > 0) return { container, choice: null, reason: label('stack.needs', { length: lengthWords(container.minimum), time }) };
    const next = starts.find((one) => minutesIn(date, one.at) > minute);
    if (next) return { container, choice: null, reason: label('stack.next-start', { time: clockOf(minutesIn(date, next.at)) }) };
    return { container, choice: null, reason: label('stack.no-start', { time }) };
  });
}

// The offers that can start there.
export const bookable = (offers) => offers.filter((offer) => offer.choice);

// What a screen reader hears on a free hour of an asset.
export const hourName = (date, asset, hour) => label('stack.hour-name', {
  asset: asset.name, date: dateWords(date), from: hour.label, to: hour.end,
});

// What the hour's offer says: its hours and its first choice's own hours.
export const choiceHours = (choice) => hoursWords(choice.start.at, choice.end);

// When a card's time starts that day, in minutes from midnight: the earliest
// start of its containers' periods.
export function cardStart(date, containers) {
  const starts = containers.flatMap((container) => container.periods.map((period) => minutesIn(date, period.from)));
  return starts.length ? Math.min(...starts) : Infinity;
}

// The day's cards in time order: by when their time starts that day, then the
// organization's order of booking types (`order`, their names; a type not in
// it goes last), then the server's order.
export function inTimeOrder(cards, date, order = []) {
  const rank = (card) => {
    const names = card.containers.map((container) => order.indexOf(container.type.name)).map((at) => (at < 0 ? order.length : at));
    return Math.min(...names);
  };
  return cards
    .map((card, index) => ({ card, index, start: cardStart(date, card.containers), rank: rank(card) }))
    .sort((a, b) => a.start - b.start || a.rank - b.rank || a.index - b.index)
    .map((one) => one.card);
}

// What the day panel lists: a card per container, except that an asset whose
// containers stack gets one card (at its first container's place); then in
// time order.
export function dayCards(entry, cart = null, order = []) {
  const items = dayList(entry, cart);
  const stacks = new Map(stackingAssets(entry).map((one) => [one.asset.id, one]));
  const done = new Set();
  const cards = [];
  entry.containers.forEach((container, index) => {
    const stack = !isNight(container) && stacks.get(container.asset.id);
    if (!stack) {
      cards.push({ kind: 'container', key: `container-${container.id}`, item: items[index], container, containers: [container] });
      return;
    }
    if (done.has(container.asset.id)) return;
    done.add(container.asset.id);
    cards.push({
      kind: 'asset',
      key: `asset-${container.asset.id}`,
      asset: stack.asset,
      containers: stack.containers,
      hours: assetHours(entry.date, stack.containers, cart),
    });
  });
  return inTimeOrder(cards, entry.date, order);
}
