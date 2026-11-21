// SPDX-License-Identifier: AGPL-3.0-only
// The Month's own arithmetic. Pure: dates are 'YYYY-MM-DD' strings, a month is
// 'YYYY-MM'; no clock, no DOM. Words are in words.js.

import { addDays } from './model.js';

const WEEK = 7;

export const monthOf = (date) => date.slice(0, 7);

export const monthStart = (month) => `${month}-01`;

export function shiftMonth(month, count) {
  const [year, number] = month.split('-').map(Number);
  const moved = new Date(Date.UTC(year, number - 1 + count, 1));
  return moved.toISOString().slice(0, 7);
}

export const monthLength = (month) => Number(addDays(monthStart(shiftMonth(month, 1)), -1).slice(8));

// Monday is 0.
export function weekdayOf(date) {
  const [year, month, day] = date.split('-').map(Number);
  return (new Date(Date.UTC(year, month - 1, day)).getUTCDay() + 6) % WEEK;
}

// The shown month in weeks from Monday; a day outside the month is null.
export function monthWeeks(month) {
  const length = monthLength(month);
  const cells = Array.from({ length: weekdayOf(monthStart(month)) }, () => null);
  for (let day = 1; day <= length; day += 1) cells.push(`${month}-${String(day).padStart(2, '0')}`);
  while (cells.length % WEEK) cells.push(null);
  const weeks = [];
  for (let at = 0; at < cells.length; at += WEEK) weeks.push(cells.slice(at, at + WEEK));
  return weeks;
}

// The ISO week number of a date (the week of its Thursday; weeks start on
// Monday and week 1 holds the year's first Thursday).
export function isoWeek(date) {
  const [year, month, day] = date.split('-').map(Number);
  const thursday = new Date(Date.UTC(year, month - 1, day + 3 - weekdayOf(date)));
  const first = Date.UTC(thursday.getUTCFullYear(), 0, 1);
  return Math.floor(Math.round((thursday.getTime() - first) / 86400000) / 7) + 1;
}

// The shown month's weeks with the number each is shown under.
export const numberedWeeks = (month) => monthWeeks(month)
  .map((dates) => ({ week: isoWeek(dates.find(Boolean)), dates }));

// The built day for a date, or null: a calendar built for another month has
// no such day, and the grid draws nothing from it.
export const dayOn = (built, date) => built?.dates.find((row) => row.date === date) ?? null;

// Never a month before today's.
// A full day's date is struck, but never on a day holding something of the
// member's, booked or in the cart: there it is their blue pill or ring ().
export const dateStruck = ({ full, mine, cart = false }) => Boolean(full && !mine && !cart);

export const clampMonth = (month, today) => (month < monthOf(today) ? monthOf(today) : month);

// Not before the current month.
export const canGoBack = (month, today) => month > monthOf(today);

// A calendar answer for the days [start, start + days), asking for fewer
// (halves, down to one day) while the server says 422 "fewer-days". `read` is
// `readCalendar`. The answers join into one: a container once, a date period
// once even when it came in two halves.
export async function readDays(read, query, start, days) {
  const result = await read({ ...query, start, days });
  if (!(result.kind === 'refused' && result.code === 'fewer-days' && days > 1)) return result;
  const first = Math.ceil(days / 2);
  const [a, b] = await Promise.all([
    readDays(read, query, start, first),
    readDays(read, query, addDays(start, first), days - first),
  ]);
  if (!a.ok) return a;
  if (!b.ok) return b;
  return { ok: true, data: joinAnswers(a.data, b.data) };
}

export function joinAnswers(first, second) {
  const containers = first.containers.map((row) => ({ ...row, periods: [...row.periods] }));
  for (const row of second.containers) {
    const same = containers.find((item) => item.id === row.id);
    if (!same) {
      containers.push({ ...row, periods: [...row.periods] });
      continue;
    }
    for (const period of row.periods) {
      if (!same.periods.some((item) => item.day === period.day && item.rows[0] === period.rows[0])) {
        same.periods.push(period);
      }
    }
  }
  containers.sort((a, b) => a.number - b.number);
  // The halves' dates differ; each date's other categories come once.
  const others = [...(first.other_categories || [])];
  for (const row of second.other_categories || []) {
    if (!others.some((item) => item.day === row.day)) others.push(row);
  }
  // Each half names its own dates' closed days; the assets are the same.
  const closedDays = [...(first.closed_days || []), ...(second.closed_days || [])];
  return { ...first, containers, other_categories: others, closed_days: closedDays };
}

// Where the Today button goes: today's month, today's week (the window starts
// at today and snaps to its Monday), today chosen. The same in all three views.
export const todayTarget = (today) => ({ month: monthOf(today), from: today, chosen: today });

// The kinds the server sent in some days (`buildCalendar`'s dates): the legend
// draws an event and a closed time only when one is there.
export function kindsSeen(entries) {
  const seen = new Set();
  for (const entry of entries) {
    // A closed day, too, is a closed time.
    if (entry?.summary?.closed) seen.add('block');
    for (const container of entry?.containers ?? []) {
      for (const period of container.periods) for (const column of period.columns) for (const kind of column.kinds) seen.add(kind);
    }
  }
  return seen;
}
