// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
import { crumbItems, crumbTrail } from './crumbs.js';

test('a place with nothing open is one item, the page shown', () => {
  assert.deepEqual(crumbItems('Containers', []), [{ label: 'Containers', index: 0, last: true }]);
});

test('the place and each open page link back to themselves, the last is shown', () => {
  assert.deepEqual(crumbItems('Assets and availability', ['Harbour Room', 'Availability']), [
    { label: 'Assets and availability', index: 0, last: false },
    { label: 'Harbour Room', index: 1, last: false },
    { label: 'Availability', index: 2, last: true },
  ]);
});

const clicks = (items) => items.map((i) => [i.label, i.last, i.action.type, i.action.index]);

test('on a list the place and each open page go back to themselves', () => {
  const items = crumbTrail({ onList: true, title: 'Assets', labels: ['Harbour', 'Availability'], booking: false, run: false });
  assert.deepEqual(clicks(items), [
    ['Assets', false, 'crumb', 0], ['Harbour', false, 'crumb', 1], ['Availability', true, 'crumb', 2],
  ]);
  assert.deepEqual(clicks(crumbTrail({ onList: true, title: 'Bookings', labels: [] })), [['Bookings', true, 'crumb', 0]]);
});

test('a booking opened from a place with a page open keeps the page: place picks, pages close', () => {
  const items = crumbTrail({ onList: false, title: 'Containers', labels: ['Container 001'], booking: true, run: false });
  assert.deepEqual(clicks(items), [['Containers', false, 'pick', undefined], ['Container 001', false, 'close', undefined]]);
});

test('a booking, member or run without an open page goes back by close; a run to the Imports', () => {
  assert.deepEqual(clicks(crumbTrail({ onList: false, title: 'Bookings', labels: [], booking: true, run: false })),
    [['Bookings', false, 'close', undefined]]);
  const run = crumbTrail({ onList: false, title: 'Members', labels: [], booking: false, run: true });
  assert.deepEqual(clicks(run), [['Imports', false, 'close', undefined]]);
  assert.equal(run[0].href, '/admin/members/imports/');
});

test('a payout without an open page goes back to the Payouts', () => {
  const payout = crumbTrail({ onList: false, title: 'Payments', labels: [], booking: false, run: false, payout: true });
  assert.deepEqual(clicks(payout), [['Payouts', false, 'close', undefined]]);
  assert.equal(payout[0].href, '/admin/payments/payouts/');
});
