// SPDX-License-Identifier: AGPL-3.0-only
// Where an audit event's object opens: a member, a member import and a
// container open their own page; the other kinds that have a place in the admin
// open that place. The server sends the target's kind and id
// (randirandi/core/audit_log.py, `_targets`); a kind with no page has no target.
// No DOM.
import { importAddress, memberAddress } from './bookings.js';
import { hrefOf } from './places.js';

// The place of the kinds that open a place.
export const PLACE_OF = {
  organization: 'organization',
  space: 'assets',
  asset_inventory: 'assets',
  spot_group: 'assets',
  spot: 'assets',
  asset: 'assets',
  booking_type: 'types',
  booking_category: 'types',
  price_book: 'prices',
  price_book_entry: 'prices',
  role: 'roles',
  block: 'blocks',
};

// What the click on a target does, for the shell: a member or a run (by its
// number) of the Members, a container of the Containers, or a place.
export function targetMove(target) {
  if (!target) return null;
  if (target.kind === 'member') return { type: 'member', id: target.id };
  if (target.kind === 'member_import') return { type: 'run', id: target.id };
  if (target.kind === 'booking_configuration') return { type: 'container', id: target.id };
  if (target.kind === 'event') return { type: 'event', id: target.id };
  const place = PLACE_OF[target.kind];
  return place ? { type: 'place', place } : null;
}

// The address of the same, for a click with a key held and for the status line.
export function targetAddress(target) {
  const move = targetMove(target);
  if (!move) return null;
  if (move.type === 'member') return memberAddress(move.id);
  if (move.type === 'run') return importAddress(move.id);
  if (move.type === 'event') return `${hrefOf('events')}${move.id}/`;
  return hrefOf(move.type === 'container' ? 'containers' : move.place);
}

// What a click on an event's object does, for the shell: a booking opened from the
// Audit log, a member, a run, a container, a place, or nothing (no page for it).
export function auditStep(what) {
  if (what.booking) return { type: 'booking', number: what.booking };
  return targetMove(what.target);
}
