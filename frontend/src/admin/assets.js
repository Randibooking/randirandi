// SPDX-License-Identifier: AGPL-3.0-only
// The Assets and Spaces (part 7.95): their pure logic, no DOM. The server's
// answers (randirandi/core/asset_admin.py) are the truth; what is checked here
// only spares a round trip and says why a Save is not offered.
import { LONGEST_NAME, nameOk, nameTaken } from './setup.js';

export { nameOk, nameTaken };

export const LONGEST_WORD = 80;
export const wordOk = (word) => word.trim() !== '' && word.trim().length <= LONGEST_WORD;

export const counted = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
export const titled = (word) => (word ? word.charAt(0).toUpperCase() + word.slice(1) : '');

// A floor plan without a name is called by its place among the asset's.
export const planName = (plan, plans) => plan.name || `Floor plan ${plans.findIndex((p) => p.id === plan.id) + 1}`;

export const OFFERED = { yes: 'Offered', 'no-availability': 'No availability', 'no-container': 'No container' };
export const offeredWords = (asset) => OFFERED[asset.offered] ?? '–';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
export const weekdaysText = (c) => c.weekdays.map((d) => DAYS[d]).join(' ');
const clock = (m) => `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
// A container's period as the asset's page lists it (read-only here; part 7.96 edits it).
export const periodText = (c) => (c.period === 'fixed'
  ? `${clock(c.start)} to ${clock(c.end)}${c.end >= 1440 || c.end_day ? ' next day' : ''}`
  : `${clock(c.start)}–${clock(c.end)}, ${c.interval} min`);

// --- What a Save sends: only what differs from the row, a new row what it has.

export const spaceBody = (row, { name, description, booking_page: page }) => {
  const body = {};
  if (row === null || name.trim() !== row.name) body.name = name.trim();
  if (row === null ? description.trim() !== '' : description.trim() !== row.description) body.description = description.trim();
  if (row === null ? page !== 'one_asset' : page !== row.booking_page) body.booking_page = page;
  return body;
};

const occupancy = (text) => {
  const t = String(text).trim();
  return t === '' ? null : Number(t);
};
export const occupancyOk = (text) => {
  const n = occupancy(text);
  return n === null || (Number.isInteger(n) && n >= 1 && n <= 1000000);
};

// `zone` is the organization's time zone, the one a new asset has unless told otherwise.
export const assetBody = (row, draft, zone) => {
  const body = {};
  const now = {
    name: draft.name.trim(), description: draft.description.trim(), space: draft.space === '' ? null : Number(draft.space),
    whole_space: draft.space !== '' && draft.whole_space, maximum_occupancy: occupancy(draft.maximum_occupancy),
    time_zone: draft.time_zone || zone,
  };
  const was = row === null
    ? { name: '', description: '', space: null, whole_space: false, maximum_occupancy: null, time_zone: zone }
    : { ...row, time_zone: row.time_zone };
  for (const key of Object.keys(now)) {
    if (now[key] !== was[key]) body[key] = now[key];
  }
  if (row === null) body.name = now.name;
  return body;
};

export const planBody = (row, { name, word }) => {
  const body = {};
  if (row === null || name.trim() !== row.name) body.name = name.trim();
  if (row === null || word.trim() !== row.word) body.word = word.trim();
  return body;
};

export const spotBody = (row, { name, group }) => {
  const body = {};
  if (name.trim() !== row.name) body.name = name.trim();
  const next = group === 'none' ? null : Number(group);
  if (next !== row.group) body.group = next;
  return body;
};

export const groupBody = (row, { name, word, spots }) => {
  const body = {};
  if (row === null || name.trim() !== row.name) body.name = name.trim();
  if (row === null ? word.trim() !== '' && word.trim() !== 'group' : word.trim() !== row.word) body.word = word.trim() || 'group';
  body.spots = [...spots].sort((a, b) => a - b);
  return body;
};

export const changed = (body) => Object.keys(body).length > 0;

// The spots a group sheet starts with, and whether it changes them.
export const spotsOf = (plan, group) => plan.spots.filter((s) => group && s.group === group.id).map((s) => s.id);
export const sameSpots = (a, b) => [...a].sort((x, y) => x - y).join() === [...b].sort((x, y) => x - y).join();

// Spots added at once: 1 to the most the server takes.
export const countOk = (n, most) => Number.isInteger(n) && n >= 1 && n <= most;
export const clampCount = (text, most) => Math.max(1, Math.min(most, Number(text) || 1));

// The names a spot or group would take, among the others of its floor plan.
export function spotTaken(plan, name, own) {
  const wanted = name.trim().toLowerCase();
  return wanted === '' ? null : plan.spots.find((s) => s.id !== own && s.name.toLowerCase() === wanted) ?? null;
}
export const groupTaken = (plan, name, own) => nameTaken(plan.groups, name, own);
export const planTaken = (plans, name, own) => name.trim() !== '' && nameTaken(plans, name, own);

// The spots of a floor plan in blocks: one per group, then those in none.
export function blocksOf(plan) {
  const spots = [...plan.spots].sort((a, b) => a.number - b.number);
  return [
    ...plan.groups.map((g) => ({ key: g.id, group: g, spots: spots.filter((s) => s.group === g.id) })),
    { key: 'none', group: null, spots: spots.filter((s) => !s.group) },
  ].filter((b) => b.group || b.spots.length);
}

// A floor plan beside the others of its asset needs a name.
export const planNeedsName = (plans, own) => plans.filter((p) => p.id !== own).length > 0;

const PLURAL = {
  assets: 'asset', containers: 'container', availability: 'availability', bookings: 'booking', prices: 'price',
  lines: 'booking line', spots: 'spot', spot: 'spot', closures: 'closure',
};
// What holds a row that cannot be removed, as the server counts it.
export function heldWords(used) {
  const parts = Object.entries(used ?? {}).filter(([, n]) => n > 0)
    .map(([key, n]) => (key === 'availability' ? 'availability' : `${n} ${PLURAL[key] ?? key}${n === 1 ? '' : 's'}`));
  return parts.length ? `In use: ${parts.join(', ')}.` : 'It is in use.';
}

const WORDS = {
  'invalid-name': `Give it a name of up to ${LONGEST_NAME} characters.`,
  'name-taken': 'Taken',
  'name-needed': 'An asset with more than one floor plan names each.',
  'other-unnamed': 'Name the asset\'s other floor plan first.',
  'invalid-word': `Give the word up to ${LONGEST_WORD} characters.`,
  'invalid-occupancy': 'Max occupancy is a whole number from 1.',
  'unknown-space': 'The space was not found. Reload the page.',
  'unknown-group': 'The group was not found. Reload the page.',
  'unknown-spot': 'A spot was not found. Reload the page.',
  'unknown-booking-page': 'Choose a booking page from the list.',
  'last-plan': 'An asset keeps one floor plan.',
  'last-spot': 'A floor plan keeps one spot.',
  'has-spots': 'A group with spots is not removed.',
  invalid: 'The change was not valid.',
  'bad-request': 'The request was not understood.',
  busy: 'Another change is being made. Try again in a moment.',
  'not-found': 'It was not found. Reload the page.',
};

// What a failed call says, in plain words: the model's own when it sent them.
export function assetFailure(result) {
  if (result.kind === 'offline') return 'The server could not be reached. Nothing was changed.';
  if (result.kind === 'forbidden') return 'This page is out of date. Reload it and try again.';
  if (result.code === 'in-use') return heldWords(result.body?.used);
  if (result.code === 'name-taken' && result.body?.used?.spot) return `Taken by spot ${result.body.used.spot}`;
  if (result.body?.message) return result.body.message;
  return WORDS[result.code] ?? 'The change could not be made.';
}
