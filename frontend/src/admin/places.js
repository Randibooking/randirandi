// SPDX-License-Identifier: AGPL-3.0-only
// The administrators' places in their three groups: key (the address under
// /admin/) and label. The keys and labels match randirandi/core/admin_views.py.
export const GROUPS = [
  ['Daily', [
    ['bookings', 'Bookings'],
    ['schedule', 'Schedule'],
    ['blocks', 'Blocks'],
    ['events', 'Events'],
    ['members', 'Members'],
    ['payments', 'Payments'],
  ]],
  ['Setup', [
    ['assets', 'Assets and availability'],
    ['containers', 'Containers'],
    ['types', 'Booking types and categories'],
    ['roles', 'Roles'],
    ['prices', 'Price books'],
    ['organization', 'Organization'],
  ]],
  ['Records', [
    ['audit', 'Audit log'],
  ]],
];

export const labelOf = (key) => GROUPS.flatMap(([, places]) => places).find(([k]) => k === key)?.[1] ?? '';
export const hrefOf = (key) => `/admin/${key}/`;

// The word of the button back to the booking pages, and where it goes.
export const BACK_TO_BOOKING = 'Back to Booking';
export const BOOKING_HREF = '/book/';

// The tab's title for a place, as the server titles its page (admin.html).
export const titleOf = (key) => `${labelOf(key)} · Admin · RandiRandi`;

// Up to two capital letters from a name, for the avatar.
export function initials(name) {
  const words = String(name ?? '').split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? [words[0], words[words.length - 1]] : words.slice(0, 1);
  return letters.map((w) => Array.from(w)[0].toUpperCase()).join('');
}
