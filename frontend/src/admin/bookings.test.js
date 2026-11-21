// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  IMPORTS_ADDRESS, bookingAddress, bookingsQuery, cancelBody, cancelFailure, cancelPlan, cancelTargets, figure, importAddress, importsRoute,
  memberAddress, PAYMENT_COLORS, paymentsQuery, placeRoute, priceWords, refundShown, routeOf, scheduleQuery, startOrder, targetsAmount,
} from './bookings.js';
import { GROUPS, hrefOf } from './places.js';

const open = { when: 'all', from: '2026-03-01', to: '2026-03-07', state: 'all', q: '', order: 'number-desc', page: 0 };

test('no filter asks for the newest number first, page 1', () => {
  assert.deepEqual(bookingsQuery(open), {
    state: undefined, q: undefined, sort: 'number', order: 'newest', page: 1,
  });
});

test('created: one day is both ends, a period is sorted, any date sends none', () => {
  const day = bookingsQuery({ ...open, when: 'day' });
  assert.deepEqual([day.from, day.to], ['2026-03-01', '2026-03-01']);
  const back = bookingsQuery({ ...open, when: 'period', from: '2026-03-09', to: '2026-03-02' });
  assert.deepEqual([back.from, back.to], ['2026-03-02', '2026-03-09']);
  assert.equal('from' in bookingsQuery(open), false);
});

test('state, member search and the four orders are sent as the server reads them', () => {
  const query = bookingsQuery({ ...open, state: 'held', q: '  Alex ', order: 'created-asc', page: 2 });
  assert.deepEqual(query, { state: 'held', q: 'Alex', sort: 'created', order: 'oldest', page: 3 });
  assert.deepEqual(bookingsQuery({ ...open, order: 'number-asc' }).sort, 'number');
  assert.equal(bookingsQuery({ ...open, q: '   ' }).q, undefined);
});

test('the Schedule: Past starts newest first, the others earliest first', () => {
  assert.equal(startOrder('past'), 'desc');
  for (const when of ['today', 'week', 'coming', 'day', 'period']) assert.equal(startOrder(when), 'asc');
});

test('the Schedule sends its period, the dates only for a day or a period, in order', () => {
  const base = { when: 'week', from: '2026-03-09', to: '2026-03-02', q: '', dir: 'asc', page: 0 };
  assert.deepEqual(scheduleQuery(base), { when: 'week', q: undefined, order: 'earliest', page: 1 });
  assert.deepEqual(scheduleQuery({ ...base, when: 'day' }), {
    when: 'day', from: '2026-03-09', q: undefined, order: 'earliest', page: 1,
  });
  const period = scheduleQuery({ ...base, when: 'period', dir: 'desc', q: ' Lake ', page: 4 });
  assert.deepEqual(period, {
    when: 'period', from: '2026-03-02', to: '2026-03-09', q: 'Lake', order: 'latest', page: 5,
  });
});

test('amounts are in the smallest unit, whole when they are whole', () => {
  assert.equal(figure(0), '0');
  assert.equal(figure(25000), '250');
  assert.equal(figure(125000), '1 250');
  assert.equal(figure(12550), '125,50');
  assert.equal(figure(5), '0,05');
});

test('an address is a place, or a booking opened from the Bookings, the Schedule or a member', () => {
  assert.equal(bookingAddress(1042), '/admin/bookings/1042/');
  assert.equal(memberAddress(7), '/admin/members/7/');
  const at = (place, booking = null, member = null) => ({ place, booking, member });
  assert.deepEqual(routeOf('/admin/schedule/', null), at('schedule'));
  assert.deepEqual(routeOf('/admin/audit/', { from: 'schedule' }), at('audit'));
  assert.deepEqual(routeOf('/admin/bookings/1042/', null), at('bookings', 1042));
  assert.deepEqual(routeOf('/admin/bookings/1042/', { from: 'schedule' }), at('schedule', 1042));
  assert.deepEqual(routeOf('/admin/bookings/1042/', { from: 'payments' }), at('payments', 1042));
  assert.deepEqual(routeOf('/admin/bookings/1042/', { from: 'blocks' }), at('blocks', 1042));
  assert.deepEqual(routeOf('/admin/bookings/1042/', { from: 'members' }), at('members', 1042));
  assert.deepEqual(routeOf('/admin/bookings/1042/', { from: 'audit' }), at('audit', 1042));
  assert.deepEqual(routeOf('/admin/bookings/1042/', { from: 'containers' }), at('containers', 1042));
  assert.deepEqual(routeOf('/admin/bookings/1042/', { from: 'roles' }), at('bookings', 1042));
  assert.deepEqual(routeOf('/admin/bookings/', null), at('bookings'));
  assert.deepEqual(routeOf('/admin/members/', null), at('members'));
  assert.deepEqual(routeOf('/admin/members/7/', null), at('members', null, 7));
  assert.deepEqual(routeOf('/admin/members/7/', { from: 'schedule' }), at('members', null, 7));
});

test('the imports tab and a run are the members place with a tab or a run', () => {
  assert.equal(IMPORTS_ADDRESS, '/admin/members/imports/');
  assert.equal(importAddress(12), '/admin/members/imports/12/');
  assert.deepEqual(routeOf('/admin/members/imports/', null), importsRoute());
  assert.deepEqual(routeOf('/admin/members/imports/12/', { from: 'members' }), importsRoute(12));
  assert.deepEqual(importsRoute(), { place: 'members', booking: null, member: null, imports: true, run: null });
  // the members' own list and a member keep the plain three fields
  assert.deepEqual(routeOf('/admin/members/', null), placeRoute('members'));
  assert.deepEqual(routeOf('/admin/members/imports/x/', null), placeRoute('bookings'));
});

test('a place of the menu is its address and back: pushState, then popstate', () => {
  for (const [, places] of GROUPS) {
    for (const [key] of places) {
      assert.deepEqual(placeRoute(key), { place: key, booking: null, member: null });
      // what the click pushes (state {}) and what popstate reads from it
      assert.deepEqual(routeOf(hrefOf(key), {}), placeRoute(key));
      assert.deepEqual(routeOf(hrefOf(key), null), placeRoute(key));
    }
  }
  // back from a place to the booking opened before it: its entry still says where from
  assert.deepEqual(routeOf('/admin/bookings/12/', { from: 'payments', listed: true }), { place: 'payments', booking: 12, member: null });
});

const unfiltered = { when: 'all', from: '2026-03-01', to: '2026-03-07', state: 'all', q: '', dir: 'desc', page: 0 };

test('payments: no filter asks for the newest first, page 1', () => {
  assert.deepEqual(paymentsQuery(unfiltered), { state: undefined, q: undefined, order: 'newest', page: 1 });
});

test('payments: one day, a period the wrong way round, a state, a trimmed search, oldest first, page 3', () => {
  assert.deepEqual(paymentsQuery({ ...unfiltered, when: 'day' }), {
    from: '2026-03-01', to: '2026-03-01', state: undefined, q: undefined, order: 'newest', page: 1,
  });
  assert.deepEqual(paymentsQuery({ ...unfiltered, when: 'period', from: '2026-03-09', to: '2026-03-02' }), {
    from: '2026-03-02', to: '2026-03-09', state: undefined, q: undefined, order: 'newest', page: 1,
  });
  assert.deepEqual(paymentsQuery({ ...unfiltered, state: 'refunded', q: '  vp-7 ', dir: 'asc', page: 2 }), {
    state: 'refunded', q: 'vp-7', order: 'oldest', page: 3,
  });
});

test('a price line says how many times, at what, for how many places and for which group', () => {
  assert.equal(priceWords({ times: 2, amount: 10000, places: 1, unit: 'booking', group: null }), '2 × 100');
  assert.equal(priceWords({ times: 1, amount: 10000, places: 3, unit: 'places', group: null }), '1 × 100 × 3 places');
  assert.equal(priceWords({ times: 1, amount: 10050, places: 1, unit: 'booking', group: 'Stage end' }), '1 × 100,50 · Stage end');
});

// --- Cancelling ---------------------------------------------------------------

const line = (id, extra = {}) => ({ id, state: 'paid', coming: true, amount: 20000, ...extra });
const paidBooking = (lines, extra = {}) => ({
  state: 'paid', lines, payment: { captured: 40000 }, ...extra,
});

test('the booking is offered while a line is to come, a line when more than one is', () => {
  const two = paidBooking([line(1), line(2)]);
  assert.deepEqual([cancelPlan(two).whole, cancelPlan(two).line], [true, true]);
  const one = paidBooking([line(1), line(2, { coming: false, state: 'cancelled' })]);
  assert.deepEqual([cancelPlan(one).whole, cancelPlan(one).line], [true, false]);
  const none = paidBooking([line(1, { coming: false })]);
  assert.deepEqual([cancelPlan(none).whole, cancelPlan(none).line], [false, false]);
});

test('a booking being paid is cancelled whole: no line, and all its lines are taken', () => {
  const held = paidBooking([line(1, { state: 'held' }), line(2, { state: 'held', coming: false })], { state: 'held', payment: null });
  assert.equal(cancelPlan(held).line, false);
  assert.deepEqual(cancelTargets(held, null).map((l) => l.id), [1, 2]);
});

test('the targets are the line asked for or the lines to come', () => {
  const booking = paidBooking([line(1), line(2, { coming: false }), line(3)]);
  assert.deepEqual(cancelTargets(booking, null).map((l) => l.id), [1, 3]);
  assert.deepEqual(cancelTargets(booking, booking.lines[2]).map((l) => l.id), [3]);
  assert.equal(targetsAmount(cancelTargets(booking, null)), 40000);
});

test('the refund switch shows only for money paid and an amount above 0', () => {
  const booking = paidBooking([line(1), line(2, { amount: 0 })]);
  assert.equal(refundShown(booking, [booking.lines[0]]), true);
  assert.equal(refundShown(booking, [booking.lines[1]]), false);
  assert.equal(refundShown({ ...booking, state: 'held' }, [booking.lines[0]]), false);
  assert.equal(refundShown({ ...booking, payment: null }, [booking.lines[0]]), false);
  assert.equal(refundShown({ ...booking, payment: { captured: 0 } }, [booking.lines[0]]), false);
});

test('the body sends the key, the line and the refund only when the switch was shown', () => {
  const key = 'a-request-key';
  assert.deepEqual(cancelBody({ key, line: { id: 7 }, refund: true, shown: true }), { key, line: 7, refund: true });
  assert.deepEqual(cancelBody({ key, line: null, refund: true, shown: false }), { key, line: null, refund: false });
  assert.deepEqual(cancelBody({ key, line: null, refund: false, shown: true }), { key, line: null, refund: false });
});

test('every refusal of the server is worded, and the rest read the same', () => {
  for (const code of ['busy', 'already-cancelled', 'past', 'not-cancellable', 'whole-only', 'recovery', 'payment-unavailable', 'no-refund-left', 'not-found']) {
    assert.match(cancelFailure({ kind: 'refused', code }), /\w/);
    assert.notEqual(cancelFailure({ kind: 'refused', code }), cancelFailure({ kind: 'refused', code: 'unavailable' }));
  }
  assert.equal(cancelFailure({ kind: 'refused', code: 'unavailable' }), cancelFailure({ kind: 'refused', code: null }));
  assert.match(cancelFailure({ kind: 'offline' }), /could not be reached/);
  assert.match(cancelFailure({ kind: 'forbidden' }), /out of date/);
});

test('a payment that needs attention is a warning, instead of its own state in the list', () => {
  assert.equal(PAYMENT_COLORS.attention, 'warning');
  assert.deepEqual(paymentsQuery({ ...unfiltered, state: 'attention' }), {
    state: 'attention', q: undefined, order: 'newest', page: 1,
  });
});

test('the payouts tab and a payout are the payments place with a tab or a payout', async () => {
  const { PAYOUTS_ADDRESS, payoutAddress, payoutsRoute } = await import('./bookings.js');
  assert.equal(PAYOUTS_ADDRESS, '/admin/payments/payouts/');
  assert.equal(payoutAddress(12), '/admin/payments/payouts/12/');
  assert.deepEqual(routeOf('/admin/payments/payouts/', null), payoutsRoute());
  assert.deepEqual(routeOf('/admin/payments/payouts/12/', { from: 'payments' }), payoutsRoute(12));
  assert.deepEqual(payoutsRoute(), { place: 'payments', booking: null, member: null, payouts: true, payout: null });
  assert.deepEqual(routeOf('/admin/payments/payouts/x/', null), placeRoute('bookings'));
});

test('an event\'s address is the Events place', () => {
  assert.deepEqual(routeOf('/admin/events/12/', null), { place: 'events', booking: null, member: null });
  assert.deepEqual(routeOf('/admin/events/', null), { place: 'events', booking: null, member: null });
  assert.equal(routeOf('/admin/bookings/5/', { from: 'events' }).place, 'events');
});

test('an event\'s address is the Events place', () => {
  assert.deepEqual(routeOf('/admin/events/12/', null), { place: 'events', booking: null, member: null });
  assert.deepEqual(routeOf('/admin/events/', null), { place: 'events', booking: null, member: null });
  assert.equal(routeOf('/admin/bookings/5/', { from: 'events' }).place, 'events');
});

test('freeUntilWords: the moment free cancelling ended, as data; none for an order from before the conditions', async () => {
  const { freeUntilWords } = await import('./bookings.js');
  assert.equal(freeUntilWords({ free_until: { date: '2026-11-01', time: '10:00' } }), 'Free until 2026-11-01 10:00');
  assert.equal(freeUntilWords({ free_until: null }), null);
  assert.equal(freeUntilWords({}), null);
});
