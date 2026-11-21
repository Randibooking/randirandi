// SPDX-License-Identifier: AGPL-3.0-only
// An asset's availability page (part 7.98): its pure logic, no DOM. The server
// (randirandi/core/availability_admin.py) keeps the rules and the numbers; what
// is checked here only spares a round trip and says why a Save is not offered.
import { assetFailure, counted } from './assets.js';

export const DAY_MINUTES = 1440;
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
export const DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
export const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
  'November', 'December'];
export const MOST_SPANS_A_DAY = 24;
// Availability is set in quarter hours; the day ends at 24:00.
export const QUARTERS = Array.from({ length: 97 }, (_, i) => i * 15);

const two = (n) => String(n).padStart(2, '0');
export const clock = (m) => `${two(Math.floor(m / 60))}:${two(m % 60)}`;

// Adjacent and overlapping spans join; gaps stay closed.
export const joined = (list) => [...list].sort((a, b) => a[0] - b[0]).reduce((out, [s, e]) => {
  const last = out[out.length - 1];
  if (last && s <= last[1]) out[out.length - 1] = [last[0], Math.max(last[1], e)];
  else out.push([s, e]);
  return out;
}, []);

export const spansText = (list) => (list.length ? list.map(([s, e]) => `${clock(s)}–${clock(e)}`).join(', ') : 'Closed');

// A week as runs of weekdays with the same times: "Mon–Fri 00:00–24:00".
export function weekRuns(week) {
  const runs = [];
  week.forEach((list, d) => {
    const text = spansText(joined(list));
    const last = runs[runs.length - 1];
    if (last && last.text === text) last.to = d;
    else runs.push({ from: d, to: d, text });
  });
  return runs.map((r) => `${WEEKDAYS[r.from]}${r.to > r.from ? `–${WEEKDAYS[r.to]}` : ''} ${r.text}`);
}

// --- Dates are YYYY-MM-DD, kept as text; a day number counts them in UTC.

export const dayNumber = (iso) => Math.floor(Date.parse(`${iso}T00:00:00Z`) / 86400000);
export const isoOfDay = (n) => new Date(n * 86400000).toISOString().slice(0, 10);
export const addDays = (iso, n) => isoOfDay(dayNumber(iso) + n);
// Monday is 0.
export const weekdayOf = (iso) => (new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7;
export const dayOfMonth = (iso) => Number(iso.slice(8, 10));
export const monthKey = (iso) => iso.slice(0, 7);
export const shiftMonth = (key, n) => {
  const [y, m] = key.split('-').map(Number);
  const at = y * 12 + (m - 1) + n;
  return `${Math.floor(at / 12)}-${two((at % 12) + 1)}`;
};
export const monthTitle = (key) => `${MONTH_NAMES[Number(key.slice(5, 7)) - 1]} ${key.slice(0, 4)}`;
export const inSupportedYears = (key) => Number(key.slice(0, 4)) >= 2000 && Number(key.slice(0, 4)) <= 2100;
// The cells of a month's grid: empty cells to the weekday of the first, then the days.
export const gridOf = (days) => (days.length ? [...Array(weekdayOf(days[0].date)).fill(null), ...days] : []);
export const longDay = (iso) => `${DAY_NAMES[weekdayOf(iso)]} ${dayOfMonth(iso)} ${MONTH_NAMES[Number(iso.slice(5, 7)) - 1]} ${iso.slice(0, 4)}`;

// What a day of the month draws: the bar's open pieces and, when a closure holds
// it, the whole day cut. The closure shows in words only on a wide screen.
export const dayWords = (day) => (day.closed ? 'Closure' : day.open.length ? null : 'Closed');
export const barOf = (day) => ({
  open: day.closed ? [] : day.open,
  cut: day.closed ? [[0, DAY_MINUTES]] : [],
});

// --- The weekly availabilities.

// The week a new weekly availability starts from: the one in force on its date,
// or around the clock.
export function weekToCopy(weeks, from) {
  const inForce = weeks.filter((w) => w.from <= from).sort((a, b) => (a.from < b.from ? 1 : -1))[0];
  return (inForce ?? { week: Array.from({ length: 7 }, () => [[0, DAY_MINUTES]]) }).week;
}
export const copyWeek = (week) => week.map((day) => day.map((span) => [...span]));

export const weekOk = (week) => week.length === 7 && week.every((day) => day.length <= MOST_SPANS_A_DAY
  && day.every(([s, e]) => Number.isInteger(s) && Number.isInteger(e) && s >= 0 && s < e && e <= DAY_MINUTES));
export const weekChanged = (row, week, from) => row === null || row.from !== from
  || JSON.stringify(joined2(row.week)) !== JSON.stringify(joined2(week));
const joined2 = (week) => week.map(joined);

export const weekBody = (row, { from, week }) => (row === null
  ? { kind: 'week-make', start_date: from, week: joined2(week) }
  : { kind: 'week-change', id: row.id, start_date: from, week: joined2(week) });
export const removeWeekBody = (row) => ({ kind: 'week-remove', id: row.id });

// A weekly availability that has not started yet can be changed and removed.
export const editable = (row) => row.state === 'coming';

export const STATE_WORDS = { 'in-force': ['In force', 'success'], coming: ['Coming', 'info'], past: ['Past', 'default'] };

// --- The closures.

export const REASON_WORDS = { '': 'None', holiday: 'Holiday', event: 'Event', repair: 'Repair', vacation: 'Vacation' };
export const reasonWords = (value) => REASON_WORDS[value] ?? value;
export const closureRange = (c) => (c.first === c.last ? c.first : `${c.first} to ${c.last}`);
export const closureOk = (first, last, today) => /^\d{4}-\d{2}-\d{2}$/.test(first) && /^\d{4}-\d{2}-\d{2}$/.test(last)
  && last >= first && last >= today;
export const closureBody = (row, { first, last, reason }) => (row === null
  ? { kind: 'closure-make', first_date: first, last_date: last, reason }
  : { kind: 'closure-change', id: row.id, first_date: first, last_date: last, reason });
export const removeClosureBody = (row) => ({ kind: 'closure-remove', id: row.id });
export const closureChanged = (row, { first, last, reason }) => row === null
  || row.first !== first || row.last !== last || row.reason !== reason;

// --- The public holidays.

// A holiday that is over, or already closed, is not picked; one on a Sunday is
// left out unless ticked.
export const fixedHoliday = (h) => h.over || h.closed;
export const tickedHolidays = (holidays) => new Set(holidays.filter((h) => !fixedHoliday(h) && h.weekday !== 6).map((h) => h.date));
export const holidaysBody = (year, picked, holidays) => ({
  kind: 'holidays', year, dates: holidays.filter((h) => picked.has(h.date) && !fixedHoliday(h)).map((h) => h.date),
});
export const holidayLabel = (n) => `Add ${counted(n, 'closure')}`;

// --- What a change would cancel: the preview, and its words.

export const noChange = (preview) => !preview || preview.bookings === 0;
export const cancelsTitle = (preview) => `Cancels ${counted(preview.bookings, 'booking')}`;
export const membersLine = (preview) => counted(preview.members, 'member');
// One figure per currency, in the money's own minor units (book/add.js: moneyWords).
export const refundLines = (preview, money) => preview.refund.map((r) => money(r.amount, r.currency));
export const moreThanListed = (preview) => Math.max(0, preview.bookings - preview.listed.length);
export const confirmLabel = (kind, preview) => {
  const cancels = !noChange(preview);
  if (kind === 'remove') return cancels ? 'Remove and cancel' : 'Remove';
  return cancels ? 'Save and cancel' : 'Save';
};

// What a Save says when it is through.
export function savedWords(answer) {
  const n = answer.cancelled;
  const left = answer.unfinished?.length ?? 0;
  if (left) return `Saved. ${n} cancelled, ${left} to finish.`;
  return n ? `Saved. ${n} cancelled.` : 'Saved.';
}

const WORDS = {
  changed: 'The bookings changed since the question. Look again.',
  past: 'The date has passed.',
  'not-coming': 'Only a weekly availability that has not started can be changed.',
  over: 'A closure that is over is not changed.',
  'date-taken': 'Another weekly availability starts on that date.',
  'ends-first': 'The last date comes before the first.',
  'invalid-span': 'A time span must end after it starts, inside the day.',
  'invalid-reason': 'Choose a reason from the list.',
  invalid: 'The change was not valid.',
  'bad-request': 'The request was not understood.',
  event: 'An event is in the way.',
  'event-day': 'A day of an event could not be removed.',
  busy: 'Another change is being made. Try again in a moment.',
  'not-found': 'It was not found. Reload the page.',
};

// What a failed call says, in plain words.
export function availabilityFailure(result) {
  if (result.kind === 'refused' && WORDS[result.code]) return WORDS[result.code];
  if (result.kind === 'refused' && result.code === 'bad-request') return WORDS['bad-request'];
  return assetFailure(result);
}

// An order cancelled whose paid money the server could not record as owed: it
// needs a refund by hand.
export const UNFINISHED_WORDS = {
  recovery: 'Its payment needs recovery before it can be refunded.',
  'no-refund-left': 'No refundable amount remains.',
};
export const unfinishedWords = (item) => UNFINISHED_WORDS[item.code] ?? 'It needs a refund by hand.';

// An order cancelled whose money no refund was queued for, as the page lists it.
export const byHandWords = (row) => `Booking ${row.reference}: refund by hand since ${row.since}`;

// A refund queued and not yet taken by the provider, as the page lists it.
export const owedWords = (row, money) => `Booking ${row.reference}: ${money(row.amount, row.currency)} since ${row.since}`;
