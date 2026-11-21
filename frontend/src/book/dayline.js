// SPDX-License-Identifier: AGPL-3.0-only
// The day as a line under each date of the Month and the Week (T3, 2026-11-15;
// the owner: "yes to timline." and "yes, then keep the moon."). The line holds
// the day's hours where they fall, on a span every drawn day shares, so 16:00
// sits in the same place on every tile. A night is not on the line: it is a
// mark by the date, a moon. Pure: no DOM.
import { kindsSeen } from './month.js';
import { assetHours, firmest } from './stack.js';
import { bookingBlocks, cartBlocks, markLost } from './words.js';

const HOUR = 60;

// A night: a fixed period that ends on a later date than it starts (the add
// sheet's rule, `add.js` `isNight`, taken period by period, so the day periods
// of a fixed offer stay on the line).
const overnight = (container, period) => container.periodType === 'fixed' && period.to.slice(0, 10) > period.day;

// The day's containers with only their day periods, or only their nights.
function part(entry, nights) {
  return entry.containers
    .map((container) => ({ ...container, periods: container.periods.filter((period) => overnight(container, period) === nights) }))
    .filter((container) => container.periods.length);
}

// The bookings and cart times of containers as blocks, a cart time someone took
// first `lost`.
const blocksOf = (containers, date, cart) => markLost(containers.flatMap((container) =>
  [...bookingBlocks(container), ...cartBlocks(container, date, cart)]));

// Of the assets' states in one hour, the one the line shows: the member's own
// first, so a booking of theirs is never hidden by another asset's free hour;
// then free, so free time is never hidden by another asset's booking.
const SHOWN = ['yours', 'cart', 'free', 'event', 'held', 'other', 'lost', 'closed', 'blank'];

// The day's hours as runs: each hour the state of its asset's hour
// (`assetHours`: free, other, held, yours, cart, lost, event, closed or blank),
// several assets' merged by `SHOWN`, with its minutes from the day's midnight;
// touching hours of one state make one run.
export function dayRuns(entry, cart = null) {
  const assets = new Map();
  for (const container of part(entry, false)) {
    const key = container.asset?.id ?? `container-${container.id}`;
    assets.set(key, [...(assets.get(key) ?? []), container]);
  }
  const states = new Map();
  for (const containers of assets.values()) {
    for (const hour of assetHours(entry.date, containers, cart)) states.set(hour.minute, [...(states.get(hour.minute) ?? []), hour.state]);
  }
  const runs = [];
  for (const minute of [...states.keys()].sort((a, b) => a - b)) {
    const state = SHOWN.find((one) => states.get(minute).includes(one)) ?? 'blank';
    const last = runs[runs.length - 1];
    if (last && last.state === state && last.to === minute) last.to = minute + HOUR;
    else runs.push({ state, from: minute, to: minute + HOUR });
  }
  return runs;
}

// The night's mark by the date, by the firmest of what is on the day's nights:
// yours, in your cart, an event, or someone else's (booked, being paid for, or
// taken from your cart). A free or closed night has none.
const MOON = { yours: 'yours', cart: 'cart', event: 'event', other: 'other', held: 'other', lost: 'other' };
export function nightMark(entry, cart = null) {
  const kinds = blocksOf(part(entry, true), entry.date, cart).map((block) => (block.kind === 'block' ? 'closed' : block.kind));
  return MOON[firmest(kinds)] ?? null;
}

// A day that carries a line and a moon: neither passed nor closed (the tile's
// and the chip's `open`).
export const drawnDay = (entry) => Boolean(entry) && entry.summary.state !== 'past' && entry.summary.state !== 'closed';

// The line and the moon of every drawn day, by date, worked out once.
export function dayLines(entries, cart = null) {
  return new Map(entries.filter(drawnDay).map((entry) => [entry.date, { runs: dayRuns(entry, cart), night: nightMark(entry, cart) }]));
}

// The span the lines share: from the earliest hour on any drawn day to the
// latest; null when none has one.
export function lineSpan(lines) {
  let from = Infinity;
  let to = -Infinity;
  for (const { runs } of lines.values()) {
    for (const run of runs) {
      from = Math.min(from, run.from);
      to = Math.max(to, run.to);
    }
  }
  return from < to ? { from, to } : null;
}

// What the legend names: the server's kinds of the days shown (`kindsSeen`),
// `cart` when a line or a moon draws a cart time, `night` when a day has a moon.
export function legendSeen(entries, lines) {
  const seen = kindsSeen(entries.filter(Boolean));
  for (const { runs, night } of lines.values()) {
    if (night === 'cart' || runs.some((run) => run.state === 'cart')) seen.add('cart');
    if (night) seen.add('night');
  }
  return seen;
}
