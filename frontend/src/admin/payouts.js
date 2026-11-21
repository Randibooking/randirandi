// SPDX-License-Identifier: AGPL-3.0-only
// The Payments place's Payouts tab and a payout's page: their pure logic, no DOM.
// The query is the server's (randirandi/core/admin_views.py: payouts_list,
// payout_data); the amounts are written as bookings.js writes them. What the
// daily report has not shown yet reads "Not yet", never a made-up figure.
import { figure } from './bookings.js';

export { PAYOUTS_ADDRESS, payoutAddress } from './bookings.js';

export const PAYOUT_WHEN = [['all', 'Any date'], ['day', 'One day'], ['period', 'A period']];
export const NOT_YET = 'Not yet';

// The Payouts' query: `when` is one of PAYOUT_WHEN, the days are YYYY-MM-DD (one
// day is both ends the same, a period the wrong way round reads the same) and
// `page` counts from 0 here, from 1 there.
export function payoutsQuery({ when, from, to, page }) {
  const [first, last] = from <= to ? [from, to] : [to, from];
  const days = when === 'day' ? { from, to: from } : when === 'period' ? { from: first, to: last } : {};
  return { ...days, page: page + 1 };
}

// A payout's page asks for a page of its payments and of its refunds, each
// counted from 0 here.
export const payoutPageQuery = ({ payments, refunds }) => ({ payments_page: payments + 1, refunds_page: refunds + 1 });

// An amount the report may not have shown yet.
export const amountOrNotYet = (amount) => (amount === null || amount === undefined ? NOT_YET : figure(amount));

// The day a payment was paid out, or that it was not yet.
export const paidOutOn = (payout) => (payout ? payout.day : NOT_YET);

export const payoutTitle = (day) => (day ? `Payout ${day}` : 'Payout');

// The pages a payout's cards have: the most of the two lists.
export const pagesOf = (found) => ({ payments: found.payments_on_record.pages, refunds: found.refunds_on_record.pages });
