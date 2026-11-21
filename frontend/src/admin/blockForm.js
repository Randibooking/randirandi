// SPDX-License-Identifier: AGPL-3.0-only
// The Blocks' pure logic, no DOM: the filters as the server's query
// (randirandi/core/admin_views.py: blocks), the new block's stretch and what
// its refusals say.
import { addDays, scheduleQuery } from './bookings.js';

export const BLOCK_WHEN = [['coming', 'Coming'], ['past', 'Past'], ['day', 'One day'], ['period', 'A period']];

// The list's query: the Schedule's, without a search. `dir` is "asc"
// (earliest first) or "desc"; `page` counts from 0 here, from 1 there.
export const blocksQuery = ({ when, from, to, dir, page }) => scheduleQuery({ when, from, to, q: '', dir, page });

// The stretch a new block starts with, as the drawing has it: a week from today,
// 10:00 to 22:00. Moments are "YYYY-MM-DDTHH:MM" on the asset's clock.
export const firstStretch = (today) => ({ start: `${addDays(today, 7)}T10:00`, end: `${addDays(today, 7)}T22:00` });

// The preview is asked for when an asset is chosen and the stretch runs forwards
// (the moments sort as text).
export const stretchAsked = ({ asset, start, end }) => Boolean(asset) && Boolean(start) && Boolean(end) && start < end;

export const layQuery = ({ asset, start, end }) => ({ asset, start, end });

// The body of the make; the note without the spaces round it.
export const makeBody = ({ asset, start, end, note }) => ({ asset, start, end, note: note.trim() });

// A piece as the preview lists it: "2027-03-08 18:00 to 2027-03-08 22:00 · Room hire".
export const pieceWords = (p) => `${p.start_date} ${p.start_time} to ${p.end_date} ${p.end_time} · ${p.booking_type}`;

// Block shows enabled when the preview is current and nothing refuses it.
export const canBlock = (laid, loading) => Boolean(laid) && !loading && laid.refusal === null && laid.pieces.length > 0;

export const BLOCK_WORDS = {
  nothing: 'No whole cell in this time.',
  booked: 'Booked.',
  blocked: 'Blocked already.',
  busy: 'Another change is being made. Try again in a moment.',
  past: 'The block is over, so it cannot be removed.',
  'not-found': 'The asset or the block was not found.',
  'bad-request': 'The time is not one the server accepts.',
};
const FALLBACK = 'The block could not be saved.';

// What a failed call says, in plain words.
export function blockFailure(result) {
  if (result.kind === 'offline') return 'The server could not be reached. Nothing was changed.';
  if (result.kind === 'forbidden') return 'This page is out of date. Reload it and try again.';
  return BLOCK_WORDS[result.code] ?? FALLBACK;
}
