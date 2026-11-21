// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  bookDay, categoryWord, daysArePast, daysOf, eventList, eventsAddress, eventsQuery, placeChoices, placeName, readEventsAddress, shortDays, whenWords,
} from './events.js';
import { setLabels } from './labels.js';

const HALL = { id: 4, name: 'Hall' };
const weekend = {
  id: 1, category: 'open_house', title: 'Weekend drop-in', asset: HALL, description: '', contact: null, start: null, end: null,
  repeat: { weekdays: [5, 6], from: '10:00', to: '14:00', first: '2026-10-03', end: null },
  days: [
    { date: '2026-10-31', from: '10:00', to: '14:00' }, { date: '2026-11-01', cancelled: true },
    { date: '2026-11-07', from: '12:00', to: '14:00' },
  ],
};
const lunch = { id: 2, category: 'social', title: 'Lunch', asset: HALL, repeat: null, days: [], start: '2026-10-28T12:00', end: '2026-10-28T15:00' };
const party = { id: 3, category: 'social', title: 'Party', asset: null, repeat: null, days: [], start: '2026-12-11T19:00', end: '2026-12-12T01:00' };
const board = { id: 4, category: 'organization', title: 'Board', asset: HALL, repeat: null, days: [], start: '2026-11-11T18:00', end: '2026-11-11T19:00' };

test('the category is in the organization\'s words, the default otherwise', () => {
  setLabels({});
  assert.equal(categoryWord('volunteer_work'), 'Volunteer work');
  setLabels({ 'event.category.volunteer_work': 'Dugnad' });
  assert.equal(categoryWord('volunteer_work'), 'Dugnad');
  setLabels({});
});

test('whenWords: a repeat\'s rule, a day, both dates across midnight', () => {
  assert.equal(whenWords(weekend), 'Every Saturday and Sunday, 10:00–14:00');
  assert.equal(whenWords(board), 'Wednesday 11 November, 18:00–19:00');
  assert.equal(whenWords(party), 'Friday 11 December, 19:00 – Saturday 12 December, 01:00');
});

test('shortDays: a pair, a run, a list', () => {
  assert.equal(shortDays([6, 5]), 'Sat and Sun');
  assert.equal(shortDays([0, 1, 2, 3, 4]), 'Mon to Fri');
  assert.equal(shortDays([1, 3, 4]), 'Tue, Thu, Fri');
});

test('the list: repeats once under Every week, then months, soonest first; past newest first', () => {
  const coming = eventList([party, weekend, board, lunch], false, '2026-10-25');
  assert.deepEqual(coming.weekly.map((row) => [row.event.id, row.hours]), [[1, 'Sat and Sun 10:00–14:00']]);
  assert.deepEqual(coming.months.map((month) => [month.title, month.rows.map((row) => row.event.id)]),
    [['October', [2]], ['November', [4]], ['December', [3]]]);
  assert.equal(coming.count, 4);
  assert.equal(coming.first.id, 1);
  const past = eventList([lunch, board], true, '2026-12-25');
  assert.deepEqual(past.months.map((month) => month.rows.map((row) => row.event.id)), [[4], [2]]);
  const nextYear = eventList([{ ...board, start: '2027-01-05T18:00', end: '2027-01-05T19:00' }], false, '2026-12-25');
  assert.equal(nextYear.months[0].title, 'January 2027');
  const none = eventList([], false, '2026-10-25');
  assert.deepEqual([none.count, none.first], [0, null]);
});

test('a repeat\'s days are the server\'s: the cancelled one stays, a skipped one was never sent', () => {
  assert.deepEqual(daysOf(weekend, 2).map((day) => day.date), ['2026-10-31', '2026-11-01']);
  assert.equal(daysOf(weekend, 6)[1].cancelled, true);
  assert.deepEqual(daysOf({ repeat: null }, 6), []);
  assert.equal(daysArePast(weekend, '2026-11-08'), true);
  assert.equal(daysArePast(weekend, '2026-11-02'), false);
  assert.equal(daysArePast({ days: [] }, '2026-11-02'), false);
});

test('See it in Book opens the day: a one-off\'s own, a repeat\'s next that is not cancelled', () => {
  assert.equal(bookDay(lunch, '2026-10-25'), '2026-10-28');
  assert.equal(bookDay(weekend, '2026-11-01'), '2026-11-07');
  assert.equal(bookDay(weekend, '2026-12-01'), '2026-10-31');
  assert.equal(bookDay({ ...weekend, days: [] }, '2026-12-01'), '2026-10-03');
});

test('the Filter is the address and the question to the server', () => {
  const filter = { when: 'past', categories: ['social', 'open_house'], where: '4' };
  const search = eventsAddress(filter);
  assert.equal(search, 'when=past&category=social%2Copen_house&where=4');
  assert.deepEqual(readEventsAddress(`?${search}`), filter);
  assert.deepEqual(eventsQuery(filter), { when: 'past', category: ['social', 'open_house'], asset: '4' });
  const nothing = { when: 'coming', categories: [], where: 'all' };
  assert.equal(eventsAddress(nothing), '');
  assert.deepEqual(eventsQuery(nothing), { when: undefined, category: undefined, asset: undefined });
  assert.deepEqual(readEventsAddress(''), nothing);
});

test('an address that names no category or place is left out', () => {
  assert.deepEqual(readEventsAddress('?category=party,social&where=../x&when=later'),
    { when: 'coming', categories: ['social'], where: 'all' });
  assert.equal(readEventsAddress('?where=none').where, 'none');
});

test('where: every place with events, then Elsewhere; a chip is named by its place', () => {
  assert.deepEqual(placeChoices([HALL]), [['all', 'Everywhere'], ['4', 'Hall'], ['none', 'Elsewhere']]);
  assert.equal(placeName('4', [HALL]), 'Hall');
  assert.equal(placeName('none', []), 'Elsewhere');
  assert.equal(placeName('9', [HALL]), null);
});
