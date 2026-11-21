// SPDX-License-Identifier: AGPL-3.0-only
// The Price books (part 7.97): their pure logic, no DOM. The server's answers
// (randirandi/core/price_admin.py) are the truth; what is checked here only
// spares a round trip and says why a Save is not offered.
// Amounts travel in the smallest unit of the book's currency (two decimals
// for NOK, DKK and EUR); the sheet shows and takes the main unit.
import { weekdayShort } from '../book/words.js';
import { setupFailure } from './setup.js';

export const DAY = 1440;
export const LENGTH = 'length';
export const TIME_OF_DAY = 'time of day';
export const PER_BOOKING = 'per_booking';
export const PER_SPOT = 'per_capacity_unit';
export const LARGEST_AMOUNT = 2147483647;

export const two = (n) => String(n).padStart(2, '0');
export const hm = (m) => `${two(Math.floor(m / 60))}:${two(m % 60)}`;
export const containerNo = (n) => String(n).padStart(3, '0');
// A length as the drawing words it: 90 is "1 h 30 min".
export const length = (minutes) => {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return [h && `${h} h`, m && `${m} min`].filter(Boolean).join(' ') || '0 min';
};

// Weekdays as runs, Monday first: "Mon–Thu, Sun".
export function daysText(days) {
  const set = [...new Set(days)].sort((a, b) => a - b);
  if (set.length === 7) return 'Every day';
  const runs = [];
  set.forEach((d) => {
    const r = runs[runs.length - 1];
    if (r && r[1] === d - 1) r[1] = d;
    else runs.push([d, d]);
  });
  return runs.map(([a, b]) => (a === b ? weekdayShort(a) : `${weekdayShort(a)}${b - a > 1 ? '–' : ', '}${weekdayShort(b)}`)).join(', ');
}
const endText = (end) => (end > DAY ? `${hm(end - DAY)} next day` : hm(end));
export const timesText = (c) => `${hm(c.start)}–${endText(c.end)}`;
// The longest booking: a fixed period is booked whole, and the server sets its
// maximum to the period.
export const maxOf = (c) => c.maximum;

// --- Money ---

// 24950 is "249.50", 25000 is "250": whole amounts show no decimals.
const grouped = new Intl.NumberFormat('nb-NO', { useGrouping: true });
export function figure(minor) {
  const whole = Math.trunc(minor / 100);
  const cents = Math.abs(minor % 100);
  const main = grouped.format(whole).replace(/ /g, ' ');
  return cents === 0 ? main : `${main},${two(cents)}`;
}
export const money = (minor, currency) => `${figure(minor)} ${currency}`;
// What an input shows for an amount, and the amount of what was typed: a main
// unit with up to two decimals, "249.5", "249,50" or "250"; NaN when it is not.
export const amountText = (minor) => (Number.isFinite(minor) ? (minor % 100 === 0 ? String(minor / 100) : (minor / 100).toFixed(2)) : '');
export function amountOf(text) {
  const found = /^(\d{1,9})(?:[.,](\d{1,2}))?$/.exec(String(text).trim());
  if (!found) return NaN;
  const minor = Number(found[1]) * 100 + (found[2] ? Number(found[2].padEnd(2, '0')) : 0);
  return minor <= LARGEST_AMOUNT ? minor : NaN;
}

// --- The tiers a booking is cut at ---

// How an example booking is worded: its length where the tiers go by length
// in a flexible period, otherwise the times it runs between. The price is the
// server's (pricing.py), never calculated here.
export const exampleText = (e, c, g) => (e.tiers_go_by === LENGTH && c.period === 'flexible'
  ? length(g.length) : `${hm(g.start)}–${endText(g.start + g.length)}`);

// --- Drafts: what a sheet edits ---

export const steps = (from, to) => Array.from({ length: Math.max(0, Math.floor((to - from) / 15) + 1) }, (_, i) => from + i * 15);
// Where the tiers of a time-of-day price end at first: the period's end, or
// the end of the day (24:00) where the period runs past it. A tier may end at
// 24:00 at the latest.
export const lastTimeOfDay = (c) => Math.min(c.end, DAY);

const tierDraft = (t) => ({ up_to: t.up_to, paid_per: t.paid_per, text: amountText(t.amount) });
export const draftOf = (entry) => ({
  group: entry.group, tiers_go_by: entry.tiers_go_by, billing_basis: entry.billing_basis, tiers: entry.tiers.map(tierDraft),
});
// A new price: one tier reaching the longest booking, at zero.
export function freshDraft(c, group, basis) {
  return {
    group, tiers_go_by: LENGTH, billing_basis: basis,
    tiers: [{ up_to: maxOf(c), paid_per: c.period === 'fixed' ? maxOf(c) : c.interval, text: '0' }],
  };
}
// The drafts of a container in a category: the normal price and one for each
// group that has its own (null where it has none).
export function draftsOf(c, category) {
  const found = (group) => c.entries.find((e) => e.category === category && e.group === group);
  return {
    normal: found(null) ? draftOf(found(null)) : freshDraft(c, null, PER_BOOKING),
    existed: !!found(null),
    ...Object.fromEntries(c.groups.map((g) => [g.id, found(g.id) ? draftOf(found(g.id)) : null])),
  };
}
// The tiers of a draft in the smallest unit.
export const tiersOf = (draft) => draft.tiers.map((t) => ({ up_to: t.up_to, paid_per: t.paid_per, amount: amountOf(t.text) }));

// What the sheet sends: the normal price first, then each group's own, with
// the stamp of the prices the sheet saw.
export function saveBody(drafts, groups, stamp) {
  const own = groups.filter((g) => drafts[g.id]);
  const entry = (d) => ({ group: d.group, tiers_go_by: d.tiers_go_by, billing_basis: d.billing_basis, tiers: tiersOf(d) });
  return { entries: [entry(drafts.normal), ...own.map((g) => entry(drafts[g.id]))], stamp };
}

// The refusals of one draft before it is sent: the server's, in the drawing's
// words. `label` names the price a refusal belongs to when there are several.
export function refusals(d, c, label) {
  const out = [];
  const t = tiersOf(d);
  const last = t[t.length - 1].up_to;
  const time = d.tiers_go_by === TIME_OF_DAY;
  if (time && c.end > DAY) out.push('Time of day does not price a period over midnight.');
  if (t.some((x, i) => i > 0 && x.up_to <= t[i - 1].up_to)) out.push('Each tier reaches past the one before.');
  if (t.some((x) => !Number.isFinite(x.amount) || x.amount < 0)) out.push('Each tier has an amount of 0 or more, with up to two decimals.');
  if (time && t.some((x) => x.up_to > DAY)) out.push('A tier ends by 24:00.');
  if (!time && last < maxOf(c)) out.push(`The tiers stop at ${length(last)}; the longest booking is ${length(maxOf(c))}.`);
  if (time && c.end <= DAY && last < c.end) out.push(`The tiers stop at ${hm(last)}; the period ends at ${hm(c.end)}.`);
  return out.map((x) => (label ? `${label}: ${x}` : x));
}

// All the refusals of a sheet: each price, and the group rule (a price for a
// group makes the normal price per spot too).
export function sheetRefusals(drafts, c, word) {
  const own = c.groups.filter((g) => drafts[g.id]);
  const out = [...refusals(drafts.normal, c, own.length ? 'Normal price' : ''),
    ...own.flatMap((g) => refusals(drafts[g.id], c, g.name))];
  if (own.length && drafts.normal.billing_basis !== PER_SPOT) out.push(`With a price for a group, the normal price is per ${word} too.`);
  return out;
}

// Switching what the tiers go by starts the tiers again, from the first.
export function goingBy(d, goesBy, c) {
  const time = goesBy === TIME_OF_DAY;
  return { ...d, tiers_go_by: goesBy, tiers: [{ ...d.tiers[0], up_to: time ? lastTimeOfDay(c) : maxOf(c) }] };
}
// A tier added after the last one.
export function withTier(d, c) {
  const l = d.tiers[d.tiers.length - 1];
  const ceiling = d.tiers_go_by === TIME_OF_DAY ? DAY : maxOf(c);
  return { ...d, tiers: [...d.tiers, { ...l, up_to: Math.min(l.up_to + l.paid_per, ceiling) }] };
}

// --- The list ---

// The booking categories of a container with a price in its book, and those without.
export const priced = (c, category) => c.entries.some((e) => e.category === category && e.group === null);
export const categoriesOf = (c, categories) => c.categories.map((id) => categories.find((x) => x.id === id)).filter(Boolean);
export const missing = (c, categories) => categoriesOf(c, categories).filter((x) => !priced(c, x.id)).map((x) => x.name);
export const pricedNames = (c, categories) => categoriesOf(c, categories).filter((x) => priced(c, x.id)).map((x) => x.name).join(', ') || '–';
export const spotWord = (c) => (c.mode === 'shared' ? (c.unit || 'spot') : 'spot');

// A book name is 1 to 200 characters, trimmed, taken whatever its case.
export const bookNameTaken = (books, name, ownId = null) => {
  const wanted = name.trim().toLowerCase();
  return wanted !== '' && books.some((b) => b.id !== ownId && b.name.toLowerCase() === wanted);
};
export const bookBody = (book, name, currency) => {
  const body = {};
  if (book === null || name.trim() !== book.name) body.name = name.trim();
  if (book === null || currency !== book.currency) body.currency = currency;
  return body;
};

// --- Words for what the server refuses ---

const WORDS = {
  'currency-fixed': 'It has a price, so its currency stays as it is; make another price book for the other currency.',
  'unknown-currency': 'Choose a currency from the list.',
  'container-without-payment': 'The container is without payment: it has no price book.',
  'category-not-on-container': 'The container does not apply to this booking category.',
  'group-not-on-floor-plan': 'The group is not on the floor plan the container books.',
  'billing-basis': 'An exclusive container books the whole asset, so it is priced per booking.',
  'group-twice': 'A group has one price.',
  'needs-normal-price': 'A price for a group needs the normal price, for the spots in no group.',
  'groups-per-spot': 'With a price for a group, the normal price is per spot too.',
  'no-tier': 'It has no price tier.',
  'tiers-overlap': 'Each tier reaches past the one before.',
  'tiers-incomplete': 'The tiers stop short of the longest booking.',
  'invalid-tier': 'A tier is not valid.',
  'invalid-tiers-go-by': 'Choose length or time of day.',
  'invalid-billing-basis': 'Choose per booking or per spot.',
};

// What a failed call says, in plain words: the model's own reason when the
// server gave one.
export function priceFailure(result) {
  const reason = result.body?.reason;
  if (reason && result.code !== 'in-use') return reason;
  return WORDS[result.code] ?? setupFailure(result);
}
