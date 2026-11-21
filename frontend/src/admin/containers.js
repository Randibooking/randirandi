// SPDX-License-Identifier: AGPL-3.0-only
// The Containers (part 7.96): their pure logic, no DOM. The server's answers
// (randirandi/core/container_admin.py) are the truth; the model's own words
// come back as `reasons` when the validator refuses a Save. What is worked out
// here only draws the page and says which sheet changed what.
import { setupFailure } from './setup.js';

export const DAY = 1440;
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
export const LONG_DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

const two = (n) => String(n).padStart(2, '0');
export const hm = (m) => `${two(Math.floor(m / 60))}:${two(m % 60)}`;
export const containerNo = (n) => String(n).padStart(3, '0');

// A length in minutes: "30 min", "1 h", "1 h 30 min".
export function length(minutes) {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const r = minutes % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}

// Where the period ends: the clock, and which day after the start day.
export function endText(c) {
  if (c.end_day === 0) return hm(c.period_end);
  if (c.end_day === 1) return `${hm(c.period_end)} next day`;
  return `${hm(c.period_end)} in ${c.end_day} days`;
}
export const timesText = (c) => `${hm(c.period_start)}–${endText(c)}`;
export const periodLength = (c) => c.end_day * DAY + c.period_end - c.period_start;
export const periodTypeWords = (type) => (type === 'fixed' ? 'Fixed' : 'Flexible');

// Weekdays as runs, Monday first: "Mon–Thu, Sun".
export function daysText(days) {
  const set = [...new Set(days)].sort((a, b) => a - b);
  if (set.length === 7) return 'Every day';
  const runs = [];
  set.forEach((d) => {
    const r = runs[runs.length - 1];
    if (r && r[1] === d - 1) r[1] = d;
    else runs.push([d, d]);
  });
  return runs.map(([a, b]) => (a === b ? WEEKDAYS[a] : `${WEEKDAYS[a]}${b - a > 1 ? '–' : ', '}${WEEKDAYS[b]}`)).join(', ');
}

// --- Calendar weeks, by their Monday ("2027-03-08"), as ISO weeks.

const dayOf = (iso) => Date.UTC(Number(iso.slice(0, 4)), Number(iso.slice(5, 7)) - 1, Number(iso.slice(8, 10))) / 86400000;
const isoOf = (day) => new Date(day * 86400000).toISOString().slice(0, 10);

export function isoWeek(monday) {
  const thursday = dayOf(monday) + 3;
  const year = new Date(thursday * 86400000).getUTCFullYear();
  const first = dayOf(`${year}-01-04`);
  const firstMonday = first - ((first + 3) % 7);
  return { y: year, w: (dayOf(monday) - firstMonday) / 7 + 1 };
}
export function weekText(monday) {
  const { y, w } = isoWeek(monday);
  return `week ${w}, ${y}`;
}
export function weeksText(c) {
  const every = c.every_weeks > 1 ? `Every ${c.every_weeks} weeks` : 'Every week';
  return [every, c.first_week !== null && `from ${weekText(c.first_week)}`, c.last_week !== null && `to ${weekText(c.last_week)}`]
    .filter(Boolean).join(' ');
}
export const noWeeks = (c) => c.every_weeks === 1 && c.first_week === null && c.last_week === null;

// The Monday of the week a date (YYYY-MM-DD) is in, and the weeks from there,
// two years ahead, plus the one a container already has.
export function mondayOf(iso) {
  const day = dayOf(iso);
  return isoOf(day - ((day + 3) % 7));
}
export function weekChoices(today, current) {
  const start = dayOf(mondayOf(today));
  const weeks = Array.from({ length: 105 }, (_, i) => isoOf(start + i * 7));
  return [...new Set([...(current ? [current] : []), ...weeks])].sort();
}

// --- What a container uses and what it costs.

export const planOf = (assets, c) => assets.find((a) => a.id === c.asset)?.floor_plans.find((p) => p.id === c.uses);
export function usesText(c, assets) {
  if (c.uses === null) return 'Whole asset';
  const plan = planOf(assets, c);
  if (!plan) return '–';
  return `${plan.name} · ${plan.capacity} ${plan.unit}${plan.capacity === 1 ? '' : 's'}`;
}
export const paymentText = (c, books) => (c.price_book === null ? 'No' : `Yes · ${books.find((b) => b.id === c.price_book)?.name ?? ''}`.replace(/ · $/, ''));
export const nameOf = (rows, id) => rows.find((r) => r.id === id)?.name ?? '';
export const categoryNames = (c, categories) => c.categories.map((x) => nameOf(categories, x.id)).join(', ') || '–';

// Own hours of a category, as the list shows them: "Mon–Fri 10:00–12:00".
export function ownText(category) {
  const groups = hoursGroups(category.hours);
  return groups.length ? groups.map((h) => `${daysText(h.weekdays)} ${hm(h.start)}–${hm(h.end)}`).join(', ') : '–';
}

// The server holds one stretch for each weekday; the sheet one for the same
// times on several weekdays.
export function hoursGroups(hours) {
  const groups = [];
  [...hours].sort((a, b) => a.start - b.start || a.end - b.end || a.weekday - b.weekday).forEach((h) => {
    const group = groups.find((g) => g.start === h.start && g.end === h.end);
    if (group) group.weekdays.push(h.weekday);
    else groups.push({ weekdays: [h.weekday], start: h.start, end: h.end });
  });
  return groups.map((g) => ({ ...g, weekdays: g.weekdays.sort((a, b) => a - b) }));
}
export const hoursList = (groups) => groups.flatMap((g) => [...new Set(g.weekdays)].sort((a, b) => a - b)
  .map((weekday) => ({ weekday, start: g.start, end: g.end })));

// --- What a lock covers.

// What stays as it is while a booking is still to come (docs/booking-model.md, "After a
// container is saved"; validator.FIXED). The booking type, the lengths, the last
// week, payment, categories and status may change.
export const FIXED_FIELDS = ['asset', 'uses', 'period_type', 'period_start', 'period_end', 'end_day', 'weekdays', 'interval',
  'first_week', 'every_weeks'];

// --- The sheets' drafts and what each sends: only what differs.

export const detailsDraft = (c) => ({ asset: c.asset, booking_type: c.booking_type, uses: c.uses, price_book: c.price_book });
export function detailsBody(c, d) {
  const body = {};
  if (d.asset !== c.asset) body.asset = d.asset;
  if (d.booking_type !== c.booking_type) body.booking_type = d.booking_type;
  if (d.uses !== c.uses || d.asset !== c.asset) body.uses = d.uses;
  if (d.price_book !== c.price_book) body.price_book = d.price_book;
  return body;
}

export const timeDraft = (c) => ({
  period_type: c.period_type, period_start: c.period_start, endTime: c.period_end, endDay: c.end_day, weekdays: c.weekdays,
  interval: c.interval, minimum_length: c.minimum_length, maximum_length: c.maximum_length,
  first_week: c.first_week, last_week: c.last_week, every_weeks: c.every_weeks,
});
// The end as the model has it: a clock time before 24:00 and the days after the
// start day, so 24:00 is midnight of the next day.
export function endOf(d) {
  const absolute = d.endTime + d.endDay * DAY;
  return { end_day: Math.floor(absolute / DAY), period_end: absolute % DAY };
}
export function timeBody(c, d) {
  const body = {};
  const { end_day: endDay, period_end: periodEnd } = endOf(d);
  if (d.period_type !== c.period_type) body.period_type = d.period_type;
  if (d.period_start !== c.period_start) body.period_start = d.period_start;
  if (periodEnd !== c.period_end) body.period_end = periodEnd;
  if (endDay !== c.end_day) body.end_day = endDay;
  if (d.weekdays.join() !== c.weekdays.join()) body.weekdays = d.weekdays;
  if (d.period_type === 'flexible') {
    // A container made fixed has no lengths of its own: all three are sent.
    const all = c.period_type === 'fixed';
    ['interval', 'minimum_length', 'maximum_length'].forEach((key) => {
      if (all || d[key] !== c[key]) body[key] = d[key];
    });
  }
  if (d.first_week !== c.first_week) body.first_week = d.first_week;
  if (d.last_week !== c.last_week) body.last_week = d.last_week;
  if (d.every_weeks !== c.every_weeks) body.every_weeks = d.every_weeks;
  return body;
}
export const changed = (body) => Object.keys(body).length > 0;

// The lengths a flexible period offers: whole intervals up to the period (at
// most a day), and the one it has.
export function lengthChoices(draft) {
  const { end_day: endDay, period_end: periodEnd } = endOf(draft);
  const long = Math.min(endDay * DAY + periodEnd - draft.period_start, DAY);
  const count = Math.max(0, Math.floor(long / draft.interval));
  const lengths = Array.from({ length: count }, (_, i) => (i + 1) * draft.interval);
  return (value) => [...new Set([...lengths, value])].sort((a, b) => a - b).map((m) => [m, length(m)]);
}
export const INTERVALS = [15, 30, 60, 120, 180, 240];
export const intervalChoices = (value) => [...new Set([...INTERVALS, value])].sort((a, b) => a - b).map((m) => [m, length(m)]);
export const endDayChoices = (value) => [...new Set([0, 1, 2, 3, 4, 5, 6, value])].sort((a, b) => a - b)
  .map((n) => [n, n === 0 ? 'Same day' : n === 1 ? 'Next day' : `${n} days later`]);
export const everyChoices = () => Array.from({ length: 52 }, (_, i) => [i + 1, i ? `${i + 1} weeks` : 'week']);
export const QUARTERS = Array.from({ length: 97 }, (_, i) => i * 15);
// What the Flexible choice starts from, as the drawing does.
export const FLEXIBLE_LENGTHS = { interval: 60, minimum_length: 60, maximum_length: 60 };

// Categories: the ticked ids in the list's order, each with its own hours.
// seen: the categories and own hours the sheet opened with; the server refuses the save if the container has others by then.
export const categoriesDraft = (c) => ({
  seen: c.categories.map((x) => ({ category: x.id, hours: x.hours.map((h) => ({ weekday: h.weekday, start: h.start, end: h.end })) })),
  ids: c.categories.map((x) => x.id),
  own: Object.fromEntries(c.categories.map((x) => [x.id, hoursGroups(x.hours)])),
});
export function tickCategory(draft, order, id) {
  if (draft.ids.includes(id)) {
    const own = { ...draft.own };
    delete own[id];
    return { ...draft, ids: draft.ids.filter((x) => x !== id), own };
  }
  return { ...draft, ids: order.filter((x) => x === id || draft.ids.includes(x)) };
}
// The hours a category gets when its own hours are switched on: the container's.
export const freshHours = (c) => ({ weekdays: [...c.weekdays], start: c.period_start, end: c.end_day === 0 ? c.period_end : DAY });
export const categoriesBody = (draft) => ({
  seen: draft.seen,
  categories: draft.ids.map((id) => ({ category: id, hours: hoursList(draft.own[id] ?? []) })),
});
export function categoriesChanged(c, draft) {
  return JSON.stringify(categoriesBody(categoriesDraft(c))) !== JSON.stringify(categoriesBody(draft));
}

// A new container: what the drawing asks, and what the server fills in.
export const newDraft = (assets, types) => ({ asset: assets[0]?.id ?? null, booking_type: types[0]?.id ?? null, period_type: 'flexible', uses: null });
export const newBody = (d) => ({ asset: d.asset, booking_type: d.booking_type, period_type: d.period_type, uses: d.uses });
export const newReady = (d) => d.asset !== null && d.booking_type !== null;

// --- The words of a refusal.

const WORDS = {
  'no-category': 'A container needs a booking category.',
  'unknown-category': 'A booking category was not found. Reload the page.',
  unknown: 'It was not found. Reload the page.',
};
const PLURAL = { bookings: 'booking', blocks: 'block', prices: 'price' };
export function inUseWords(used) {
  const parts = Object.entries(used ?? {}).filter(([, n]) => n > 0)
    .map(([key, n]) => `${n} ${n === 1 ? PLURAL[key] ?? key : key}`);
  return parts.length ? `In use: ${parts.join(', ')}.` : 'It is in use.';
}

// What a failed call says, a line each: the model's own words when it refused.
export function containerFailure(result) {
  if (result.code === 'refused' || result.code === 'unknown') {
    const texts = (result.body?.reasons ?? []).map((r) => r.text);
    if (texts.length) return texts;
  }
  if (result.code === 'in-use') return [inUseWords(result.body?.used)];
  return [WORDS[result.code] ?? setupFailure(result)];
}
