// SPDX-License-Identifier: AGPL-3.0-only
// What a day says, in words, from `buildCalendar`'s dates. Pure: no DOM.
// Times are the strings the server sent (local, offset included): the hours
// are read off them, never through the browser's zone.
import { hasLabel, label } from './labels.js';

// The names of the weekdays and months are labels too: `weekdays.mon` is
// "Monday", `weekdays-short.mon` "Mon", `weekdays-initial.mon` "M";
// `months.1` is "January", `months-short.1` "Jan".
export const WEEKDAY_CODES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

export const weekdayShort = (index) => label(`weekdays-short.${WEEKDAY_CODES[index]}`);
export const weekdayInitial = (index) => label(`weekdays-initial.${WEEKDAY_CODES[index]}`);
export const monthName = (number) => label(`months.${number}`);
export const monthShort = (number) => label(`months-short.${number}`);
// A server's word shown on its own ("taken" → "Taken").
export const sentence = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

export const weekdayIndex = (date) => {
  const [year, month, day] = date.split('-').map(Number);
  return (new Date(Date.UTC(year, month - 1, day)).getUTCDay() + 6) % 7;
};

// The short name of a date's weekday: "Tue".
export const weekdayShortOf = (date) => weekdayShort(weekdayIndex(date));

// A column's or chip's name: "Today" on today, else the weekday's short name.
export const dayName = (date, today) => (date === today ? label('day.today') : weekdayShortOf(date));

export const monthTitle = (month) => `${monthName(Number(month.slice(5)))} ${month.slice(0, 4)}`;

// "Tuesday 20 October"
export const dateWords = (date) => label('date.words', {
  weekday: label(`weekdays.${WEEKDAY_CODES[weekdayIndex(date)]}`),
  day: Number(date.slice(8)),
  month: monthName(Number(date.slice(5, 7))),
});


// The mail link of a booking's sheet: the organization's contact address, a
// subject that names the booking .
export const contactHref = (booked, address) => `mailto:${address}?subject=${encodeURIComponent(label('booked.subject', {
  name: booked.name, place: booked.place, date: dateWords(booked.date), hours: booked.hours,
}))}`;

export const hhmm = (moment) => moment.slice(11, 16);

// "17:00" for a minute from midnight.
export const clockOf = (minute) =>
  `${String(Math.floor(minute / 60) % 24).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;

// "16:00–17:00", with "next day" when it ends on a later date.
export const hoursWords = (from, to) => (to.slice(0, 10) > from.slice(0, 10)
  ? label('hours.next-day', { from: hhmm(from), to: hhmm(to) })
  : `${hhmm(from)}–${hhmm(to)}`);

// A closed day in words (`closedReason`: the reason alone, for where the Closed look is shown beside it): "Closed", or "Closed: Holiday" with the closure's reason
// (the organization's word for it, `closure.<reason>`).
export const closedReason = (closed) => (closed.reason && hasLabel(`closure.${closed.reason}`)
  ? label(`closure.${closed.reason}`) : '');
export const closedWords = (closed) => (closedReason(closed)
  ? label('day.closed-why', { reason: closedReason(closed) })
  : label('day.closed'));

// The tile's name for a screen reader: the date and what is left.
// `nightBooked`: someone else's night, or an event's, the moon by the date.
export function tileName(date, summary, today, { inCart = false, nightBooked = false, runs = [] } = {}) {
  const parts = [dateWords(date)];
  const free = runs.filter((run) => run.state === 'free');
  if (date === today) parts.push(label('tile.today'));
  if (summary.state === 'past') parts.push(label('tile.passed'));
  else if (summary.closed) parts.push(closedWords(summary.closed));
  else if (summary.state === 'closed') parts.push(label('common.nothing-to-book'));
  else if (summary.state === 'full') parts.push(label('tile.full'));
  else if (free.length) parts.push(...free.map((run) => label('tile.free', { from: clockOf(run.from), to: clockOf(run.to) })));
  else parts.push(label('tile.left', { percent: Math.round(summary.share * 100) }));
  if (nightBooked) parts.push(label('tile.night'));
  if (summary.mine) parts.push(label('tile.yours'));
  else if (inCart) parts.push(label('tile.cart'));
  return parts.join(', ');
}

// The label key of the word on a booking's block, by the kind of booking.
const BOOKED_WORD = {
  other: 'state.booked', held: 'state.held', cart: 'state.cart', yours: 'state.yours', lost: 'state.lost',
  event: 'state.event', closed: 'state.closed',
};

// A day with nothing to book: no container, or all its time closed.
export const isEmptyDay = (entry) => entry.containers.length === 0 || entry.summary.state === 'closed';

// What an empty day says, and the one button that loosens only the choice
// that left it empty , in the demo's order (`whyEmpty`): another of the member's
// categories with time that day, keeping the type; else all types. The date
// is named, as a weekday's other dates may differ. `button`: its words and
// the Filter `patch`; null without one. Not the switch's own change, so the
// category is not kept for the member ; the button moves the
// page's switch, which shows the change.
export function emptyDay(entry, filter) {
  // A closed day is closed, whatever the Filter asks: nothing to loosen.
  if (entry.summary.closed) return { line: closedWords(entry.summary.closed), button: null };
  const day = dateWords(entry.date);
  const type = filter.types.find((row) => row.id === filter.type);
  const chosen = filter.categories.find((row) => row.id === filter.category);
  const other = filter.categories.find((row) => row.id !== filter.category && (entry.others || []).includes(Number(row.id)));
  if (other) {
    return {
      line: type
        ? label('day.no-category-type', { category: chosen.name, type: type.name, day })
        : label('day.no-category', { category: chosen.name, day }),
      button: { words: label('day.show-category', { category: other.name }), patch: { category: other.id } },
    };
  }
  if (type) return { line: label('day.no-type', { type: type.name, day }), button: { words: label('day.show-all'), patch: { type: null } } };
  return { line: label('day.nothing', { day }), button: null };
}

// The bookable time of one date period: what the server's starts cover, from
// each start to its end, joined where they touch. [[from, to], ...] in order.
// Free cells with no start behind them are not free here.
function periodRanges(period) {
  const found = period.starts.map((start) => [start.at, start.end]);
  found.sort((a, b) => Date.parse(a[0]) - Date.parse(b[0]) || Date.parse(b[1]) - Date.parse(a[1]));
  const joined = [];
  for (const [from, to] of found) {
    const last = joined[joined.length - 1];
    if (last && Date.parse(from) <= Date.parse(last[1])) {
      if (Date.parse(to) > Date.parse(last[1])) last[1] = to;
    } else {
      joined.push([from, to]);
    }
  }
  return joined;
}

// The free time of a container in a day, over all its periods.
export const freeRanges = (container) =>
  container.periods.flatMap((period) => periodRanges(period))
    .sort((a, b) => Date.parse(a[0]) - Date.parse(b[0]));

// The member's own bookings, drawn wherever they are.
export const OWN = new Set(['yours', 'cart']);
// Of other members' bookings over one time, the first of these shows.
const OTHERS = ['held', 'other', 'event', 'closed'];

// What other members take in one row of a period: their firmest kind when
// every open spot is taken and none of them by the member, else null. One
// taken spot of many only shortens what is left .
function othersIn(period, index) {
  const kinds = period.columns.map((column) => column.kinds[index]).filter((kind) => kind !== 'blank');
  if (!kinds.length || kinds.some((kind) => kind === 'free' || OWN.has(kind))) return null;
  return kinds.map((kind) => (kind === 'block' ? 'closed' : kind))
    .reduce((one, two) => (OTHERS.indexOf(two) < OTHERS.indexOf(one) ? two : one));
}

// Who is behind the booking of `kind` in a row : its member's
// name when every spot it takes there is one member's, else none; and which
// booking it is, so two bookings one after the other are two blocks.
export function whoIn(period, index, kind) {
  if (kind !== 'held' && kind !== 'other') return { name: '', booking: '' };
  const cells = period.columns.filter((column) => column.kinds[index] === kind);
  const names = new Set(cells.map((column) => column.names?.[index] ?? ''));
  const spans = [...new Set(cells.map((column) => column.spans?.[index] ?? ''))].sort();
  return { name: names.size === 1 ? [...names][0] : '', booking: spans.join(',') };
}

// The event an event row is: the first column's, an event holds every column.
function eventIn(period, index) {
  for (const column of period.columns) {
    if (column.kinds[index] === 'event' && column.events?.[index]) return column.events[index];
  }
  return null;
}

// Whether anything is taken in the container, in any spot: also a spot of
// many, which draws no block.
export const anyTaken = (container) => container.periods.some((period) =>
  period.columns.some((column) => column.kinds.some((kind) => kind !== 'free' && kind !== 'blank')));

// The bookings in a container as blocks, one per kind and hours: the member's
// own in any spot (a shared container's spots with the same booking time are
// one block), other members' only where no spot is left.
export function bookingBlocks(container) {
  const seen = new Set();
  const blocks = [];
  // An event is worded by its title, and carries its id to open its page .
  // Another member's booking carries that member's name , and
  // `booked`, what its sheet says.
  const place = [container.asset?.name, container.type?.name].filter(Boolean).join(' · ');
  const add = (kind, from, to, event = null, name = '') => {
    const key = `${kind}|${from}|${to}`;
    if (seen.has(key)) return;
    seen.add(key);
    const hours = hoursWords(from, to);
    blocks.push({
      key, kind, word: event ? (event.title || label(BOOKED_WORD.event)) : name || label(BOOKED_WORD[kind]), from, to, hours, ...(event && { event }),
      ...(name && { name, booked: { name, place, date: from.slice(0, 10), hours } }),
    });
  };
  for (const period of container.periods) {
    for (const column of period.columns) {
      for (const stretch of column.stretches) {
        if (OWN.has(stretch.kind)) add(stretch.kind, stretch.from, stretch.to);
      }
    }
    // A stretch never runs over a gap where the hours break off.
    let open = null;
    period.rows.forEach((row, index) => {
      const kind = othersIn(period, index);
      const event = kind === 'event' ? eventIn(period, index) : null;
      const who = kind ? whoIn(period, index, kind) : { name: '', booking: '' };
      if (open && kind === open.kind && (open.event?.id ?? null) === (event?.id ?? null) && open.booking === who.booking
        && Date.parse(open.to) === Date.parse(row.at)) {
        open.to = row.end;
        return;
      }
      if (open) add(open.kind, open.from, open.to, open.event, open.name);
      open = kind ? { kind, from: row.at, to: row.end, event, ...who } : null;
    });
    if (open) add(open.kind, open.from, open.to, open.event, open.name);
  }
  return blocks.sort((a, b) => Date.parse(a.from) - Date.parse(b.from));
}

// What the member has in the cart in this container on this date, as blocks
// (the cart answer's bookings, `cart.bookings`). A row whose booking the
// calendar already draws (an order waiting for payment) is left to it. A cart
// time is held only at payment, so someone else may book it first: a row inside
// a stretch another member has taken is `lost`, drawn as taken, never as the cart's.
export function cartBlocks(container, date, cart) {
  const taken = bookingBlocks(container).filter((block) => !OWN.has(block.kind));
  const lostTo = (row) => taken.some((block) =>
    Date.parse(block.from) < Date.parse(row.end) && Date.parse(row.start) < Date.parse(block.to));
  const drawn = container.periods.flatMap((period) => period.columns.flatMap((column) => column.stretches))
    .filter((stretch) => stretch.kind === 'cart');
  return (cart?.bookings || [])
    .filter((row) => row.container.id === container.id && row.start.slice(0, 10) === date)
    .filter((row) => !drawn.some((stretch) =>
      Date.parse(stretch.from) < Date.parse(row.end) && Date.parse(row.start) < Date.parse(stretch.to)))
    .map((row) => ({
      key: `cart|${row.id}`,
      kind: lostTo(row) ? 'lost' : 'cart',
      word: label(lostTo(row) ? 'state.lost' : 'state.cart'),
      from: row.start,
      to: row.end,
      hours: hoursWords(row.start, row.end),
    }));
}

// A flexible container: "Available 16:00–17:00, 19:00–20:00". A fixed one (a
// night, a session) reads per period, "17:00–19:00 · Available" (the demo's
// Week). "Nothing free" when something is booked, "Passed" when nothing is
// left to book.
export function freeLines(container, blocks) {
  if (container.periodType === 'fixed') {
    const lines = container.periods
      .filter((period) => period.starts.length > 0)
      .map((period) => label('free.fixed', { hours: hoursWords(period.from, period.to) }));
    if (lines.length) return lines;
  } else {
    const ranges = freeRanges(container);
    if (ranges.length) return [label('free.flexible', { hours: ranges.map(([from, to]) => hoursWords(from, to)).join(', ') })];
  }
  return [label(blocks.length || anyTaken(container) ? 'free.nothing' : 'free.passed')];
}

const overlaps = (a, b) => Date.parse(a.from) < Date.parse(b.to) && Date.parse(b.from) < Date.parse(a.to);

// One rule for a cart time against other members' bookings, whatever the
// container: a cart block touching one at all is `lost` whole (the server takes
// the whole cart item as taken).
export function markLost(blocks) {
  const taken = blocks.filter((block) => !OWN.has(block.kind) && block.kind !== 'lost');
  return blocks.map((block) => (block.kind === 'cart' && taken.some((one) => overlaps(block, one))
    ? { ...block, kind: 'lost', word: label('state.lost') } : block));
}

// The booked blocks of a list cut around the lost times, so no time is listed twice.
export function cutAround(blocks) {
  const lost = blocks.filter((block) => block.kind === 'lost').sort((a, b) => Date.parse(a.from) - Date.parse(b.from));
  return blocks.flatMap((block) => {
    if (OWN.has(block.kind) || block.kind === 'lost') return [block];
    const parts = [];
    let from = block.from;
    for (const one of lost) {
      if (!overlaps({ from, to: block.to }, one)) continue;
      if (Date.parse(one.from) > Date.parse(from)) parts.push([from, one.from]);
      from = Date.parse(one.to) > Date.parse(from) ? one.to : from;
    }
    if (Date.parse(from) < Date.parse(block.to)) parts.push([from, block.to]);
    return parts.map(([start, stop]) => (start === block.from && stop === block.to ? block
      : { ...block, key: `${block.key}|${start}`, from: start, to: stop, hours: hoursWords(start, stop) }));
  });
}

// What a day lists: its containers, each with its name, its free hours in
// words and its bookings as blocks. The asset's name comes first when the
// day's containers are on several assets.
export function dayList(entry, cart = null) {
  const several = new Set(entry.containers.map((row) => row.asset.id)).size > 1;
  return entry.containers.map((container) => {
    const blocks = cutAround([...bookingBlocks(container), ...cartBlocks(container, entry.date, cart)])
      .sort((a, b) => Date.parse(a.from) - Date.parse(b.from));
    const lines = freeLines(container, blocks);
    return {
      id: container.id,
      name: several ? `${container.asset.name} · ${container.type.name}` : container.type.name,
      night: container.periods.some((period) => period.to.slice(0, 10) > period.day),
      lines,
      free: freeRanges(container).length > 0,
      blocks,
    };
  });
}
