// SPDX-License-Identifier: AGPL-3.0-only
// Admin's events: the pure logic of the pages . What the
// server answers (randirandi/core/event_pages.py) is read as it comes; here
// are the words, the query of the list, the form of an event and the body the
// server takes, what a sheet may save, and the ways out of what is in the
// way. Moments are "YYYY-MM-DDTHH:MM" on the event's clock, as the server
// writes them. No DOM.
import { addDays, scheduleQuery } from './bookings.js';

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
export const EVENT_WHEN = [['coming', 'Coming'], ['past', 'Past'], ['day', 'One day'], ['period', 'A period']];
export const SHOWN_SKIPS = 5;
export const PER_PAGE = 25;
const TWO = (n) => String(n).padStart(2, '0');

// Monday 0 to Sunday 6.
export const weekday = (date) => (new Date(`${date}T00:00Z`).getUTCDay() + 6) % 7;
export const minutes = (hm) => Number(hm.slice(0, 2)) * 60 + Number(hm.slice(3, 5));
export const hm = (m) => `${TWO(Math.floor(m / 60))}:${TWO(m % 60)}`;
export const hours = (from, to) => `${from}–${to}`;

// "Sat 24 Oct", with the year when it is not this year's.
export function dayWords(date, today) {
  const [y, m, d] = date.split('-').map(Number);
  return `${WEEKDAYS[weekday(date)]} ${d} ${MONTHS[m - 1]}${String(y) === today.slice(0, 4) ? '' : ` ${y}`}`;
}
// "Saturday 3 October 2026".
export function longDay(date) {
  const [y, m, d] = date.split('-').map(Number);
  return `${DAY_NAMES[weekday(date)]} ${d} ${MONTH_NAMES[m - 1]} ${y}`;
}
// "Sat and Sun", "Fri", "Mon, Wed and Fri".
export function daysWords(days) {
  const names = days.map((d) => WEEKDAYS[d]);
  return names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}` : names.join('');
}

// A time as the page says it: one day "Sat 24 Oct 10:00–14:00", or across days.
export function timeWords(start, end, today) {
  if (!start) return '';
  if (start.slice(0, 10) === end.slice(0, 10)) return `${dayWords(start.slice(0, 10), today)} ${hours(start.slice(11), end.slice(11))}`;
  return `${dayWords(start.slice(0, 10), today)} ${start.slice(11)} – ${dayWords(end.slice(0, 10), today)} ${end.slice(11)}`;
}
// The part of a cell an event leaves, as "09:00–10:00" or across midnight.
export function leftWords([a, b], today) {
  return a.slice(0, 10) === b.slice(0, 10) || b.slice(11) === '00:00'
    ? hours(a.slice(11), b.slice(11)) : `${a.slice(11)} – ${dayWords(b.slice(0, 10), today)} ${b.slice(11)}`;
}

// --- The list.

// The server's query for the filters: the Blocks' (when, from, to, q, order,
// page) and the event's own (category, asset). `page` counts from 0 here.
export function eventsQuery({ when, from, to, category, asset, q, dir, page }) {
  return {
    ...scheduleQuery({ when, from, to, q, dir, page }),
    category: category === 'all' ? undefined : category,
    asset: asset === 'all' ? undefined : asset,
  };
}

// The words of a category by key, from the answer's list.
export const categoryWord = (categories, key) => categories.find((c) => c.key === key)?.label ?? key;
export const seeWords = (e) => ['Name', e.show_email && 'email', e.show_phone && 'phone'].filter(Boolean).join(', ');

// --- The form of an event.

export const CATEGORY_KEYS = ['social', 'volunteer_work', 'organization', 'open_house'];

// The form of an event as the server read it, or a new one a week from today.
export function draft(e, today) {
  if (!e) {
    const date = addDays(today, 7);
    return {
      category: 'social', asset: '', title: '', description: '', start: `${date}T18:00`, end: `${date}T20:00`,
      repeat: false, weekdays: [], until: '', person: null, email: true, phone: false, resolved: {},
    };
  }
  const time = e.repeat
    ? {
      start: `${e.repeat.first}T${e.repeat.from}`,
      end: e.repeat.to === '24:00' ? `${addDays(e.repeat.first, 1)}T00:00` : `${e.repeat.first}T${e.repeat.to}`,
      repeat: true, weekdays: e.repeat.weekdays, until: e.repeat.end ?? '',
    }
    : { start: e.start, end: e.end, repeat: false, weekdays: [], until: '' };
  return {
    category: e.category, asset: e.asset?.id ?? '', title: e.title, description: e.description ?? '', ...time,
    person: e.contact, email: e.show_email, phone: e.show_phone, resolved: {},
  };
}

// A repeat's hours of the day: the end on the start's day, or midnight after
// it; null when they are neither.
export function repeatHours(f) {
  const from = f.start.slice(11);
  const day = f.start.slice(0, 10);
  if (f.end.slice(0, 10) === day) return { from, to: f.end.slice(11) };
  if (f.end === `${addDays(day, 1)}T00:00`) return { from, to: '24:00' };
  return null;
}

// What the server takes for "when".
export function whenOf(f) {
  if (!f.repeat) return { repeat: false, start: f.start, end: f.end };
  const times = repeatHours(f) ?? { from: f.start.slice(11), to: f.end.slice(11) };
  return {
    repeat: true, weekdays: f.weekdays, first: f.start.slice(0, 10), until: f.until || null, from: times.from, to: times.to,
  };
}

// The ways out chosen for other events' repeat days in the way, by the day's id.
export function settleOf(resolved) {
  const found = {};
  for (const [id, how] of Object.entries(resolved)) {
    if (!how) continue;
    found[id] = how === 'remove' ? 'remove' : { from: how.from, to: how.to };
  }
  return found;
}

// The whole event, for a make or a change.
export function eventBody(f) {
  return {
    category: f.category,
    title: f.title.trim(),
    description: f.description.trim(),
    asset: f.asset === '' ? null : f.asset,
    contact: f.person ? f.person.id : null,
    show_email: f.email,
    show_phone: f.phone,
    when: whenOf(f),
    settle: settleOf(f.resolved),
  };
}

// The preview needs only the asset, the time and the ways out chosen; it is
// asked again when one of them changes, never for a letter typed in a title.
export const previewBody = (f, eventId = null) => ({
  asset: f.asset === '' ? null : f.asset, when: whenOf(f), event: eventId, settle: settleOf(f.resolved),
});

export const asked = (f) => Boolean(f.asset) && Boolean(f.start) && Boolean(f.end) && f.start < f.end
  && (!f.repeat || (f.weekdays.length > 0 && repeatHours(f) !== null && repeatHours(f).from < repeatHours(f).to));

// Whether the time or the asset differs from the event's: only then does the
// server check it again, and only then does the sheet wait for the preview.
export function timeChanged(f, was) {
  return ['asset', 'start', 'end', 'repeat', 'weekdays', 'until'].some((k) => JSON.stringify(f[k]) !== JSON.stringify(was[k]));
}
export const formChanged = (f, was) => JSON.stringify({ ...f, resolved: {} }) !== JSON.stringify({ ...was, resolved: {} })
  || Object.keys(settleOf(f.resolved)).length > 0;

// The other hours offered for another event's day in the way: the part of its
// time before the new event, or after it, on quarter hours.
const quarter = (t, up) => hm(Math.min((up ? Math.ceil : Math.floor)(minutes(t) / 15) * 15, 23 * 60 + 45));
export function otherHoursFor(w, f) {
  const [from, to] = [w.start.slice(11), w.end.slice(11)];
  const starts = f.start.slice(0, 10) === w.date ? f.start.slice(11) : '00:00';
  const ends = f.end.slice(0, 10) === w.date ? f.end.slice(11) : '24:00';
  if (from < starts) return { from, to: quarter(starts < to ? starts : to, false) };
  if (ends < to) return { from: quarter(ends > from ? ends : from, true), to };
  return { from, to };
}
// Other hours chosen clear the new event. What else may hold them the server
// says when it is saved.
export function hoursClear(w, how, f) {
  if (how === 'remove') return true;
  if (!how || how.from >= how.to) return false;
  return !(`${w.date}T${how.from}` < f.end && f.start < `${w.date}T${how.to}`);
}

// Save stays off without a contact person, while the time waits for the
// preview, and while anything but a settled day of another event is in the way
// of a one-off. `shown` is the server's preview (null when none is asked).
export function saveable(f, shown, { contact = true, timed = true } = {}) {
  if (!f.title.trim() || !f.start || !f.end || f.start >= f.end || (f.repeat && (!f.weekdays.length || repeatHours(f) === null))) return false;
  if (contact && !f.person) return false;
  if (!timed) return true;
  if (!shown) return !f.asset;
  if (shown.repeat) return shown.takes.length > 0;
  if (shown.refusal === 'past') return false;
  return shown.way.every((w) => w.reason === 'event' && w.repeating && hoursClear(w, f.resolved[w.day], f));
}

// The skipped days counted by reason, in their words.
export const REASON_WORDS = { closed: 'Closed', booked: 'Booked', blocked: 'Blocked', event: 'Event' };
export function skipWhy(s) {
  const name = s.name ?? s.way?.title ?? s.way?.note;
  return `${REASON_WORDS[s.reason]}${name && s.reason !== 'booked' ? ` · ${name}` : ''}`;
}
export function toldWords(s, madeBy, today) {
  if (!s.told) return 'In the preview';
  const who = s.told.to.length === 1 ? s.told.to[0] : s.told.to.length > 1 ? 'every administrator' : madeBy;
  return `Email to ${who}, ${dayWords(s.told.date, today)}`;
}

// A repeat's days, coming or past, newest first when past.
export function daysShown(days, when) {
  const shown = days.filter((d) => (when === 'coming' ? !d.over : d.over));
  return when === 'past' ? [...shown].reverse() : shown;
}
export const comingDays = (e) => e.days.filter((d) => !d.removed && !d.over);

// --- What the server refuses, in words.

export const EVENT_WORDS = {
  category: 'Choose a category.',
  title: 'The title is needed, and at most 200 characters.',
  description: 'The description is at most 2000 characters.',
  asset: 'Choose an asset of this organization.',
  contact: 'Choose a contact person who is an active member.',
  dates: 'The dates are not ones an event can have.',
  weekdays: 'Choose the weekdays.',
  hours: 'The hours are not ones an event can have.',
  settle: 'The days to remove or change are not in the way any more. Look again.',
  past: 'That time is over.',
  closed: 'The asset is closed then.',
  booked: 'A booking holds that time.',
  blocked: 'A block holds that time.',
  event: 'Another event holds that time.',
  removed: 'That day is removed already.',
  'one-off': 'A one-day event is removed as a whole.',
  busy: 'Another change is being made. Try again in a moment.',
  'not-found': 'It was not found. Reload the page.',
  'bad-request': 'The request was not understood.',
};

export function eventFailure(result) {
  if (result.kind === 'offline') return 'The server could not be reached. Nothing was changed.';
  if (result.kind === 'forbidden') return 'This page is out of date. Reload it and try again.';
  const title = result.body?.way?.find((w) => w.title)?.title;
  const words = EVENT_WORDS[result.code] ?? 'The event could not be saved.';
  return result.code === 'event' && title ? `${title} holds that time.` : words;
}

// --- The event days in the way of a closure or the public holidays (S8).

// The server's query: an asset and a run of dates, or the picked dates.
export const inTheWayQuery = (asset, first, last) => ({ asset, first, last });
export const holidaysInTheWayQuery = (asset, dates) => ({ asset, dates: dates.join(',') });
// An event's page has its own address under the Events place.
export const EVENTS_ADDRESS = '/admin/events/';
export const eventAddress = (id) => `${EVENTS_ADDRESS}${id}/`;
export function eventOfAddress(pathname) {
  const found = /^\/admin\/events\/([0-9]+)\/$/.exec(pathname);
  return found ? Number(found[1]) : null;
}
export const dayKey = (t) => `${t.day}`;
// Whether every day in the way is a repeat's and removed in the same step.
export const clearOf = (days, gone) => days.every((t) => t.repeating && gone[dayKey(t)]);
export const removedDays = (days, gone) => days.filter((t) => t.repeating && gone[dayKey(t)]).map((t) => t.day);
// The body of the closure or the holidays with the days to remove.
export const withRemoved = (body, days, gone) => {
  const ids = removedDays(days, gone);
  return ids.length ? { ...body, remove_event_days: ids } : body;
};
// The event days a new block's stretch is refused for, as the in-the-way rows.
export const blockDays = (laid) => (laid?.event_days ?? []).map((d) => ({
  day: `${d.event}${d.start}`, event: d.event, title: d.title, repeating: false, date: d.start.slice(0, 10), start: d.start, end: d.end,
}));
