// SPDX-License-Identifier: AGPL-3.0-only
// The cart page, the payment and the outcome page (part 3.7), from the server's
// own answers. Pure: no DOM, no fetch.
import { moneyWords } from './add.js';
import { label, plural, serverWords } from './labels.js';
import { dateWords, hoursWords } from './words.js';

// --- Lines grouped by booking type  --------------------------------

// The organization's order of types: as the member's choices list them, each
// name once, asset by asset.
export function typeOrder(choices) {
  const names = [];
  for (const asset of choices?.assets || []) {
    for (const type of asset.types || []) {
      if (!names.includes(type.name)) names.push(type.name);
    }
  }
  return names;
}

// The cart sends the type as {id, name}, the order read as its name.
export const typeName = (line) => (typeof line.type === 'string' ? line.type : line.type?.name ?? '');
export const categoryName = (line) => (typeof line.category === 'string' ? line.category : line.category?.name ?? '');
export const assetName = (line) => (typeof line.asset === 'string' ? line.asset : line.asset?.name ?? '');

// The places a line holds: the cart's `quantity`, the order read's `places`.
export const placesOf = (line) => line.quantity ?? line.places ?? 1;

// The word for a line's places: its floor plan's ("seat", "bed"), as the server
// sent it; the label's only for a line without a floor plan.
export const spotWordOf = (line) => line.spot_word || label('spot.word');

// Whether the lines are on more than one asset, so each line names its own.
export const manyAssets = (groups) => new Set(groups.flatMap((group) => group.lines.map(assetName))).size > 1;

// A night is a line that ends on a later date than it starts.
export const isNight = (line) => line.end.slice(0, 10) > line.start.slice(0, 10);

const byStart = (a, b) => Date.parse(a.start) - Date.parse(b.start) || Date.parse(a.end) - Date.parse(b.end);

// One heading per type in `order` (a type not in it goes last, by name), with
// how many lines and their sum; inside it the lines in date order. A line the
// server could not price counts nothing.
export function groupLines(lines, order = []) {
  const found = new Map();
  for (const line of lines) {
    const name = typeName(line);
    if (!found.has(name)) found.set(name, []);
    found.get(name).push(line);
  }
  const rank = (name) => { const at = order.indexOf(name); return at < 0 ? order.length : at; };
  return [...found.entries()]
    .sort(([a], [b]) => rank(a) - rank(b) || (a < b ? -1 : a > b ? 1 : 0))
    .map(([type, rows]) => {
      const sorted = [...rows].sort(byStart);
      return {
        type,
        night: isNight(sorted[0]),
        count: sorted.length,
        sum: sorted.reduce((total, line) => total + (line.total ?? 0), 0),
        lines: sorted,
      };
    });
}

// What one line shows: its date, its time, what it is booked as, its places
// when more than one, its asset when the lines are on several (`withAsset`), and
// its price ("No charge" for none). In the cart (`inCart`) `marked` is the words for
// the line's state key (the label, else the server's own text) when it is not
// fine; an order's lines carry their booking's state, not a mark.
export function lineView(line, currency, inCart = false, withAsset = false) {
  const marked = inCart && line.state && line.state !== 'ok';
  const places = placesOf(line);
  return {
    key: line.id,
    date: dateWords(line.start.slice(0, 10)),
    time: hoursWords(line.start, line.end),
    as: categoryName(line),
    bookedAs: label('line.booking-as', { category: categoryName(line) }),
    places: places > 1 ? plural('line.places', places, { word: spotWordOf(line) }) : null,
    asset: withAsset ? assetName(line) : null,
    price: line.total ? moneyWords(line.total, currency) : (line.total === null ? '' : label('price.free')),
    marked: marked ? (serverWords(line.state, line.text) || line.state) : null,
  };
}

// With one line in all, its price is shown on the line and the pay button
// only: no group sum and no Total row. Two lines or more: sums and Total.
export const onlyLine = (groups) => groups.reduce((count, group) => count + group.lines.length, 0) === 1;

// Vipps is named above the pay button when there is something to pay.
export const paidWithVipps = (total) => Boolean(total);

export const sumWords = (sum, currency) => (sum ? moneyWords(sum, currency) : label('price.free'));

// --- The cart page -----------------------------------------------------------

// The cart as the page holds it: its lines, whether an order is held (the
// cart is locked and the payment page shows), and the lines to show: the
// held order's own bookings, priced, as the cart's rows are not while their
// order holds them.
export function cartView(cart) {
  const held = cart?.order && cart.order.state === 'held' ? cart.order : null;
  return {
    held,
    lines: cart?.bookings || [],
    shown: held ? held.bookings || [] : cart?.bookings || [],
    total: cart?.total ?? 0,
    ready: Boolean(cart?.ready),
    // The identifier of the booking conditions in force, which the order carries
    // as `accepted_version` ; null when the cart did not say.
    version: cart?.conditions?.version ?? null,
    marked: (cart?.bookings || []).filter((line) => line.state && line.state !== 'ok'),
  };
}

// Whether Pay is enabled : the cart is ready and the box is ticked. The
// box only enables Pay; the server is the gate.
export const canPay = (view, accepted) => Boolean(view.ready && view.version && accepted);

export const payWords = (total, currency) => (total
  ? label('cart.pay', { amount: moneyWords(total, currency) })
  : label('cart.confirm'));

// Seconds left of the hold at `now` (ms); never below zero. Null without one.
export function secondsLeft(until, now) {
  if (!until) return null;
  return Math.max(0, Math.floor((Date.parse(until) - now) / 1000));
}

export const clockWords = (seconds) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

// Functions, not constants: the label set is read when the page starts.
export const paymentUnavailable = () => label('cart.payment-unavailable');
export const releasedWords = () => label('cart.released');

// What the page does with the answer to the order, or to the payment start:
//   done      confirmed at once: go to the outcome page of `order`
//   vipps     go to `address`
//   retry     the order is held, payment unavailable: offer to try again
//   refused   a refusal: read the cart again; `text` is the server's
//   signed-out, forbidden, offline: as the other pages
export function afterOrder(result) {
  if (result.ok) {
    const { order, confirmed, redirect } = result.data;
    if (confirmed) return { kind: 'done', order };
    if (redirect === '/') return { kind: 'done', order };
    if (redirect) return { kind: 'vipps', address: redirect, order };
    return { kind: 'error', order };
  }
  if (result.kind === 'refused') {
    if (result.code === 'payment-unavailable') return { kind: 'retry' };
    return { kind: 'refused', text: result.text || label('cart.could-not-order'), code: result.code };
  }
  return { kind: result.kind };
}

// The answer to starting a held order's payment again (`/payments/start/`):
// Vipps; try again when payment is unavailable (409, 503) or there was no
// answer; any other refusal (the order is gone, or no longer held) only reads
// the cart again (`gone`).
export function afterPaymentStart(result, order) {
  if (result.ok) return afterOrder({ ok: true, data: { order, redirect: result.data.redirect } });
  if (result.kind === 'signed-out' || result.kind === 'forbidden') return { kind: result.kind };
  if (result.kind === 'refused' && ![409, 503].includes(result.status)) return { kind: 'gone' };
  return { kind: 'retry' };
}

// The answer to letting go of the held order (`/booking/order/<id>/cancel/`):
//   cart      let go: the cart, its times back in it
//   paid      it was paid first: go to its outcome page
//   gone      no such order any more: read the cart again
//   refused   the server's text (the payment is being checked)
//   signed-out, forbidden, offline: as the other pages
export function afterCancel(result) {
  if (result.ok) return { kind: 'cart', cart: result.data };
  if (result.kind === 'refused') {
    if (result.code === 'confirmed') return { kind: 'paid' };
    if (result.status === 404) return { kind: 'gone' };
    return { kind: 'refused', text: result.text || label('cart.could-not-cancel') };
  }
  return { kind: result.kind };
}

// After a change to the cart: every kept calendar is old.
export async function removeLine({ csrf, id, remove, store }) {
  const result = await remove(csrf, id);
  if (result.ok) {
    store.clear();
    return { kind: 'removed', cart: result.data };
  }
  if (result.kind === 'refused') return { kind: 'refused', text: result.text || label('cart.could-not-remove') };
  return { kind: result.kind };
}

// --- The outcome page --------------------------------------------------------

export const READ_AGAIN = 5;
export const READ_EVERY = 2000;

// The page for an order read: confirmed, waiting (held or being checked),
// cancelled (confirmed, then every booking cancelled), or not paid (expired or
// never completed). `again` is whether the page
// reads once more by itself: only while the payment is being checked, and only
// `READ_AGAIN` times.
export function outcomeOf(order, reads = 0) {
  if (order.state === 'confirmed') return { kind: 'confirmed', again: false };
  if (order.state === 'settling') return { kind: 'waiting', again: reads < READ_AGAIN };
  if (order.state === 'held') return { kind: 'waiting', again: false };
  if (order.state === 'cancelled') return { kind: 'cancelled', again: false };
  return { kind: 'not-paid', again: false };
}

// The result of one read of the order as the page needs it.
export function readOutcome(result) {
  if (result.ok) return { kind: 'order', order: result.data };
  if (result.kind === 'refused' && result.status === 404) return { kind: 'not-found' };
  return { kind: result.kind };
}

export const paidWords = (order) => (order.total
  ? label('outcome.paid', { amount: moneyWords(order.total, order.currency) })
  : label('outcome.free'));

// "Add to calendar" on a confirmed order (): the calendar file of its
// confirmed bookings, then each confirmed booking in Google Calendar, named by
// its date and time when there are several. Nothing for an order without one.
export const GOOGLE_CALENDAR = 'https://calendar.google.com/calendar/render?';

export function calendarChoices(order) {
  const google = (order.bookings || []).filter((line) => typeof line.google === 'string' && line.google.startsWith(GOOGLE_CALENDAR));
  if (!google.length) return [];
  const when = (line) => `${dateWords(line.start.slice(0, 10))} ${hoursWords(line.start, line.end)}`;
  // The shortest naming that tells every item apart: by time, then with the
  // asset, then with the type too.
  const namings = [
    (line) => label('outcome.google-calendar-at', { when: when(line) }),
    (line) => label('outcome.google-calendar-asset-at', { asset: assetName(line), when: when(line) }),
    (line) => label('outcome.google-calendar-type-at', { type: typeName(line), asset: assetName(line), when: when(line) }),
  ];
  const named = google.length === 1
    ? () => label('outcome.google-calendar')
    : namings.find((naming) => new Set(google.map(naming)).size === google.length) || namings[namings.length - 1];
  return [
    { key: 'file', href: `/booking/order/${encodeURIComponent(order.id)}/calendar/`, text: label('outcome.calendar-file'), file: true },
    ...google.map((line, n) => ({ key: `google-${n}`, href: line.google, text: named(line), file: false })),
  ];
}
