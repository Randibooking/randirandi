// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { bookingStep, importsStep, memberStep, payoutStep, payoutsStep, placeStep, plainClick, runStep } from './route.js';
import { importsRoute, payoutsRoute, placeRoute, routeOf } from './bookings.js';

// A history as the browser keeps it: push adds an entry, back and forward move,
// and each move reads the route of the entry it lands on, as popstate does.
function browser(start) {
  const entries = [{ address: start, state: null }];
  let at = 0;
  const visit = () => routeOf(entries[at].address, entries[at].state);
  return {
    push: (step) => { entries.splice(at + 1); entries.push({ address: step.address, state: step.state }); at += 1; return step.route; },
    back: () => { at -= 1; return visit(); },
    forward: () => { at += 1; return visit(); },
    address: () => entries[at].address,
  };
}

test('a place of the menu pushes its address and shows its list; its own list does nothing', () => {
  const step = placeStep(placeRoute('bookings'), 'members');
  assert.deepEqual(step.route, placeRoute('members'));
  assert.equal(step.address, '/admin/members/');
  assert.equal(placeStep(placeRoute('members'), 'members'), null);
  // on a booking of that place the click goes to its list
  assert.notEqual(placeStep({ place: 'members', booking: 5, member: null }, 'members'), null);
});

test('back and forward across places, a booking and a member', () => {
  const b = browser('/admin/bookings/');
  assert.deepEqual(b.push(placeStep(placeRoute('bookings'), 'payments')), placeRoute('payments'));
  assert.deepEqual(b.push(bookingStep(12, 'payments')), { place: 'payments', booking: 12, member: null });
  assert.deepEqual(b.push(placeStep({ place: 'payments', booking: 12, member: null }, 'members')), placeRoute('members'));
  assert.deepEqual(b.push(memberStep(7)), { place: 'members', booking: null, member: 7 });
  assert.deepEqual(b.back(), placeRoute('members'));
  assert.deepEqual(b.back(), { place: 'payments', booking: 12, member: null });
  assert.deepEqual(b.back(), placeRoute('payments'));
  assert.deepEqual(b.back(), placeRoute('bookings'));
  assert.deepEqual(b.forward(), placeRoute('payments'));
  assert.deepEqual(b.forward(), { place: 'payments', booking: 12, member: null });
  assert.equal(b.address(), '/admin/bookings/12/');
});

test('a booking opened on its own address, then a place, comes back to that booking', () => {
  const b = browser('/admin/bookings/3/');
  b.push(placeStep({ place: 'bookings', booking: 3, member: null }, 'schedule'));
  assert.deepEqual(b.back(), { place: 'bookings', booking: 3, member: null });
});

test('a plain click is ours; a modifier or another button is left to the browser', () => {
  const click = (extra = {}) => ({ defaultPrevented: false, button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...extra });
  assert.equal(plainClick(click()), true);
  for (const extra of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }, { defaultPrevented: true }]) {
    assert.equal(plainClick(click(extra)), false);
  }
});

test('the Members place has two tabs, each its own address, and a run opens over the imports', () => {
  const b = browser('/admin/members/');
  assert.equal(importsStep(placeRoute('members')).address, '/admin/members/imports/');
  assert.deepEqual(b.push(importsStep(placeRoute('members'))), importsRoute());
  // the tab already shown does nothing; the menu's Members leads back to the members
  assert.equal(importsStep(importsRoute()), null);
  assert.deepEqual(placeStep(importsRoute(), 'members').route, placeRoute('members'));
  assert.equal(placeStep(importsRoute(), 'members').address, '/admin/members/');
  assert.deepEqual(b.push(runStep(4)), importsRoute(4));
  assert.equal(b.address(), '/admin/members/imports/4/');
  // a member and a booking opened from the run, and back
  assert.deepEqual(b.push(memberStep(9)), { place: 'members', booking: null, member: 9 });
  assert.deepEqual(b.back(), importsRoute(4));
  assert.deepEqual(b.back(), importsRoute());
  assert.deepEqual(b.back(), placeRoute('members'));
  assert.deepEqual(b.forward(), importsRoute());
  assert.deepEqual(b.forward(), importsRoute(4));
  // from a run the Imports tab is its list, and the run's own menu entry its list
  assert.deepEqual(importsStep(importsRoute(4)).route, importsRoute());
  assert.notEqual(placeStep(importsRoute(4), 'members'), null);
});

test('the Payments place has two tabs, and a payout opens over the payouts and leads to its booking', () => {
  const b = browser('/admin/payments/');
  assert.equal(payoutsStep(placeRoute('payments')).address, '/admin/payments/payouts/');
  assert.deepEqual(b.push(payoutsStep(placeRoute('payments'))), payoutsRoute());
  assert.equal(payoutsStep(payoutsRoute()), null);
  assert.deepEqual(placeStep(payoutsRoute(), 'payments').route, placeRoute('payments'));
  assert.equal(placeStep(payoutsRoute(), 'payments').address, '/admin/payments/');
  assert.deepEqual(b.push(payoutStep(7)), payoutsRoute(7));
  assert.equal(b.address(), '/admin/payments/payouts/7/');
  assert.deepEqual(b.push(bookingStep(3, 'payments')), { place: 'payments', booking: 3, member: null });
  assert.deepEqual(b.back(), payoutsRoute(7));
  assert.deepEqual(b.back(), payoutsRoute());
  assert.deepEqual(b.back(), placeRoute('payments'));
  assert.deepEqual(payoutsStep(payoutsRoute(7)).route, payoutsRoute());
  assert.notEqual(placeStep(payoutsRoute(7), 'payments'), null);
});
