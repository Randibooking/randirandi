// SPDX-License-Identifier: AGPL-3.0-only
// What the member's screens draw, from one calendar answer
// (randirandi/core/booking_views.py `_calendar`). Pure: no DOM, no words.
// Times stay the strings the server sent, offsets included; they are only
// compared as instants. Bookable starts are the server's, never worked out here.

const MINUTE = 60000;

export function addDays(date, count) {
  const [year, month, day] = date.split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, day + count)).toISOString().slice(0, 10);
}

// A change of length is a new question to the server; no start is kept or
// recomputed from the old answer.
export function askWithLength(query, length) {
  return { ...query, length };
}

// The member's own booking still waiting for payment is the cart's, not yet
// theirs: it shows once, "In your cart" .
// The server's word for a taken cell (`randirandi/core/member_calendar.py`
// STATES: held, confirmed, block, event) to the calendar's: an event and a
// block are no member's, so `mine` never changes them.
export function kindOf(span) {
  if (span.state === 'block') return 'block';
  if (span.state === 'event') return 'event';
  if (span.mine) return span.state === 'held' ? 'cart' : 'yours';
  return span.state === 'held' ? 'held' : 'other';
}

// Other members' bookings: each is a stretch of its own, never joined with
// the one beside it.
const SIDE = new Set(['held', 'other']);

function insideSpans(spans, from, to) {
  return spans.some(([first, last]) => Date.parse(first) <= from && to <= Date.parse(last));
}

function buildPeriod(container, period, category) {
  const ends = period.row_ends;
  // Ids compared as numbers, whether the page holds them as text or not.
  const hours = category == null
    ? null
    : period.category_hours.find((item) => Number(item.category) === Number(category));
  const blank = period.rows.map((row, index) =>
    period.closed_rows.includes(index)
    || (hours ? !insideSpans(hours.spans, Date.parse(row), Date.parse(ends[index])) : false));
  const spots = container.floor_plan ? container.floor_plan.spots.map((spot) => spot.number) : [null];
  const columns = spots.map((spot) => {
    const kinds = period.rows.map((_, index) => (blank[index] ? 'blank' : 'free'));
    // The event ({id, title}) an event cell is, by row; null elsewhere.
    const events = period.rows.map(() => null);
    // Another member's name by row ('' for none), and the booking's first row
    // (the server's span) so two bookings side by side stay two .
    const names = period.rows.map(() => '');
    const spans = period.rows.map(() => null);
    for (const span of period.taken) {
      if (span.spot !== spot) continue;
      for (let index = span.first; index <= span.last; index += 1) {
        if (blank[index]) continue;
        kinds[index] = kindOf(span);
        events[index] = span.state === 'event' ? span.event ?? null : null;
        names[index] = span.name ?? '';
        spans[index] = span.first;
      }
    }
    const stretches = [];
    kinds.forEach((kind, index) => {
      const last = stretches[stretches.length - 1];
      // A stretch never runs over a gap where the hours break off.
      // Nor over two events: each is a stretch of its own.
      if (last && last.kind === kind && Date.parse(last.to) === Date.parse(period.rows[index])
        && (last.event?.id ?? null) === (events[index]?.id ?? null)
        && (!SIDE.has(kind) || last.span === spans[index])) {
        last.last = index;
        last.to = ends[index];
      } else {
        stretches.push({ kind, first: index, last: index, from: period.rows[index], to: ends[index], span: spans[index], ...(events[index] && { event: events[index] }) });
      }
    });
    for (const stretch of stretches) {
      stretch.minutes = (Date.parse(stretch.to) - Date.parse(stretch.from)) / MINUTE;
    }
    return { spot, kinds, events, names, spans, stretches };
  });
  return {
    day: period.day,
    from: period.rows[0],
    to: period.end,
    rows: period.rows.map((at, index) => ({ at, end: ends[index], blank: blank[index], closed: period.closed_rows.includes(index) })),
    columns,
    starts: period.starts.map((start) => ({
      row: start.row,
      at: period.rows[start.row],
      end: start.end,
      spots: start.spots,
      free: start.free,
    })),
  };
}

// The Month's tile. "full": the server offers no start for what was asked
// (its length included), even when some free time is left in the share.
function summarize(date, today, now, built) {
  let total = 0;
  let free = 0;
  let mine = false;
  let cart = false;
  let rowsToday = 0;
  let starts = 0;
  for (const { period } of built) {
    starts += period.starts.length;
    for (const column of period.columns) {
      column.kinds.forEach((kind, index) => {
        if (kind === 'yours') mine = true;
        if (kind === 'cart') cart = true;
        if (kind === 'blank') return;
        rowsToday += 1;
        const row = period.rows[index];
        if (Date.parse(row.at) < now) return;
        const minutes = (Date.parse(row.end) - Date.parse(row.at)) / MINUTE;
        total += minutes;
        if (kind === 'free') free += minutes;
      });
    }
  }
  const share = total === 0 ? 0 : free / total;
  let state = 'open';
  if (date < today || (date === today && rowsToday > 0 && total === 0)) state = 'past';
  else if (rowsToday === 0) state = 'closed';
  else if (free === 0 || starts === 0) state = 'full';
  return { share, mine, cart, state };
}

// Whether a date is closed, and why: the answer lists the assets asked for and
// the dates each is closed on (by a closure with its reason's key, or by its
// weekly availability). A date is closed when every asset is; the reason is the
// one a closure gave, none when there is none or they differ. Null: not closed.
function closedOn(answer, date) {
  const assets = (answer.assets || []).map(Number);
  const rows = (answer.closed_days || []).filter((row) => row.day === date);
  if (assets.length === 0 || !assets.every((id) => rows.some((row) => Number(row.asset) === id))) return null;
  const reasons = [...new Set(rows.map((row) => row.reason).filter(Boolean))];
  return { reason: reasons.length === 1 ? reasons[0] : '' };
}

// answer: the calendar answer. query: {start: 'YYYY-MM-DD', days, category}.
// A period belongs to the date it starts on; one that starts before `start`
// (the server sends those whole) is left out. "Today" and "now" are the
// server's worked_out_at, so the member's clock plays no part.
export function buildCalendar(answer, { start, days, category = null }) {
  const now = Date.parse(answer.worked_out_at);
  const today = answer.worked_out_at.slice(0, 10);
  // The member's other categories with time on a date (the empty day's offer).
  const others = new Map((answer.other_categories || []).map((row) => [row.day, row.categories.map(Number)]));
  const byDate = new Map();
  for (const container of answer.containers) {
    for (const period of container.periods) {
      const built = { container, period: buildPeriod(container, period, category) };
      if (!byDate.has(period.day)) byDate.set(period.day, []);
      byDate.get(period.day).push(built);
    }
  }
  const dates = [];
  for (let offset = 0; offset < days; offset += 1) {
    const date = addDays(start, offset);
    const built = byDate.get(date) || [];
    const containers = [];
    for (const item of built) {
      let entry = containers.find((row) => row.id === item.container.id);
      if (!entry) {
        const { id, number, asset, type, period_type: periodType, floor_plan: plan } = item.container;
        // What the add sheet needs: the container's own interval, minimum and
        // maximum length (minutes) and its floor plan.
        entry = {
          id, number, asset, type, periodType, spots: plan ? plan.spots : [], periods: [],
          categories: (item.container.categories || []).map(Number),
          interval: item.container.interval,
          minimum: item.container.minimum_length,
          maximum: item.container.maximum_length,
          withPayment: item.container.with_payment,
          // What it costs per booking category, as numbers (day.js words them).
          prices: item.container.prices ?? null,
          shared: item.container.consumption_mode === 'shared',
          plan: plan ? { spotWord: plan.spot_word, groups: plan.groups, spots: plan.spots } : null,
        };
        containers.push(entry);
      }
      entry.periods.push(item.period);
    }
    const closed = closedOn(answer, date);
    dates.push({
      date, summary: { ...summarize(date, today, now, built), ...(closed && { closed }) }, containers, others: others.get(date) || [],
    });
  }
  return { today, now, at: answer.worked_out_at, dates };
}
