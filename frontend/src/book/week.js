// SPDX-License-Identifier: AGPL-3.0-only
// The Week's own arithmetic and what its chips say. Pure: dates are
// 'YYYY-MM-DD' strings, a month is 'YYYY-MM'; no clock, no DOM.

import { label } from './labels.js';
import { addDays } from './model.js';
import { monthLength, monthOf, monthStart, weekdayOf } from './month.js';
import { monthName, monthShort } from './words.js';

// The week's seven dates from its first day (always a Monday in the Week).
export const weekDays = (first) => Array.from({ length: 7 }, (_, offset) => addDays(first, offset));

// "19–25 October", or "28 Oct – 3 Nov" when the week crosses months.
export function weekTitle(first) {
  const last = addDays(first, 6);
  const [from, to] = [first, last].map((date) => Number(date.slice(5, 7)));
  if (monthOf(first) === monthOf(last)) {
    return label('week.title-month', { first: Number(first.slice(8)), last: Number(last.slice(8)), month: monthName(from) });
  }
  return label('week.title-months', {
    first: Number(first.slice(8)), firstMonth: monthShort(from), last: Number(last.slice(8)), lastMonth: monthShort(to),
  });
}

// The Monday of the week that holds a date.
export const mondayOf = (date) => addDays(date, -weekdayOf(date));

// The week a date is in: Monday to Sunday, always. A date before today is in
// the week that holds today.
export const windowStart = (date, today) => mondayOf(date < today ? today : date);

// The day chosen in Book, unless it is already past.
export const usableDay = (day, today) => (day && day >= today ? day : null);

// Of a week's asks, those to send: all when forced (a return to Book, Try again),
// else only the months the store does not hold fresh.
export const weekToAsk = (asks, has, force) => (force ? asks : asks.filter((ask) => !has(ask)));

// Not before the week that holds today.
export const canGoBackWeek = (first, today) => first > mondayOf(today);

// The day chosen after a move to a week: the one picked if it is in the week,
// else today if it is, else the first day.
export function chosenDay(first, picked, today) {
  const days = weekDays(first);
  if (picked && days.includes(picked)) return picked;
  return days.includes(today) ? today : first;
}

// The calendar answers a week needs: one per month it touches, each the
// whole month (the same question the Month asks, so its kept answer serves).
export function weekAsks(first) {
  const months = [];
  for (const date of weekDays(first)) {
    const month = monthOf(date);
    if (!months.includes(month)) months.push(month);
  }
  return months.map((month) => ({ month, start: monthStart(month), days: monthLength(month) }));
}

// A chip, from the day's built entry (null while its answer is not in).
// `dim`: past and closed days (and days with no answer) are dimmed, yours never.
export function chipState(entry, date, today) {
  if (!entry) return {
    reach: date >= today, open: false, full: false, mine: false, share: 0, dim: true,
  };
  const { state, mine, share } = entry.summary;
  const open = state !== 'past' && state !== 'closed';
  const full = state === 'full';
  return {
    reach: date >= today, open, full, mine, share: open && !full ? share : 0, dim: !open && !mine,
  };
}
