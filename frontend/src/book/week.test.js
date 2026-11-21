// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  canGoBackWeek, chipState, chosenDay, mondayOf, windowStart, usableDay, weekAsks, weekDays, weekTitle, weekToAsk,
} from './week.js';
import { clampMonth } from './month.js';

test('the week runs seven days from its first day, whatever weekday', () => {
  assert.deepEqual(weekDays('2026-10-21'), [
    '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24', '2026-10-25', '2026-10-26', '2026-10-27']);
  assert.deepEqual(weekDays('2026-10-26'), [
    '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30', '2026-10-31', '2026-11-01']);
  assert.equal(weekDays('2026-12-28')[6], '2027-01-03');
});

test('the title: one month, across months, across the year', () => {
  assert.equal(weekTitle('2026-10-19'), '19–25 October');
  assert.equal(weekTitle('2026-10-26'), '26 Oct – 1 Nov');
  assert.equal(weekTitle('2026-10-21'), '21–27 October');
  assert.equal(weekTitle('2026-12-28'), '28 Dec – 3 Jan');
});

test('no week before the one that holds today', () => {
  assert.equal(canGoBackWeek('2026-10-19', '2026-10-21'), false);
  assert.equal(canGoBackWeek('2026-10-19', '2026-10-25'), false);
  assert.equal(canGoBackWeek('2026-10-26', '2026-10-21'), true);
  assert.equal(canGoBackWeek('2026-10-26', '2026-10-26'), false);
});

test('the Monday of a week, across a month and a year', () => {
  assert.equal(mondayOf('2026-10-22'), '2026-10-19');
  assert.equal(mondayOf('2026-10-19'), '2026-10-19');
  assert.equal(mondayOf('2026-10-25'), '2026-10-19');
  assert.equal(mondayOf('2026-11-01'), '2026-10-26');
  assert.equal(mondayOf('2027-01-03'), '2026-12-28');
});

test('the chosen day: the picked one in the week, else today, else the Monday', () => {
  assert.equal(chosenDay('2026-10-19', '2026-10-23', '2026-10-21'), '2026-10-23');
  assert.equal(chosenDay('2026-10-19', '2026-11-02', '2026-10-21'), '2026-10-21');
  assert.equal(chosenDay('2026-10-26', '2026-10-23', '2026-10-21'), '2026-10-26');
  assert.equal(chosenDay('2026-10-26', null, '2026-10-21'), '2026-10-26');
});

test('the answers a week needs: one whole month, or two', () => {
  assert.deepEqual(weekAsks('2026-10-19'), [{ month: '2026-10', start: '2026-10-01', days: 31 }]);
  assert.deepEqual(weekAsks('2026-10-26'), [
    { month: '2026-10', start: '2026-10-01', days: 31 },
    { month: '2026-11', start: '2026-11-01', days: 30 },
  ]);
  assert.deepEqual(weekAsks('2026-12-28').map((ask) => ask.start), ['2026-12-01', '2027-01-01']);
});

const day = (state, share = 0.5, mine = false) => ({ summary: { state, share, mine } });

test('chip state: open shows its bar, full and closed and past show none', () => {
  assert.deepEqual(chipState(day('open', 0.4), '2026-10-22', '2026-10-21'),
    { reach: true, open: true, full: false, mine: false, share: 0.4, dim: false });
  assert.deepEqual(chipState(day('full', 0.2), '2026-10-22', '2026-10-21'),
    { reach: true, open: true, full: true, mine: false, share: 0, dim: false });
  assert.equal(chipState(day('closed'), '2026-10-22', '2026-10-21').dim, true);
  const past = chipState(day('past'), '2026-10-20', '2026-10-21');
  assert.equal(past.dim, true);
  assert.equal(past.reach, false);
});

test('chip state: a day of yours is never dimmed; no answer yet is dimmed', () => {
  assert.equal(chipState(day('closed', 0, true), '2026-10-22', '2026-10-21').dim, false);
  assert.deepEqual(chipState(null, '2026-10-22', '2026-10-21'),
    { reach: true, open: false, full: false, mine: false, share: 0, dim: true });
});

test('never before today: week, month, and a past chosen day', () => {
  assert.equal(windowStart('2026-10-12', '2026-10-21'), '2026-10-19');
  assert.equal(windowStart('2026-10-26', '2026-10-21'), '2026-10-26');
  assert.equal(clampMonth('2026-09', '2026-10-21'), '2026-10');
  assert.equal(clampMonth('2026-11', '2026-10-21'), '2026-11');
  assert.equal(usableDay('2026-10-20', '2026-10-21'), null);
  assert.equal(usableDay('2026-10-21', '2026-10-21'), '2026-10-21');
  assert.equal(usableDay(null, '2026-10-21'), null);
});

test('months to ask: the held ones are skipped unless forced', () => {
  const asks = weekAsks('2026-10-26');
  const has = (ask) => ask.month === '2026-10';
  assert.deepEqual(weekToAsk(asks, has, false).map((ask) => ask.month), ['2026-11']);
  assert.equal(weekToAsk(asks, has, true).length, 2);
  assert.deepEqual(weekToAsk(asks, () => true, false), []);
});

test('weeks start on Monday, never on another day', () => {
  const today = '2026-10-22';
  assert.equal(windowStart('2026-10-22', today), '2026-10-19');
  assert.equal(windowStart('2026-10-25', today), '2026-10-19');
  assert.equal(windowStart('2026-10-26', today), '2026-10-26');
  assert.equal(windowStart('2026-11-01', today), '2026-10-26');
  assert.equal(windowStart('2026-11-04', today), '2026-11-02');
});

test('a chosen future day opens the Monday-to-Sunday week that holds it, and stays selected', () => {
  const today = '2026-10-22';
  const start = windowStart('2026-11-06', today);
  assert.equal(start, '2026-11-02');
  assert.deepEqual(weekDays(start).map((date) => date.slice(8)), ['02', '03', '04', '05', '06', '07', '08']);
  assert.equal(chosenDay(start, '2026-11-06', today), '2026-11-06');
  assert.equal(chosenDay(start, null, today), '2026-11-02');
});

test('the current week opens with today chosen; the days before it are past', () => {
  const today = '2026-10-22';
  const start = windowStart(today, today);
  assert.equal(start, '2026-10-19');
  assert.equal(chosenDay(start, null, today), today);
  const past = chipState({ summary: { state: 'past', share: 0, mine: false } }, '2026-10-20', today);
  assert.deepEqual(past, { reach: false, open: false, full: false, mine: false, share: 0, dim: true });
});

test('the week across the clock change of 25 October 2026 keeps seven dates', () => {
  assert.deepEqual(weekDays('2026-10-19'), [
    '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24', '2026-10-25']);
  assert.equal(windowStart('2026-10-28', '2026-10-21'), '2026-10-26');
  assert.equal(windowStart('2026-10-25', '2026-10-21'), '2026-10-19');
  assert.deepEqual(weekAsks('2026-10-19'), [{ month: '2026-10', start: '2026-10-01', days: 31 }]);
});

test('a week across New Year asks December and January', () => {
  assert.deepEqual(weekAsks('2026-12-28'), [
    { month: '2026-12', start: '2026-12-01', days: 31 },
    { month: '2027-01', start: '2027-01-01', days: 31 },
  ]);
  assert.equal(weekTitle('2026-12-28'), '28 Dec – 3 Jan');
  assert.equal(windowStart('2027-01-02', '2026-12-30'), '2026-12-28');
});
