// SPDX-License-Identifier: AGPL-3.0-only
// The Bookings, the Schedule, the Payments and a booking's page: their pure logic, no DOM.
// (The Members' is members.js.)
// The filters as the server's query (randirandi/core/admin_views.py), the
// amounts as the pages write them, and which place an address belongs to.

export { addDays, todayIn } from './audit.js';

export const BOOKING_WHEN = [['all', 'Any date'], ['day', 'One day'], ['period', 'A period']];
export const BOOKING_ORDER = [
  ['number-desc', 'Booking, newest first'], ['number-asc', 'Booking, oldest first'],
  ['created-desc', 'Created, newest first'], ['created-asc', 'Created, oldest first'],
];
export const SCHEDULE_WHEN = [
  ['today', 'Today'], ['week', 'Next 7 days'], ['coming', 'Coming'], ['past', 'Past'],
  ['day', 'One day'], ['period', 'A period'],
];
export const TIME_HEADS = ['Start date', 'Start time', 'End date', 'End time'];

// A period given the wrong way round reads the same either way.
const ordered = (from, to) => (from <= to ? [from, to] : [to, from]);

// The "Created" filter's dates: one day is both ends the same.
function createdDates(when, from, to) {
  const [first, last] = ordered(from, to);
  return when === 'day' ? { from, to: from } : when === 'period' ? { from: first, to: last } : {};
}

// The Bookings' query: `order` is "number-desc", "created-asc" and so on;
// `page` counts from 0 here, from 1 there.
export function bookingsQuery({ when, from, to, state, q, order, page }) {
  const [sort, dir] = order.split('-');
  return {
    ...createdDates(when, from, to),
    state: state === 'all' ? undefined : state,
    q: q.trim() || undefined,
    sort,
    order: dir === 'asc' ? 'oldest' : 'newest',
    page: page + 1,
  };
}

// The Payments' query: `dir` is "desc" (newest first) or "asc".
export function paymentsQuery({ when, from, to, state, q, dir, page }) {
  return {
    ...createdDates(when, from, to),
    state: state === 'all' ? undefined : state,
    q: q.trim() || undefined,
    order: dir === 'asc' ? 'oldest' : 'newest',
    page: page + 1,
  };
}

// The order a "Booked for" choice starts in: Past reads newest first.
export const startOrder = (when) => (when === 'past' ? 'desc' : 'asc');

// The Schedule's query: `dir` is "asc" (earliest first) or "desc".
export function scheduleQuery({ when, from, to, q, dir, page }) {
  const [first, last] = ordered(from, to);
  const dates = when === 'day' ? { from } : when === 'period' ? { from: first, to: last } : {};
  return {
    when,
    ...dates,
    q: q.trim() || undefined,
    order: dir === 'asc' ? 'earliest' : 'latest',
    page: page + 1,
  };
}

// An amount in the currency's smallest unit as a number: 125000 is "1 250",
// 12550 is "125,50" (nb-NO, plain spaces). The currency stands beside it.
export function figure(amount) {
  const value = amount / 100;
  const places = Number.isInteger(value) ? 0 : 2;
  return new Intl.NumberFormat('nb-NO', { minimumFractionDigits: places, maximumFractionDigits: places })
    .format(value).replace(/[\u00a0\u202f]/g, ' ');
}

export const bookingAddress = (number) => `/admin/bookings/${number}/`;
export const memberAddress = (id) => `/admin/members/${id}/`;
// The Members place's Imports tab, and one run (part 7.93).
export const IMPORTS_ADDRESS = '/admin/members/imports/';
export const importAddress = (number) => `/admin/members/imports/${number}/`;
// The Payments place's Payouts tab, and one payout (part 5.1).
export const PAYOUTS_ADDRESS = '/admin/payments/payouts/';
export const payoutAddress = (id) => `/admin/payments/payouts/${id}/`;

// The place, the booking and the member an address is. A booking opened from the
// Schedule, the Payments, the Blocks, a container's page or a member's page keeps its place in the
// history entry (`state.from`); an address without one is opened from the
// Bookings.
export const OPENS_BOOKINGS = ['schedule', 'payments', 'blocks', 'events', 'members', 'containers', 'audit'];
export function routeOf(pathname, state) {
  const booking = /^\/admin\/bookings\/([0-9]+)\/$/.exec(pathname);
  if (booking) {
    return { place: OPENS_BOOKINGS.includes(state?.from) ? state.from : 'bookings', booking: Number(booking[1]), member: null };
  }
  const member = /^\/admin\/members\/([0-9]+)\/$/.exec(pathname);
  if (member) return { place: 'members', booking: null, member: Number(member[1]) };
  if (pathname === IMPORTS_ADDRESS) return importsRoute();
  const run = /^\/admin\/members\/imports\/([0-9]+)\/$/.exec(pathname);
  if (run) return importsRoute(Number(run[1]));
  if (pathname === PAYOUTS_ADDRESS) return payoutsRoute();
  const payout = /^\/admin\/payments\/payouts\/([0-9]+)\/$/.exec(pathname);
  if (payout) return payoutsRoute(Number(payout[1]));
  if (/^\/admin\/events\/[0-9]+\/$/.test(pathname)) return { place: 'events', booking: null, member: null };
  const place = /^\/admin\/([a-z]+)\/$/.exec(pathname);
  return { place: place ? place[1] : 'bookings', booking: null, member: null };
}

// The Imports tab of the Members, or one run of it: the route has `imports`,
// and `run` (a number) for a run's page. A route of another place leaves both out.
export const importsRoute = (run = null) => ({ place: 'members', booking: null, member: null, imports: true, run });

// The Payouts tab of the Payments, or one payout of it: the route has `payouts`,
// and `payout` (a number) for a payout's page.
export const payoutsRoute = (payout = null) => ({ place: 'payments', booking: null, member: null, payouts: true, payout });

// The route of a place's own list (a click in the menu): no booking, no member.
// Its address is `hrefOf(place)` (places.js), and `routeOf` reads it back.
export const placeRoute = (place) => ({ place, booking: null, member: null });

// The state colours of the chips; the words come from the server.
export const STATE_COLORS = { paid: 'success', held: 'warning', cancelled: 'default', expired: 'default' };
export const PAYMENT_COLORS = {
  created: 'warning', authorized: 'warning', captured: 'success', cancelled: 'default', expired: 'default', refunded: 'info', attention: 'warning',
};

// What a price line says of itself: "2 × 100", "1 × 100 × 3 places", with
// its group of spots after a dot.
export function priceWords(price) {
  const parts = [`${price.times} × ${figure(price.amount)}`];
  if (price.places > 1) parts.push(`${price.places} ${price.unit}`);
  return parts.join(' × ') + (price.group ? ` · ${price.group}` : '');
}

// --- Cancelling a booking or a line (part 7.7) --------------------------------
// The server decides (payment_adjustments.cancel); these only decide what the
// page offers, from the answer for the booking: a line is `coming` when it is
// paid or being paid and not over.

// What the page offers: the booking while a line is to come; one line when
// more than one is, and the booking is not being paid (that is cancelled whole).
export function cancelPlan(booking) {
  const coming = booking.lines.filter((line) => line.coming);
  return { coming, whole: coming.length > 0, line: coming.length > 1 && booking.state !== 'held' };
}

// The lines a cancel takes: the one asked for, or the booking's: the lines to
// come, or all the lines being paid (a booking being paid is cancelled whole).
export function cancelTargets(booking, line) {
  if (line) return [line];
  return booking.state === 'held' ? booking.lines.filter((l) => l.state === 'held') : cancelPlan(booking).coming;
}

// The moment free cancelling of a line ended , as data under its times:
// "Free until 2026-11-01 10:00"; none for a line whose order accepted no
// conditions .
export const freeUntilWords = (line) => (line.free_until ? `Free until ${line.free_until.date} ${line.free_until.time}` : null);

export const targetsAmount = (targets) => targets.reduce((sum, line) => sum + line.amount, 0);

// The switch shows only when the money was paid and the amount is more than 0.
export const refundShown = (booking, targets) =>
  booking.state !== 'held' && booking.payment !== null && booking.payment.captured > 0 && targetsAmount(targets) > 0;

// The body of the call. A switch that is not shown sends no refund.
export const cancelBody = ({ key, line, refund, shown }) => ({ key, line: line ? line.id : null, refund: shown && refund });

export const CANCEL_WORDS = {
  busy: 'Another change to this booking is being made. Try again in a moment.',
  'already-cancelled': 'It is already cancelled.',
  past: 'The time is over, so it cannot be cancelled.',
  'not-cancellable': 'It can no longer be cancelled.',
  'whole-only': 'A booking being paid is cancelled whole.',
  recovery: 'The payment needs checking before this can be cancelled. Try again later.',
  'payment-unavailable': 'The payment could not be checked. Nothing was cancelled.',
  'no-refund-left': 'There is no money left to refund. Cancel without the refund.',
  'not-found': 'The booking or the line was not found.',
};
const FALLBACK = 'The booking could not be cancelled.';

// What a failed call says, in plain words.
export function cancelFailure(result) {
  if (result.kind === 'offline') return 'The server could not be reached. Nothing was cancelled.';
  if (result.kind === 'forbidden') return 'This page is out of date. Reload it and try again.';
  return CANCEL_WORDS[result.code] ?? FALLBACK;
}
