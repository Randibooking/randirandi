// SPDX-License-Identifier: AGPL-3.0-only
// The Organization place (part 7.99): its pure logic, no DOM. The server's
// answers (randirandi/core/organization_admin.py) are the truth; what is checked
// here only spares a round trip and says why a Save is not offered. The
// organization's own words (labels) are not on this screen.

export const NO_LIMIT = 'No limit';
export const LONGEST_NAME = 200;
export const MOST_TAX = 10000;
// The most a retention may be: the column's (a small integer).
export const MOST_MONTHS = 32767;

export const monthsWords = (n) => `${n} month${n === 1 ? '' : 's'}`;
export const membersWords = (n) => `${n} member${n === 1 ? '' : 's'}`;
export const bookingsWords = (n) => `${n} booking${n === 1 ? '' : 's'}`;
export const limitWords = (n) => (n === null || n === undefined ? NO_LIMIT : String(n));
export const limitMonthsWords = (n) => (n === null || n === undefined ? NO_LIMIT : monthsWords(n));

// The tax rate in hundredths of a percent: 2500 is "25 %", 1250 "12.5 %".
export function taxText(rate) {
  const whole = Math.floor(rate / 100);
  const hundredths = rate % 100;
  if (hundredths === 0) return String(whole);
  return `${whole}.${String(hundredths).padStart(2, '0').replace(/0$/, '')}`;
}
export const taxWords = (rate) => `${taxText(rate)} %`;

// A percent as typed ("25", "12,5", "0.25") in hundredths of a percent, or null.
export function parseTax(text) {
  const found = /^(\d{1,3})(?:[.,](\d{1,2}))?$/.exec(text.trim());
  if (!found) return null;
  const rate = Number(found[1]) * 100 + Number((found[2] ?? '').padEnd(2, '0') || 0);
  return rate <= MOST_TAX ? rate : null;
}

// A whole number as typed, or null. `least` is the smallest it may be.
export function parseWhole(text, least, most = MOST_MONTHS) {
  if (!/^\d{1,9}$/.test(text.trim())) return null;
  const n = Number(text.trim());
  return n >= least && n <= most ? n : null;
}

// One plain address: no name, no list, no line break (the server's rule, `contact_fault`).
export const EMAIL = /^[^@\s<>",;]+@[^@\s<>",;]+\.[^@\s<>",;]+$/;
export const MOST_DAYS = 365;
export const daysWords = (n) => `${n} day${n === 1 ? '' : 's'}`;
export const nameOk = (name) => name.trim() !== '' && name.trim().length <= LONGEST_NAME;

// The sheet's fields, as texts, from the place the server sent.
export function draftOf(place) {
  const text = (n) => (n === null || n === undefined ? '' : String(n));
  return {
    name: place.name,
    contact_email: place.contact_email,
    free_cancellation_days: text(place.free_cancellation_days),
    standard_role: String(place.standard_role ?? ''),
    language: place.language,
    max_bookings_a_day: text(place.max_bookings_a_day),
    book_ahead_months: text(place.book_ahead_months),
    tax_rate: taxText(place.tax_rate),
    member_retention_months: text(place.member_retention_months),
    booking_retention_months: text(place.booking_retention_months),
    time_zone: place.time_zone,
    currency: place.currency,
  };
}

const WORDS = {
  name: 'Give it a name of up to 200 characters.',
  contact_email: 'Enter one email address, without a name.',
  free_cancellation_days: 'A whole number of days, from 0 to 365.',
  standard_role: 'Choose a role.',
  language: 'Choose a language.',
  max_bookings_a_day: 'A whole number, 1 or more, or empty.',
  book_ahead_months: 'A whole number, 1 or more, or empty.',
  tax_rate: 'From 0 to 100, up to two decimals.',
  member_retention_months: 'A whole number of months, 1 or more.',
  booking_retention_months: 'A whole number of months, 1 or more.',
  time_zone: 'Choose a time zone.',
  currency: 'Choose a currency.',
};

// What a draft means: the values the server takes, and the words for each field
// that cannot be one. Empty limits are "no limit".
export function readDraft(place, draft) {
  const values = {};
  const faults = {};
  const take = (key, value) => { if (value === null) faults[key] = WORDS[key]; else values[key] = value; };
  take('name', nameOk(draft.name) ? draft.name.trim() : null);
  take('contact_email', EMAIL.test(draft.contact_email.trim()) ? draft.contact_email.trim() : null);
  take('free_cancellation_days', parseWhole(draft.free_cancellation_days, 0, MOST_DAYS));
  take('standard_role', place.roles.some((r) => String(r.id) === draft.standard_role && !r.admin_access)
    ? Number(draft.standard_role) : null);
  take('language', place.languages.some((l) => l.code === draft.language) ? draft.language : null);
  for (const key of ['max_bookings_a_day', 'book_ahead_months']) {
    if (draft[key].trim() === '') values[key] = null;
    else take(key, parseWhole(draft[key], 1));
  }
  take('tax_rate', parseTax(draft.tax_rate));
  take('member_retention_months', parseWhole(draft.member_retention_months, 1));
  take('booking_retention_months', parseWhole(draft.booking_retention_months, 1));
  take('time_zone', place.time_zones.includes(draft.time_zone) ? draft.time_zone : null);
  take('currency', place.currencies.includes(draft.currency) ? draft.currency : null);
  return { values, faults };
}

// What a Save sends: the version the sheet was opened on, and what differs;
// null when nothing does or a field cannot be saved.
export function saveBody(place, draft) {
  const { values, faults } = readDraft(place, draft);
  if (Object.keys(faults).length) return null;
  const changed = Object.keys(values).filter((key) => values[key] !== place[key]);
  if (!changed.length) return null;
  return { version: place.version, ...Object.fromEntries(changed.map((key) => [key, values[key]])) };
}

const REFUSALS = {
  'code-refused': 'The code was not accepted.',
  'code-limited': 'Too many codes were not accepted. Wait 15 minutes and try again.',
  'unknown-role': 'That role was not found. Reload the page.',
  'standard-admin': 'The standard role gives no admin access.',
  stale: 'Someone else changed the details.',
  'bad-request': 'The request was not understood.',
  busy: 'Another change is being made. Try again in a moment.',
  invalid: 'The change was not valid.',
};

const upper = (text) => (text ? text[0].toUpperCase() + text.slice(1) : text);

// What a failed save says: the field it belongs to (or null) and its words,
// the server's reason for a rule it broke.
export function failure(result) {
  if (result.kind === 'offline') return { field: null, text: 'The server could not be reached. Nothing was changed.' };
  if (result.kind === 'forbidden') return { field: null, text: 'This page is out of date. Reload it and try again.' };
  const code = result.code ?? '';
  if (code.startsWith('invalid-')) {
    const field = code.slice('invalid-'.length).replace(/-/g, '_');
    if (field in WORDS) return { field, text: upper(result.body?.reason ?? '') || WORDS[field] };
  }
  return { field: null, text: REFUSALS[code] ?? 'The change could not be made.' };
}

// The numbers a lowered retention asks to be confirmed, as the server counted.
export function eraseWords(erase) {
  return `${membersWords(erase.members)} and ${bookingsWords(erase.bookings)} will be erased at the next run.`;
}
