// SPDX-License-Identifier: AGPL-3.0-only
// How the screen fits the window (part 3.9984): pure arithmetic, no DOM.
// The wide Week takes the window's height below what is above it; its hour
// rows grow with the room, between a least and a largest row. The Month sits
// beside its day panel only where both fit well.
export const ROW_LEAST = 34; // pixels per hour
export const ROW_MOST = 72;
// The least height the Week is given, so a very short window scrolls the page a little.
// 360 left the Week 45 px too tall for the pill's strip at 1280x800 (part 3.99892): the Night row
// lay under the pill. 260 keeps the day headings, the Night row and about four hours in view.
export const ROOM_LEAST = 260;
// The room under the Week: the page's own bottom padding.
export const ROOM_BELOW = 16;
// The width from which the Month and its day panel sit side by side (the Month's
// seven tiles need about 60 px each beside a 300 px panel); under it the panel
// sits under the Month.
export const SIDE_BY_SIDE = 900;

export const sideBySide = (width) => width >= SIDE_BY_SIDE;

// The strip the cart pill keeps at the bottom of a wide page (part 3.99892): the pill's own
// distance from the window's bottom, its height and a gap, so nothing the member can press lies
// under it. 0 while the pill is hidden. `PILL_BOTTOM` is the pill's `bottom` on a wide screen
// (CartPill.jsx `pillPlace`) without the safe-area inset, which the caller adds.
export const PILL_BOTTOM = 24;
export const PILL_GAP = 8;
export const pillStrip = (pillHeight) => (pillHeight > 0 ? PILL_BOTTOM + pillHeight + PILL_GAP : 0);

// The Week's height: the window below its top (a document position) less the room under it
// (the page's padding, or the pill's strip when that is more), never less than `ROOM_LEAST`.
export const weekRoom = (windowHeight, top, below = 0) => Math.max(ROOM_LEAST, Math.floor(windowHeight - top - Math.max(ROOM_BELOW, below)));

// The pixels one hour takes when the hours have `room` pixels for `hours.from`..`hours.to`:
// whole pixels, filling the room, between the least and the largest row.
export function rowHeight(room, { from, to }) {
  const count = Math.max(1, (to - from) / 60);
  if (!Number.isFinite(room)) return ROW_LEAST;
  return Math.min(ROW_MOST, Math.max(ROW_LEAST, Math.floor(room / count)));
}

// Where the hours open: at the first open hour; on the week that holds today,
// at the hour before now when that is later. Never past the end of the hours.
// `now`: minute of today's red line, or null. `view`: the pixels the hours show.
export function scrollStart({ hours, row, now, view }) {
  const content = ((hours.to - hours.from) / 60) * row;
  const last = Math.max(0, content - view);
  if (now === null || now === undefined || now <= hours.from) return 0;
  const hour = Math.max(hours.from, Math.floor(now / 60) * 60 - 60);
  return Math.min(last, Math.max(0, ((hour - hours.from) / 60) * row));
}
