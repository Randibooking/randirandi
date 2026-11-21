// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  canGoBack, dateStruck, dayOn, joinAnswers, monthLength, monthOf, monthStart, monthWeeks, readDays, shiftMonth, weekdayOf,
} from './month.js';

test('months: start, length, shifts across the year', () => {
  assert.equal(monthOf('2026-10-20'), '2026-10');
  assert.equal(monthStart('2026-10'), '2026-10-01');
  assert.equal(monthLength('2026-10'), 31);
  assert.equal(monthLength('2026-11'), 30);
  assert.equal(monthLength('2028-02'), 29);
  assert.equal(monthLength('2027-02'), 28);
  assert.equal(shiftMonth('2026-12', 1), '2027-01');
  assert.equal(shiftMonth('2026-01', -1), '2025-12');
  assert.equal(shiftMonth('2026-10', 0), '2026-10');
});

test('weekdays count from Monday', () => {
  assert.equal(weekdayOf('2026-10-19'), 0);
  assert.equal(weekdayOf('2026-10-25'), 6);
});

test('the grid: weeks from Monday, days outside the month are null', () => {
  const weeks = monthWeeks('2026-10');
  assert.equal(weeks.length, 5);
  assert.ok(weeks.every((week) => week.length === 7));
  assert.deepEqual(weeks[0], [null, null, null, '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04']);
  assert.deepEqual(weeks[4].slice(0, 5), ['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30']);
  assert.deepEqual(weeks[4].slice(5), ['2026-10-31', null]);
  assert.equal(monthWeeks('2026-10').flat().filter(Boolean).length, 31);
  // A month that starts on a Monday and ends on a Sunday has no blanks.
  assert.deepEqual(monthWeeks('2027-02').flat().filter((day) => day === null), []);
  assert.equal(monthWeeks('2027-02').length, 4);
});

test('no month before the current one', () => {
  assert.equal(canGoBack('2026-10', '2026-10-20'), false);
  assert.equal(canGoBack('2026-11', '2026-10-20'), true);
});

const period = (day, hour) => ({ day, rows: [`${day}T${hour}:00:00+02:00`] });
const answer = (containers) => ({ worked_out_at: '2026-10-19T08:00:00+02:00', containers });

test('answers join: a container once, a date period once', () => {
  const first = answer([{ id: 1, number: 1, periods: [period('2026-10-01', '08'), period('2026-10-02', '08')] }]);
  const second = answer([
    { id: 1, number: 1, periods: [period('2026-10-02', '08'), period('2026-10-03', '08')] },
    { id: 0, number: 0, periods: [period('2026-10-03', '10')] },
  ]);
  const joined = joinAnswers(first, second);
  assert.deepEqual(joined.containers.map((row) => row.id), [0, 1]);
  assert.deepEqual(joined.containers[1].periods.map((row) => row.day), ['2026-10-01', '2026-10-02', '2026-10-03']);
  assert.equal(first.containers[0].periods.length, 2);
  // Each half's other categories, by date.
  const one = { ...first, other_categories: [{ day: '2026-10-01', categories: [2] }] };
  const two = { ...second, other_categories: [{ day: '2026-10-03', categories: [2] }] };
  assert.deepEqual(joinAnswers(one, two).other_categories.map((row) => row.day), ['2026-10-01', '2026-10-03']);
  assert.deepEqual(joined.other_categories, []);
});

test('readDays asks for fewer days while the server says fewer-days', async () => {
  const asked = [];
  const read = async ({ start, days, asset }) => {
    asked.push([start, days]);
    if (days > 8) return { kind: 'refused', status: 422, code: 'fewer-days' };
    return { ok: true, data: answer([{ id: 1, number: 1, asset, periods: [period(start, '08')] }]) };
  };
  const result = await readDays(read, { asset: 3 }, '2026-10-01', 31);
  assert.equal(result.ok, true);
  assert.deepEqual(asked.slice(0, 3), [['2026-10-01', 31], ['2026-10-01', 16], ['2026-10-17', 15]]);
  assert.deepEqual(result.data.containers[0].periods.map((row) => row.day).sort(),
    ['2026-10-01', '2026-10-09', '2026-10-17', '2026-10-25']);
});

test('readDays gives up on one day, and passes other refusals on', async () => {
  const always = async () => ({ kind: 'refused', status: 422, code: 'fewer-days' });
  assert.equal((await readDays(always, {}, '2026-10-01', 1)).code, 'fewer-days');
  const offline = async () => ({ kind: 'offline' });
  assert.equal((await readDays(offline, {}, '2026-10-01', 31)).kind, 'offline');
});

test('readDays asks once when the server answers', async () => {
  let calls = 0;
  const read = async () => { calls += 1; return { ok: true, data: answer([]) }; };
  assert.equal((await readDays(read, {}, '2026-10-01', 31)).ok, true);
  assert.equal(calls, 1);
});

test('a day of another month is no day: the grid draws nothing from an old answer', () => {
  const built = { today: '2026-10-19', dates: [{ date: '2026-10-31' }, { date: '2026-10-30' }] };
  assert.equal(dayOn(built, '2026-10-31').date, '2026-10-31');
  assert.equal(dayOn(built, '2026-11-01'), null);
  assert.equal(dayOn(null, '2026-11-01'), null);
});

test('a full day is struck only when nothing of the member\'s is on it', () => {
  assert.equal(dateStruck({ full: true, mine: false }), true);
  assert.equal(dateStruck({ full: true, mine: true }), false);
  assert.equal(dateStruck({ full: true, mine: false, cart: true }), false);
  assert.equal(dateStruck({ full: true, mine: true, cart: true }), false);
  assert.equal(dateStruck({ full: false, mine: false }), false);
  assert.equal(dateStruck({ full: false, mine: true }), false);
});

import { todayTarget } from './month.js';
import { windowStart } from './week.js';

test('the Today button goes to today\'s month, today\'s week and chooses today, in every view', () => {
  const target = todayTarget('2026-10-25');
  assert.deepEqual(target, { month: '2026-10', from: '2026-10-25', chosen: '2026-10-25' });
  // The Week opens on the Monday of today's week, from wherever it was.
  assert.equal(windowStart(target.from, '2026-10-25'), '2026-10-19');
  assert.equal(todayTarget('2026-11-01').month, '2026-11');
});
