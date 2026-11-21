// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
import { NOT_YET, PAYOUTS_ADDRESS, amountOrNotYet, pagesOf, paidOutOn, payoutAddress, payoutPageQuery, payoutTitle, payoutsQuery } from './payouts.js';

test('the Payouts query: any date sends none, one day both ends, a period in order', () => {
  assert.deepEqual(payoutsQuery({ when: 'all', from: '2027-02-01', to: '2027-02-09', page: 0 }), { page: 1 });
  assert.deepEqual(payoutsQuery({ when: 'day', from: '2027-02-05', to: '2027-02-09', page: 2 }), { from: '2027-02-05', to: '2027-02-05', page: 3 });
  assert.deepEqual(payoutsQuery({ when: 'period', from: '2027-02-09', to: '2027-02-01', page: 0 }), { from: '2027-02-01', to: '2027-02-09', page: 1 });
});

test('a payout page asks for its two lists by page, counted from 1 at the server', () => {
  assert.deepEqual(payoutPageQuery({ payments: 0, refunds: 0 }), { payments_page: 1, refunds_page: 1 });
  assert.deepEqual(payoutPageQuery({ payments: 2, refunds: 1 }), { payments_page: 3, refunds_page: 2 });
});

test('what the report has not shown reads Not yet; an amount of nothing is an amount', () => {
  assert.equal(amountOrNotYet(null), NOT_YET);
  assert.equal(amountOrNotYet(undefined), NOT_YET);
  assert.equal(amountOrNotYet(0), '0');
  assert.equal(amountOrNotYet(12550), '125,50');
  assert.equal(paidOutOn(null), NOT_YET);
  assert.equal(paidOutOn({ id: 4, day: '2027-02-20' }), '2027-02-20');
});

test('the addresses and the title', () => {
  assert.equal(PAYOUTS_ADDRESS, '/admin/payments/payouts/');
  assert.equal(payoutAddress(12), '/admin/payments/payouts/12/');
  assert.equal(payoutTitle('2027-02-20'), 'Payout 2027-02-20');
  assert.equal(payoutTitle(undefined), 'Payout');
  assert.deepEqual(pagesOf({ payments_on_record: { pages: 3 }, refunds_on_record: { pages: 1 } }), { payments: 3, refunds: 1 });
});
