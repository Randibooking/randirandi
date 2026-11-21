// SPDX-License-Identifier: AGPL-3.0-only
// The Events page's content: the list in its sections, an event's times in
// words, its days, the Filter's choices, its address and its question to the
// server . Pure: no DOM.
import { label } from './labels.js';
import { daysWords } from './prices.js';
import { dateWords, hoursWords, monthName, monthShort, weekdayShort, weekdayShortOf } from './words.js';

// The four categories by key; the words are the organization's labels.
export const CATEGORIES = ['social', 'volunteer_work', 'organization', 'open_house'];
export const categoryWord = (key) => label(`event.category.${key}`);

// "Sat and Sun", "Mon to Fri", "Tue, Thu".
export function shortDays(weekdays) {
  const days = [...weekdays].sort((a, b) => a - b);
  const run = days.every((day, index) => index === 0 || day === days[index - 1] + 1);
  if (days.length === 2) return label('prices.days-pair', { first: weekdayShort(days[0]), last: weekdayShort(days[1]) });
  if (days.length > 2 && run) return label('prices.days-range', { first: weekdayShort(days[0]), last: weekdayShort(days[days.length - 1]) });
  return days.map(weekdayShort).join(', ');
}

export const dayWords = (date) => `${weekdayShortOf(date)} ${Number(date.slice(8))} ${monthShort(Number(date.slice(5, 7)))}`;

// A repeat's days as the server sent them: the first `count`, each with its
// hours, a removed day as cancelled. A day the repeat skipped is not sent.
export const daysOf = (event, count) => (event.days || []).slice(0, count);

// Whether a repeat's days are all behind us: the caption says so.
export const daysArePast = (event, today) => (event.days || []).length > 0 && event.days.every((day) => day.date < today);

// When, in words: "Every Saturday and Sunday, 10:00–14:00"; "Wednesday 11
// November, 18:00–19:00"; across midnight both dates.
export function whenWords(event) {
  if (event.repeat) return label('events.every', { days: daysWords(event.repeat.weekdays), hours: `${event.repeat.from}–${event.repeat.to}` });
  const [day, end] = [event.start.slice(0, 10), event.end.slice(0, 10)];
  if (day === end) return label('events.at', { date: dateWords(day), time: hoursWords(event.start, event.end) });
  return label('events.span', {
    from: label('events.at', { date: dateWords(day), time: event.start.slice(11, 16) }),
    to: label('events.at', { date: dateWords(end), time: event.end.slice(11, 16) }),
  });
}

// The Filter in the address, as Book's is .
export function readEventsAddress(search) {
  const query = new URLSearchParams(search);
  const where = query.get('where') || 'all';
  return {
    when: query.get('when') === 'past' ? 'past' : 'coming',
    categories: (query.get('category') || '').split(',').filter((key) => CATEGORIES.includes(key)),
    where: where === 'all' || where === 'none' || /^[1-9][0-9]{0,8}$/.test(where) ? where : 'all',
  };
}

export function eventsAddress(filter) {
  const query = new URLSearchParams();
  if (filter.when === 'past') query.set('when', 'past');
  if (filter.categories.length) query.set('category', filter.categories.join(','));
  if (filter.where !== 'all') query.set('where', filter.where);
  return query.toString();
}

// The question to the server for a Filter: what the Filter leaves out is not
// asked (`call` leaves out an empty value).
export const eventsQuery = (filter) => ({
  when: filter.when === 'past' ? 'past' : undefined,
  category: filter.categories.length ? filter.categories : undefined,
  asset: filter.where === 'all' ? undefined : filter.where,
});

// Where an event can be: every place the server names that has events, then
// Elsewhere for an event without an asset. Ids are text, as the address has them.
export const placeChoices = (places) => [
  ['all', label('events.everywhere')],
  ...(places || []).map((place) => [String(place.id), place.name]),
  ['none', label('events.elsewhere')],
];

// The name a "where" choice goes by in its chip; null for an unknown place.
export const placeName = (where, places) => {
  if (where === 'none') return label('events.elsewhere');
  return (places || []).find((place) => String(place.id) === where)?.name ?? null;
};

// The list: the repeating events once, under Every week, then each one-off
// event under its month; coming soonest first, past newest first. The server
// has already left out what the Filter does not ask for.
export function eventList(events, past, today) {
  const weekly = events.filter((event) => event.repeat)
    .map((event) => ({ event, hours: `${shortDays(event.repeat.weekdays)} ${event.repeat.from}–${event.repeat.to}` }));
  const once = events.filter((event) => !event.repeat)
    .sort((a, b) => (past ? b.start.localeCompare(a.start) : a.start.localeCompare(b.start)));
  const months = [];
  for (const event of once) {
    const month = event.start.slice(0, 7);
    if (months[months.length - 1]?.month !== month) {
      const name = monthName(Number(month.slice(5)));
      months.push({ month, title: month.slice(0, 4) === today.slice(0, 4) ? name : label('events.month-year', { month: name, year: month.slice(0, 4) }), rows: [] });
    }
    months[months.length - 1].rows.push({ event, date: event.start.slice(0, 10), hours: hoursWords(event.start, event.end) });
  }
  return { weekly, months, count: weekly.length + once.length, first: weekly[0]?.event ?? once[0]?.event ?? null };
}

// The day an event's "See it in Book" opens: a repeat's next day from today
// that is not cancelled, else its own first; a one-off's day.
export function bookDay(event, today) {
  if (!event.repeat) return event.start.slice(0, 10);
  const next = (event.days || []).find((day) => !day.cancelled && day.date >= today);
  return next?.date ?? event.days?.find((day) => !day.cancelled)?.date ?? event.repeat.first;
}
