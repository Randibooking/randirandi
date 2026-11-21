// SPDX-License-Identifier: AGPL-3.0-only
// The Booking types and categories and the Roles (part 7.94): their pure logic,
// no DOM. The server's answers (randirandi/core/setup_admin.py) are the
// truth; what is checked here only spares a round trip and says why a Save is
// not offered.

export const TYPES = 'booking-types';
export const CATEGORIES = 'booking-categories';

// A name is 1 to 200 characters, trimmed; one list holds a name once, whatever
// its case (the server compares the same way).
export const LONGEST_NAME = 200;
export const nameOk = (name) => name.trim() !== '' && name.trim().length <= LONGEST_NAME;
export const nameTaken = (rows, name, ownId = null) => {
  const wanted = name.trim().toLowerCase();
  return wanted !== '' && rows.some((row) => row.id !== ownId && row.name.toLowerCase() === wanted);
};

// The words of each icon key a category may have (CATEGORY_ICONS in
// randirandi/limits.py; the server sends the list it takes).
export const ICON_WORDS = { person: 'Person', work: 'Work', building: 'Building', home: 'Home', group: 'Group' };
export const NO_ICON = 'None';
export const iconWords = (key) => (key ? (ICON_WORDS[key] ?? key) : NO_ICON);

// What a type or category change sends: only what differs from the row.
export function itemBody(list, row, { name, description, icon }) {
  const body = {};
  if (row === null || name.trim() !== row.name) body.name = name.trim();
  if (list === TYPES && (row === null ? description.trim() !== '' : description.trim() !== row.description)) {
    body.description = description.trim();
  }
  if (list === CATEGORIES && (row === null ? icon !== '' : icon !== row.icon)) body.icon = icon;
  return body;
}
export const itemChanged = (body) => Object.keys(body).length > 0;

// Each list in two columns of the drawing: what holds a row.
export const count = (n) => (n ? String(n) : '–');
export const containersWords = (n) => `${n} container${n === 1 ? '' : 's'}`;

const PLURAL = { containers: 'container', roles: 'role', prices: 'price', bookings: 'booking', members: 'member' };
// What holds a row that cannot be removed, as the server counts it.
export function usedWords(used) {
  const parts = Object.entries(used ?? {}).filter(([, n]) => n > 0)
    .map(([key, n]) => `${n} ${PLURAL[key] ?? key}${n === 1 ? '' : 's'}`);
  return parts.length ? `In use: ${parts.join(', ')}.` : 'It is in use.';
}

const WORDS = {
  'invalid-name': 'Give it a name of up to 200 characters.',
  'name-taken': 'Taken',
  'unknown-icon': 'Choose an icon from the list.',
  'unknown-category': 'A booking category was not found. Reload the page.',
  'no-category': 'A role needs a booking category.',
  'standard-admin': 'The standard role gives no admin access.',
  'standard-needed': 'One role stays the standard role.',
  'last-administrator': 'One role keeps admin access.',
  'is-standard': 'The standard role is not removed.',
  'has-members': 'A member has this role.',
  invalid: 'The change was not valid.',
  'bad-request': 'The request was not understood.',
  busy: 'Another change is being made. Try again in a moment.',
  'not-found': 'It was not found. Reload the page.',
};

// What a failed call says, in plain words.
export function setupFailure(result) {
  if (result.kind === 'offline') return 'The server could not be reached. Nothing was changed.';
  if (result.kind === 'forbidden') return 'This page is out of date. Reload it and try again.';
  if (result.code === 'in-use') return usedWords(result.body?.used);
  return WORDS[result.code] ?? 'The change could not be made.';
}

// --- Roles ---

export const emptyRole = () => ({ id: null, name: '', categories: [], admin_access: false, standard: false });

// A tick on a category: the categories stay in the order the list has.
export function ticked(order, chosen, id) {
  return chosen.includes(id) ? chosen.filter((x) => x !== id) : order.filter((x) => x === id || chosen.includes(x));
}

// The refusals of a role as it would be saved, among the roles as they would
// stand: the drawing's three, and the one the setup file makes (a category).
export function roleRefusals(roles, draft) {
  const next = draft.id === null ? [...roles, draft] : roles.map((r) => (r.id === draft.id ? draft : r));
  const list = next.map((r) => (draft.standard && r !== draft && r.id !== draft.id ? { ...r, standard: false } : r));
  const refusals = [];
  if (draft.standard && draft.admin_access) refusals.push(WORDS['standard-admin']);
  if (!list.some((r) => r.admin_access)) refusals.push(WORDS['last-administrator']);
  if (!list.some((r) => r.standard)) refusals.push(WORDS['standard-needed']);
  if (draft.categories.length === 0) refusals.push(WORDS['no-category']);
  return refusals;
}

// What a role's Save sends: all four for a new role, what differs for one that
// is changed; null when nothing does.
export function roleBody(role, draft) {
  const full = { name: draft.name.trim(), categories: draft.categories, admin_access: draft.admin_access, standard: draft.standard };
  if (role === null) return full;
  const body = {};
  if (full.name !== role.name) body.name = full.name;
  if (full.categories.join() !== role.categories.join()) body.categories = full.categories;
  if (full.admin_access !== role.admin_access) body.admin_access = full.admin_access;
  if (full.standard !== role.standard) body.standard = full.standard;
  return itemChanged(body) ? body : null;
}

// The categories of a role by name, in the categories' order, as the drawing's
// list shows them.
export function categoryNames(role, categories) {
  return categories.filter((c) => role.categories.includes(c.id)).map((c) => c.name).join(', ') || '–';
}

export const membersWords = (n) => `${n} member${n === 1 ? '' : 's'}`;
