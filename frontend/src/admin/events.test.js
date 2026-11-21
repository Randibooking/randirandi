// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  EVENTS_ADDRESS, asked, blockDays, eventAddress, eventOfAddress, categoryWord, clearOf, comingDays, dayWords, daysShown, daysWords, draft, eventBody, eventFailure, eventsQuery,
  formChanged, holidaysInTheWayQuery, hoursClear, inTheWayQuery, otherHoursFor, previewBody, removedDays, repeatHours, saveable,
  seeWords, settleOf, skipWhy, timeChanged, toldWords, whenOf, withRemoved,
} from './events.js';

const today = '2026-10-25';
const base = {
  category: 'social', asset: 3, title: ' Quiz ', description: '', start: '2026-11-02T18:00', end: '2026-11-02T20:00',
  repeat: false, weekdays: [], until: '', person: { id: 7, name: 'Sam' }, email: true, phone: false, resolved: {},
};

test('days are said short, with the year only when it is not this one', () => {
  assert.equal(dayWords('2026-10-24', today), 'Sat 24 Oct');
  assert.equal(dayWords('2027-01-04', today), 'Mon 4 Jan 2027');
  assert.equal(daysWords([0, 2]), 'Mon and Wed');
});

test('the list query follows the Schedule\'s, with category and asset only when chosen', () => {
  const filters = { when: 'coming', from: today, to: today, category: 'all', asset: 'all', q: '', dir: 'asc', page: 0 };
  const query = eventsQuery(filters);
  assert.equal(query.category, undefined);
  assert.equal(query.asset, undefined);
  assert.equal(query.when, 'coming');
  const chosen = eventsQuery({ ...filters, category: 'open_house', asset: 'none', page: 2 });
  assert.equal(chosen.category, 'open_house');
  assert.equal(chosen.asset, 'none');
  assert.equal(chosen.page, 3);
});

test('categories are keys; the word comes from the server\'s list', () => {
  assert.equal(categoryWord([{ key: 'social', label: 'Social event' }], 'social'), 'Social event');
  assert.equal(categoryWord([], 'social'), 'social');
  assert.equal(seeWords({ show_email: true, show_phone: false }), 'Name, email');
});

test('a new event starts a week from today, 18:00 to 20:00, without a contact', () => {
  const d = draft(null, today);
  assert.equal(d.start, '2026-11-01T18:00');
  assert.equal(d.end, '2026-11-01T20:00');
  assert.equal(d.person, null);
  assert.equal(d.category, 'social');
});

test('an event is read back into its form, a repeat by its first day and hours', () => {
  const e = {
    category: 'open_house', asset: { id: 3 }, title: 'Open', description: null, contact: { id: 7, name: 'Sam' },
    show_email: false, show_phone: true, repeat: { weekdays: [1], first: '2026-11-03', from: '18:00', to: '24:00', end: null },
  };
  const d = draft(e, today);
  assert.equal(d.start, '2026-11-03T18:00');
  assert.equal(d.end, '2026-11-04T00:00');
  assert.equal(d.until, '');
  assert.equal(d.phone, true);
  assert.deepEqual(repeatHours(d), { from: '18:00', to: '24:00' });
});

test('the body has a one-off\'s moments or a repeat\'s weekdays and hours', () => {
  assert.deepEqual(whenOf(base), { repeat: false, start: '2026-11-02T18:00', end: '2026-11-02T20:00' });
  const repeat = { ...base, repeat: true, weekdays: [0], until: '2026-12-07' };
  assert.deepEqual(whenOf(repeat), { repeat: true, weekdays: [0], first: '2026-11-02', until: '2026-12-07', from: '18:00', to: '20:00' });
  const body = eventBody({ ...base, resolved: { 5: 'remove', 6: { from: '18:00', to: '19:00' }, 8: undefined } });
  assert.equal(body.title, 'Quiz');
  assert.equal(body.contact, 7);
  assert.deepEqual(body.settle, { 5: 'remove', 6: { from: '18:00', to: '19:00' } });
  assert.deepEqual(settleOf({}), {});
});

test('the preview is asked for an asset and a time that runs forwards, not for a title', () => {
  assert.equal(asked(base), true);
  assert.equal(asked({ ...base, asset: '' }), false);
  assert.equal(asked({ ...base, end: base.start }), false);
  assert.equal(asked({ ...base, repeat: true }), false);
  assert.equal(asked({ ...base, repeat: true, weekdays: [0] }), true);
  assert.deepEqual(previewBody(base, 4), { asset: 3, when: whenOf(base), event: 4, settle: {} });
});

test('only the time or the asset waits for a preview; any change allows a save', () => {
  assert.equal(timeChanged({ ...base, title: 'x' }, base), false);
  assert.equal(timeChanged({ ...base, asset: 4 }, base), true);
  assert.equal(formChanged(base, base), false);
  assert.equal(formChanged({ ...base, title: 'x' }, base), true);
});

test('other hours for another event\'s day keep what the new event leaves', () => {
  const w = { date: '2026-11-02', start: '2026-11-02T17:00', end: '2026-11-02T21:00' };
  assert.deepEqual(otherHoursFor(w, base), { from: '17:00', to: '18:00' });
  assert.equal(hoursClear(w, { from: '17:00', to: '18:00' }, base), true);
  assert.equal(hoursClear(w, { from: '17:00', to: '19:00' }, base), false);
  assert.equal(hoursClear(w, 'remove', base), true);
  assert.equal(hoursClear(w, undefined, base), false);
});

test('save waits for a title, a contact and a preview with nothing but settled days in the way', () => {
  const clear = { repeat: false, refusal: null, way: [] };
  assert.equal(saveable(base, clear), true);
  assert.equal(saveable({ ...base, title: ' ' }, clear), false);
  assert.equal(saveable({ ...base, person: null }, clear), false);
  assert.equal(saveable(base, null), false);
  assert.equal(saveable({ ...base, asset: '' }, null), true);
  assert.equal(saveable(base, { ...clear, way: [{ reason: 'booked' }] }), false);
  const day = { reason: 'event', repeating: true, day: 5, date: '2026-11-02', start: '2026-11-02T17:00', end: '2026-11-02T21:00' };
  assert.equal(saveable(base, { ...clear, way: [day] }), false);
  assert.equal(saveable({ ...base, resolved: { 5: 'remove' } }, { ...clear, way: [day] }), true);
  assert.equal(saveable(base, { ...clear, refusal: 'past' }), false);
  assert.equal(saveable({ ...base, repeat: true, weekdays: [0] }, { repeat: true, takes: [] }), false);
  assert.equal(saveable({ ...base, repeat: true, weekdays: [0] }, { repeat: true, takes: ['2026-11-02'] }), true);
  assert.equal(saveable({ ...base, title: '' }, null, { timed: false }), false);
  assert.equal(saveable(base, null, { timed: false }), true);
});

test('a skipped day says why, and whom it was told to', () => {
  assert.equal(skipWhy({ reason: 'closed' }), 'Closed');
  assert.equal(skipWhy({ reason: 'booked', name: 'x' }), 'Booked');
  assert.equal(skipWhy({ reason: 'event', name: 'Quiz' }), 'Event · Quiz');
  assert.equal(toldWords({ told: null }, 'Sam', today), 'In the preview');
  assert.equal(toldWords({ told: { date: '2026-10-24', to: ['Ann'] } }, 'Sam', today), 'Email to Ann, Sat 24 Oct');
  assert.equal(toldWords({ told: { date: '2026-10-24', to: ['A', 'B'] } }, 'Sam', today), 'Email to every administrator, Sat 24 Oct');
  assert.equal(toldWords({ told: { date: '2026-10-24', to: [] } }, 'Sam', today), 'Email to Sam, Sat 24 Oct');
});

test('days are coming or past, the past newest first; removed days are not counted as coming', () => {
  const days = [
    { date: 'a', over: true }, { date: 'b', over: true }, { date: 'c', over: false }, { date: 'd', over: false, removed: true },
  ];
  assert.deepEqual(daysShown(days, 'coming').map((d) => d.date), ['c', 'd']);
  assert.deepEqual(daysShown(days, 'past').map((d) => d.date), ['b', 'a']);
  assert.deepEqual(comingDays({ days }).map((d) => d.date), ['c']);
});

test('a refusal is said in words, the event in the way by its title', () => {
  assert.equal(eventFailure({ kind: 'refused', code: 'booked' }), 'A booking holds that time.');
  assert.equal(eventFailure({ kind: 'refused', code: 'event', body: { way: [{ title: 'Quiz' }] } }), 'Quiz holds that time.');
  assert.equal(eventFailure({ kind: 'refused', code: 'unheard' }), 'The event could not be saved.');
  assert.match(eventFailure({ kind: 'offline' }), /could not be reached/);
});

test('event days in the way of a closure clear when each is a repeat\'s and removed', () => {
  assert.deepEqual(inTheWayQuery(3, '2026-11-01', '2026-11-02'), { asset: 3, first: '2026-11-01', last: '2026-11-02' });
  assert.deepEqual(holidaysInTheWayQuery(3, ['2026-12-25', '2026-12-26']), { asset: 3, dates: '2026-12-25,2026-12-26' });
  const days = [{ day: 1, repeating: true }, { day: 2, repeating: false }];
  assert.equal(clearOf([], {}), true);
  assert.equal(clearOf(days, { 1: true, 2: true }), false);
  assert.equal(clearOf([days[0]], { 1: true }), true);
  assert.equal(clearOf([days[0]], { 1: false }), false);
  assert.deepEqual(removedDays(days, { 1: true, 2: true }), [1]);
  assert.deepEqual(withRemoved({ kind: 'closure-make' }, days, { 1: true }), { kind: 'closure-make', remove_event_days: [1] });
  assert.deepEqual(withRemoved({ kind: 'closure-make' }, days, {}), { kind: 'closure-make' });
});

test('a new block\'s events in the way are shown, never removed', () => {
  const rows = blockDays({ event_days: [{ event: 4, title: 'Quiz', start: '2026-11-02T18:00', end: '2026-11-02T20:00' }] });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].repeating, false);
  assert.equal(rows[0].date, '2026-11-02');
  assert.deepEqual(blockDays(null), []);
});

test('an event has its own address, and the address says which', () => {
  assert.equal(eventAddress(12), '/admin/events/12/');
  assert.equal(eventOfAddress('/admin/events/12/'), 12);
  assert.equal(eventOfAddress('/admin/events/'), null);
  assert.equal(eventOfAddress('/admin/blocks/12/'), null);
  assert.equal(EVENTS_ADDRESS, '/admin/events/');
});
