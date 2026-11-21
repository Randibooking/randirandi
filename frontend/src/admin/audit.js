// SPDX-License-Identifier: AGPL-3.0-only
// The Audit log page's pure logic: the filters as the server's query, the dates
// in the organization's time zone. No DOM.

export const WHEN = [['all', 'Any date'], ['day', 'One day'], ['period', 'A period']];

const two = (n) => String(n).padStart(2, '0');

// A date as YYYY-MM-DD, n days from an ISO date (calendar days, no clock).
export function addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  const moved = new Date(Date.UTC(y, m - 1, d + n));
  return `${moved.getUTCFullYear()}-${two(moved.getUTCMonth() + 1)}-${two(moved.getUTCDate())}`;
}

// Today's date in a time zone, as YYYY-MM-DD.
export function todayIn(zone, now = new Date()) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit' })
      .formatToParts(now).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

// The server's query for the filters: `page` counts from 0 here, from 1 there.
// A period given the wrong way round reads the same either way.
export function auditQuery({ when, from, to, by, action, q, dir, page }) {
  const [first, last] = from <= to ? [from, to] : [to, from];
  const dates = when === 'day' ? { from, to: from } : when === 'period' ? { from: first, to: last } : {};
  return {
    ...dates,
    by: by === 'all' ? undefined : by,
    action: action === 'all' ? undefined : action,
    q: q.trim() || undefined,
    order: dir === 'asc' ? 'oldest' : 'newest',
    page: page + 1,
  };
}
