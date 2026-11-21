// SPDX-License-Identifier: AGPL-3.0-only
// The wide Week's own arithmetic (part 3.10): seven columns from the day entries
// `buildCalendar` made (model.js). Pure: no DOM, no fetch. Times are the strings
// the server sent; minutes are counted from midnight of the column's own date.
// The bookable starts are the server's (`period.starts`), never worked out here.
import { firstChoice, isNight } from './add.js';
import { label } from './labels.js';
import { windowStart } from './week.js';
import {
  assetBlocks, assetHours, bookable, clockOf, minutesIn, offersAt, stackingAssets,
} from './stack.js';
import {
  OWN, anyTaken, bookingBlocks, cartBlocks, dateWords, hhmm, hoursWords,
} from './words.js';

export { minutesIn };

const HOUR = 60;

const dayPeriods = (container) => container.periods;
const byDay = (entry) => entry.containers.filter((container) => !isNight(container));
const nightsOf = (entry) => entry.containers.filter(isNight);

// The hours the grid shows: from the earliest start to the latest end of any
// day-time period in the week, on whole hours. Null when nothing is open.
export function weekHours(entries) {
  let from = Infinity;
  let to = -Infinity;
  for (const entry of entries) {
    for (const container of byDay(entry)) {
      for (const period of dayPeriods(container)) {
        from = Math.min(from, minutesIn(entry.date, period.from));
        to = Math.max(to, minutesIn(entry.date, period.to));
      }
    }
  }
  if (from === Infinity) return null;
  return { from: Math.floor(from / HOUR) * HOUR, to: Math.ceil(to / HOUR) * HOUR };
}

// The hour marks on the scale, "08:00" ... .
export function hourMarks({ from, to }) {
  const marks = [];
  for (let at = from; at <= to; at += HOUR) {
    marks.push({ at, label: `${String(Math.floor(at / HOUR) % 24).padStart(2, '0')}:00` });
  }
  return marks;
}

// A lane's place inside the column: its share of the width, a gap between.
export const laneBox = (lane, lanes) => ({ left: `${(100 * lane) / lanes}%`, width: `${100 / lanes}%` });

// The booking a click on a free slot makes: the add sheet's own first choice
// for that start (add.js `firstChoice`), worded for the pointer.
export function proposalFor(container, at) {
  const choice = firstChoice(container, at);
  if (!choice) return null;
  const night = isNight(container);
  return {
    at: choice.start.at,
    end: choice.end,
    words: night
      ? label('wide.book-night')
      : label('wide.book-hours', { from: hhmm(choice.start.at), to: hhmm(choice.end) }),
  };
}

// The booking's length on the wall clock, as the slots are drawn (not elapsed
// time: on the night the clock goes back they differ).
export const proposalMinutes = (date, proposal) => minutesIn(date, proposal.end) - minutesIn(date, proposal.at);

// What a screen reader hears on a free slot.
export function slotName(date, container, at, end, several) {
  const when = { date: dateWords(date), hours: hoursWords(at, end) };
  return several
    ? label('wide.slot-name-several', { ...when, asset: container.asset.name, type: container.type.name })
    : label('wide.slot-name', when);
}

// The week's lanes: one per asset with a day-time container on any of its days, in
// the order of its first container (by number), the same lane every day. The
// containers of one asset share its lane: they stack .
export function weekLanes(entries) {
  const found = new Map();
  for (const entry of entries) {
    for (const container of byDay(entry)) {
      const lane = found.get(container.asset.id);
      if (!lane) found.set(container.asset.id, { id: container.asset.id, number: container.number, name: container.asset.name });
      else lane.number = Math.min(lane.number, container.number);
    }
  }
  return [...found.values()].sort((a, b) => a.number - b.number).map(({ id, name }) => ({ id, name }));
}

// The parts of [from, to) outside some [start, stop) ranges in order, in minutes.
function outside(from, to, ranges) {
  const parts = [];
  let at = from;
  for (const [start, stop] of ranges) {
    if (stop <= at || start >= to) continue;
    if (start > at) parts.push([at, start]);
    at = Math.max(at, stop);
  }
  if (at < to) parts.push([at, to]);
  return parts;
}

// The names written over the lanes: only when there is more than one lane.
export const laneNames = (lanes) => (lanes.length > 1 ? lanes.map((lane) => lane.name) : []);

// How many lines of words a slot of `minutes` has room for: an hour one line.
export const wordLines = (minutes) => Math.max(1, Math.floor(minutes / HOUR));

// The day's drawing: free slots (an hour each), bookings as blocks, nights.
// `lanes`: one per day-time container, in the day's order. `cart` is the cart
// answer, for what is in the member's cart.
export function dayLayout(entry, cart = null, laneIds = weekLanes([entry]).map((lane) => lane.id)) {
  const containers = byDay(entry);
  const lanes = laneIds.length;
  const slots = [];
  const blocks = [];
  const stacks = new Map(stackingAssets(entry).map((one) => [one.asset.id, one]));
  const blockOf = (lane, container, block, title) => ({
    key: `${container.id}|${block.key}`, lane, container, title, kind: block.kind, word: block.word, hours: block.hours, ...(block.event && { event: block.event }), ...(block.booked && { booked: block.booked }),
    start: minutesIn(entry.date, block.from), stop: minutesIn(entry.date, block.to),
  });
  containers.forEach((container) => {
    if (stacks.has(container.asset.id)) return;
    const lane = laneIds.indexOf(container.asset.id);
    for (const period of container.periods) {
      for (const start of period.starts) {
        const at = period.rows[start.row].at;
        const end = container.periodType === 'fixed' ? start.end : period.rows[start.row].end;
        slots.push({
          key: `${container.id}|${at}`, lane, container, at, end,
          start: minutesIn(entry.date, at), stop: minutesIn(entry.date, end),
        });
      }
    }
    const drawn = [...bookingBlocks(container), ...cartBlocks(container, entry.date, cart)];
    // A cart time someone else booked first is drawn over their block: each place one state.
    const lost = drawn.filter((block) => block.kind === 'lost')
      .map((block) => [minutesIn(entry.date, block.from), minutesIn(entry.date, block.to)])
      .sort((a, b) => a[0] - b[0]);
    for (const block of drawn) {
      const whole = blockOf(lane, container, block, `${container.type.name} ${block.hours}`);
      const parts = block.kind === 'lost' || OWN.has(block.kind) ? [[whole.start, whole.stop]] : outside(whole.start, whole.stop, lost);
      for (const [start, stop] of parts) {
        blocks.push(start === whole.start && stop === whole.stop ? whole : {
          ...whole, key: `${whole.key}|${start}`, start, stop, title: `${container.type.name} ${clockOf(start)}–${clockOf(stop)}`,
        });
      }
    }
  });
  // An asset whose containers stack is one lane: a slot per free hour of the
  // asset's day, its bookings once each.
  for (const { asset, containers: stacked } of stacks.values()) {
    const lane = laneIds.indexOf(asset.id);
    const hours = assetHours(entry.date, stacked, cart);
    for (const hour of hours) {
      if (hour.state !== 'free') continue;
      slots.push({
        key: `asset-${asset.id}|${hour.minute}`, lane, container: null, asset, stack: stacked, hour,
        offers: offersAt(stacked, entry.date, hour.minute),
        start: hour.minute, stop: hour.minute + HOUR,
      });
    }
    // Other members' bookings only outside the asset's free hours: a seat sold
    // in one container does not cover an hour another can still start in.
    const free = hours.filter((hour) => hour.state === 'free').map((hour) => [hour.minute, hour.minute + HOUR]);
    for (const block of assetBlocks(entry.date, stacked, cart)) {
      const drawn = blockOf(lane, block.container, block, `${asset.name} ${block.hours}`);
      const parts = OWN.has(drawn.kind) ? [[drawn.start, drawn.stop]] : outside(drawn.start, drawn.stop, free);
      for (const [start, stop] of parts) {
        blocks.push(start === drawn.start && stop === drawn.stop ? drawn : {
          ...drawn, key: `${drawn.key}|${start}`, start, stop, title: `${asset.name} ${clockOf(start)}–${clockOf(stop)}`,
        });
      }
    }
  }
  // One Night slot per period of a night container, each with its own state.
  const nights = nightsOf(entry).flatMap((container) => container.periods.map((period) => {
    const start = period.starts[0];
    const own = { ...container, periods: [period] };
    const taken = [...bookingBlocks(own), ...cartBlocks(container, entry.date, cart)
      .filter((block) => Date.parse(block.from) >= Date.parse(period.from) && Date.parse(block.from) < Date.parse(period.to))][0];
    const at = start ? period.rows[start.row].at : period.from;
    // A shared night with spots left, but too few for the number asked.
    const none = !start && !taken && anyTaken(own);
    return {
      key: `night|${container.id}|${period.from}`, container,
      state: start ? 'free' : (taken ? taken.kind : (none ? 'other' : null)),
      at, end: start ? start.end : period.to,
      hours: taken ? taken.hours : (none ? hoursWords(period.from, period.to) : null),
      word: taken ? taken.word : (none ? label('free.nothing') : null),
      ...(taken?.booked && { booked: taken.booked }),
    };
  })).filter((night) => night.state !== null);
  // A block over a free slot (the member's own seat where seats are left)
  // lets the pointer through, so the slot stays bookable.
  for (const block of blocks) {
    block.over = slots.some((slot) => slot.lane === block.lane && slot.start < block.stop && block.start < slot.stop);
  }
  return {
    lanes, slots, blocks, nights,
  };
}

// The red line for now: its minute in today's column, or null when it is not
// today or lies outside the shown hours. `at` is the server's worked_out_at.
// `elapsed`: milliseconds since that answer was read, so the line moves.
export function nowLine(date, today, at, { from, to }, elapsed = 0) {
  if (date !== today) return null;
  const minute = minutesIn(today, at) + (Number.isFinite(elapsed) ? Math.floor(elapsed / 60000) : 0);
  return minute > from && minute < to ? minute : null;
}

// Where a picked date takes the Week: the Monday-to-Sunday week that holds it,
// with the date itself chosen. Never before today.
export function jumpTo(date, today) {
  const day = date < today ? today : date;
  return { first: windowStart(day, today), chosen: day };
}

// What a free slot does under the pointer: the one offer that can start there
// (stack slots) or its own container's start, as `proposalFor`; several offers
// read "From 17:00". `minutes`: the ghost's wall-clock length.
export function slotProposal(date, slot) {
  if (slot.container) {
    const proposal = proposalFor(slot.container, slot.at);
    return proposal && { words: proposal.words, minutes: proposalMinutes(date, proposal) };
  }
  const only = bookable(slot.offers);
  if (only.length === 1) {
    const proposal = proposalFor(only[0].container, only[0].choice.start.at);
    return proposal && { words: proposal.words, minutes: proposalMinutes(date, proposal) };
  }
  return { words: label('stack.from', { time: slot.hour.label }), minutes: HOUR };
}

// What a click on a free slot opens: the add sheet for one container at a
// start, or the sheet of offers for an asset's hour.
export function slotAction(date, slot) {
  if (slot.container) return { add: { container: slot.container, date, from: slot.at } };
  const only = bookable(slot.offers);
  if (only.length === 1) return { add: { container: only[0].container, date, from: only[0].choice.start.at } };
  return { stack: { asset: slot.asset, containers: slot.stack, date, minute: slot.hour.minute } };
}

// What a screen reader hears on a free slot: the stack's hour, or the hours a
// click proposes (`slotProposal`), else the slot's own.
export function slotWords(date, slot, several) {
  const named = (container, at, end) => {
    const proposal = proposalFor(container, at);
    return proposal
      ? slotName(date, container, proposal.at, proposal.end, several)
      : slotName(date, container, at, end, several);
  };
  if (slot.container) return named(slot.container, slot.at, slot.end);
  const only = bookable(slot.offers);
  if (only.length === 1) return named(only[0].container, only[0].choice.start.at, only[0].choice.end);
  return label('wide.stack-slot-name', {
    time: slot.hour.label, date: dateWords(date), asset: slot.asset.name, from: slot.hour.label, to: slot.hour.end,
  });
}

// A container's free stretch: its neighbouring free hours as one block with one
// stripe (). Hours of one lane and one owner (container, else asset) that
// touch are one run; a run keeps its hours, so a click can name the hour under
// the pointer.
export function freeRuns(slots) {
  const owner = (slot) => (slot.container ? `c${slot.container.id}` : `a${slot.asset.id}`);
  const runs = [];
  for (const slot of [...slots].sort((a, b) => a.lane - b.lane || owner(a).localeCompare(owner(b)) || a.start - b.start)) {
    const last = runs[runs.length - 1];
    if (last && last.owner === owner(slot) && last.lane === slot.lane && last.stop === slot.start) {
      last.stop = slot.stop;
      last.slots.push(slot);
    } else runs.push({ key: slot.key, owner: owner(slot), lane: slot.lane, start: slot.start, stop: slot.stop, slots: [slot] });
  }
  return runs;
}

// The hour of a run at a minute from midnight; the run's first where the
// minute falls outside (the keyboard has no pointer).
export function hourInRun(run, minute) {
  if (minute === null || minute === undefined) return run.slots[0];
  return run.slots.find((slot) => slot.start <= minute && minute < slot.stop) || run.slots[0];
}
