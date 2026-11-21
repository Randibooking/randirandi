// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DAY, categoriesBody, categoriesChanged, categoriesDraft, containerFailure, daysText, detailsBody, detailsDraft, endDayChoices,
  endOf, endText, freshHours, hoursGroups, hoursList, inUseWords, isoWeek, length, lengthChoices, mondayOf, newBody, newDraft,
  newReady, noWeeks, ownText, periodLength, tickCategory, timeBody, timeDraft, timesText, usesText, weekChoices, weekText,
  weeksText,
} from './containers.js';

const assets = [
  { id: 1, name: 'Hall', floor_plans: [{ id: 10, name: 'Floor plan 1', capacity: 8, unit: 'seat' }, { id: 11, name: 'Tables', capacity: 1, unit: 'table' }] },
  { id: 2, name: 'Sauna', floor_plans: [] },
];
const container = (more = {}) => ({
  id: 5, number: 2, asset: 1, booking_type: 3, period_type: 'flexible', period_start: 600, period_end: 1320, end_day: 0,
  weekdays: [0, 1, 2, 3, 4], interval: 60, minimum_length: 60, maximum_length: 180, first_week: null, last_week: null,
  every_weeks: 1, status: 'enabled', uses: null, price_book: 1,
  categories: [{ id: 7, hours: [], priced: true, to_come: 0 }, { id: 8, hours: [{ weekday: 0, start: 600, end: 720 }, { weekday: 1, start: 600, end: 720 }], priced: false, to_come: 1 }],
  to_come: 1, last_to_come: null, locked: true, removable: false, ...more,
});

test('a length, the times and the days read as the drawing has them', () => {
  assert.equal(length(30), '30 min');
  assert.equal(length(60), '1 h');
  assert.equal(length(90), '1 h 30 min');
  assert.equal(timesText(container()), '10:00–22:00');
  assert.equal(timesText(container({ period_start: 1320, period_end: 600, end_day: 1 })), '22:00–10:00 next day');
  assert.equal(endText(container({ end_day: 2, period_end: 0 })), '00:00 in 2 days');
  assert.equal(periodLength(container({ period_start: 1320, period_end: 600, end_day: 1 })), 720);
  assert.equal(daysText([0, 1, 2, 3, 6]), 'Mon–Thu, Sun');
  assert.equal(daysText([0, 2, 4]), 'Mon, Wed, Fri');
  assert.equal(daysText([0, 1, 2, 3, 4, 5, 6]), 'Every day');
});

test('calendar weeks are ISO weeks counted from their Monday', () => {
  assert.deepEqual(isoWeek('2027-03-08'), { y: 2027, w: 10 });
  assert.deepEqual(isoWeek('2026-12-28'), { y: 2026, w: 53 });
  assert.deepEqual(isoWeek('2027-01-04'), { y: 2027, w: 1 });
  assert.equal(weekText('2027-03-08'), 'week 10, 2027');
  assert.equal(mondayOf('2027-03-14'), '2027-03-08');
  assert.equal(mondayOf('2027-03-08'), '2027-03-08');
  assert.equal(weeksText(container()), 'Every week');
  assert.equal(weeksText(container({ every_weeks: 2, first_week: '2027-03-08', last_week: '2027-06-28' })),
    'Every 2 weeks from week 10, 2027 to week 26, 2027');
  assert.equal(noWeeks(container()), true);
  assert.equal(noWeeks(container({ last_week: '2027-06-28' })), false);
  const choices = weekChoices('2027-03-10', '2026-01-05');
  assert.equal(choices[0], '2026-01-05');
  assert.equal(choices[1], '2027-03-08');
  assert.equal(choices.length, 106);
  assert.equal(weekChoices('2027-03-10', '2027-03-15').length, 105);
});

test('what a container uses', () => {
  assert.equal(usesText(container(), assets), 'Whole asset');
  assert.equal(usesText(container({ uses: 10 }), assets), 'Floor plan 1 · 8 seats');
  assert.equal(usesText(container({ uses: 11 }), assets), 'Tables · 1 table');
  assert.equal(usesText(container({ uses: 99 }), assets), '–');
});

test('own hours: one stretch for each weekday on the server, the same times grouped on the sheet', () => {
  const hours = [{ weekday: 1, start: 600, end: 720 }, { weekday: 0, start: 600, end: 720 }, { weekday: 4, start: 840, end: 960 }];
  const groups = hoursGroups(hours);
  assert.deepEqual(groups, [{ weekdays: [0, 1], start: 600, end: 720 }, { weekdays: [4], start: 840, end: 960 }]);
  assert.deepEqual(hoursList(groups), [
    { weekday: 0, start: 600, end: 720 }, { weekday: 1, start: 600, end: 720 }, { weekday: 4, start: 840, end: 960 },
  ]);
  assert.equal(ownText({ hours }), 'Mon, Tue 10:00–12:00, Fri 14:00–16:00');
  assert.equal(ownText({ hours: [] }), '–');
  assert.deepEqual(hoursList([{ weekdays: [2, 2, 1], start: 0, end: 60 }]).map((h) => h.weekday), [1, 2]);
});

test('the details sheet sends only what differs, and the floor plan with a new asset', () => {
  const c = container({ uses: 10 });
  assert.deepEqual(detailsBody(c, detailsDraft(c)), {});
  assert.deepEqual(detailsBody(c, { ...detailsDraft(c), booking_type: 4 }), { booking_type: 4 });
  assert.deepEqual(detailsBody(c, { ...detailsDraft(c), price_book: null }), { price_book: null });
  assert.deepEqual(detailsBody(c, { ...detailsDraft(c), uses: null }), { uses: null });
  assert.deepEqual(detailsBody(c, { ...detailsDraft(c), asset: 2, uses: null }), { asset: 2, uses: null });
});

test('the end is a clock time before 24:00 and the days after the start day', () => {
  assert.deepEqual(endOf({ endTime: 1440, endDay: 0 }), { end_day: 1, period_end: 0 });
  assert.deepEqual(endOf({ endTime: 600, endDay: 1 }), { end_day: 1, period_end: 600 });
  assert.deepEqual(endOf({ endTime: 1440, endDay: 1 }), { end_day: 2, period_end: 0 });
  assert.deepEqual(endDayChoices(9).map(([n]) => n), [0, 1, 2, 3, 4, 5, 6, 9]);
  assert.equal(endDayChoices(0)[1][1], 'Next day');
});

test('the time sheet sends what differs; a fixed container gets lengths from the system', () => {
  const c = container();
  assert.deepEqual(timeBody(c, timeDraft(c)), {});
  assert.deepEqual(timeBody(c, { ...timeDraft(c), period_start: 540 }), { period_start: 540 });
  assert.deepEqual(timeBody(c, { ...timeDraft(c), endTime: 1440 }), { period_end: 0, end_day: 1 });
  assert.deepEqual(timeBody(c, { ...timeDraft(c), weekdays: [0, 6] }), { weekdays: [0, 6] });
  assert.deepEqual(timeBody(c, { ...timeDraft(c), maximum_length: 120, last_week: '2027-06-28' }),
    { maximum_length: 120, last_week: '2027-06-28' });
  assert.deepEqual(timeBody(c, { ...timeDraft(c), first_week: '2027-03-08', every_weeks: 2 }),
    { first_week: '2027-03-08', every_weeks: 2 });
  // Fixed: no lengths are sent.
  assert.deepEqual(timeBody(c, { ...timeDraft(c), period_type: 'fixed', maximum_length: 60 }), { period_type: 'fixed' });
  // Back to flexible: all three lengths, the server has none to compare with.
  const fixed = container({ period_type: 'fixed', interval: 720, minimum_length: 720, maximum_length: 720 });
  assert.deepEqual(timeBody(fixed, { ...timeDraft(fixed), period_type: 'flexible', interval: 60, minimum_length: 60, maximum_length: 60 }),
    { period_type: 'flexible', interval: 60, minimum_length: 60, maximum_length: 60 });
});

test('a flexible period offers lengths in whole intervals up to a day, and the one it has', () => {
  const d = { ...timeDraft(container()), interval: 120, endTime: 1320, endDay: 0 };
  assert.deepEqual(lengthChoices(d)(120).map(([m]) => m), [120, 240, 360, 480, 600, 720]);
  assert.deepEqual(lengthChoices(d)(90).map(([m]) => m), [90, 120, 240, 360, 480, 600, 720]);
  const long = { ...d, interval: 600, endTime: 600, endDay: 3 };
  assert.deepEqual(lengthChoices(long)(600).map(([m]) => m), [600, 1200]);
  assert.equal(DAY, 1440);
});

test('the categories sheet: ticked ids in the list order, own hours of the ticked, one body', () => {
  const c = container();
  const draft = categoriesDraft(c);
  assert.deepEqual(draft.ids, [7, 8]);
  assert.deepEqual(draft.own[8], [{ weekdays: [0, 1], start: 600, end: 720 }]);
  assert.equal(categoriesChanged(c, draft), false);
  assert.deepEqual(categoriesBody(draft), {
    seen: [{ category: 7, hours: [] }, { category: 8, hours: [{ weekday: 0, start: 600, end: 720 }, { weekday: 1, start: 600, end: 720 }] }],
    categories: [
      { category: 7, hours: [] },
      { category: 8, hours: [{ weekday: 0, start: 600, end: 720 }, { weekday: 1, start: 600, end: 720 }] },
    ],
  });
  const order = [7, 8, 9];
  const added = tickCategory(draft, order, 9);
  assert.deepEqual(added.ids, [7, 8, 9]);
  assert.equal(categoriesChanged(c, added), true);
  const without = tickCategory(draft, order, 8);
  assert.deepEqual(without.ids, [7]);
  assert.deepEqual(without.seen, draft.seen);
  assert.equal('8' in without.own, false);
  assert.deepEqual(tickCategory(tickCategory(draft, order, 7), order, 7).ids, [7, 8]);
  // Own hours switched on start from the container's.
  assert.deepEqual(freshHours(c), { weekdays: [0, 1, 2, 3, 4], start: 600, end: 1320 });
  assert.deepEqual(freshHours(container({ end_day: 1, period_end: 600 })).end, 1440);
  const own = { ...draft, own: { ...draft.own, 7: [freshHours(c)] } };
  assert.equal(categoriesChanged(c, own), true);
});

test('a new container asks for an asset, a booking type, a period and what it uses', () => {
  const types = [{ id: 3, name: 'Hire' }];
  const d = newDraft(assets, types);
  assert.deepEqual(newBody(d), { asset: 1, booking_type: 3, period_type: 'flexible', uses: null });
  assert.equal(newReady(d), true);
  assert.equal(newReady(newDraft([], types)), false);
  assert.equal(newReady(newDraft(assets, [])), false);
});

test('a refusal says the models own words, a line each', () => {
  const reasons = [{ field: 'status', text: 'It covers the same time as container 001.' }, { field: '', text: 'Not covered.' }];
  assert.deepEqual(containerFailure({ kind: 'refused', code: 'refused', body: { reasons } }),
    ['It covers the same time as container 001.', 'Not covered.']);
  assert.deepEqual(containerFailure({ kind: 'refused', code: 'unknown', body: { reasons: [{ field: 'asset', text: 'There is no such asset.' }] } }),
    ['There is no such asset.']);
  assert.match(containerFailure({ kind: 'refused', code: 'in-use', body: { used: { bookings: 1, blocks: 2, prices: 0 } } })[0],
    /In use: 1 booking, 2 blocks\./);
  assert.match(containerFailure({ kind: 'refused', code: 'no-category' })[0], /needs a booking category/);
  assert.match(containerFailure({ kind: 'refused', code: 'unknown-category' })[0], /Reload/);
  assert.match(containerFailure({ kind: 'offline' })[0], /could not be reached/);
  assert.match(containerFailure({ kind: 'forbidden' })[0], /out of date/);
  assert.match(containerFailure({ kind: 'refused', code: 'busy' })[0], /Another change/);
  assert.equal(inUseWords({}), 'It is in use.');
  assert.equal(inUseWords({ prices: 3 }), 'In use: 3 prices.');
});
