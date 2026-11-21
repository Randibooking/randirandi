// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  addDays, availabilityFailure, barOf, clock, closureBody, closureChanged, closureOk, closureRange, confirmLabel, copyWeek, dayOfMonth,
  dayWords, dayNumber, editable, fixedHoliday, gridOf, holidayLabel, holidaysBody, inSupportedYears, joined, longDay, monthKey, monthTitle,
  moreThanListed, noChange, reasonWords, refundLines, removeClosureBody, removeWeekBody, savedWords, shiftMonth, spansText,
  tickedHolidays, unfinishedWords, byHandWords, owedWords, weekBody, weekChanged, weekOk, weekRuns, weekToCopy, weekdayOf,
} from './availability.js';

const FULL = [[0, 1440]];
const around = () => Array.from({ length: 7 }, () => FULL.map((s) => [...s]));

test('spans join where they meet or overlap and gaps stay closed', () => {
  assert.deepEqual(joined([[600, 700], [0, 600], [800, 900], [850, 1000]]), [[0, 700], [800, 1000]]);
  assert.equal(spansText([]), 'Closed');
  assert.equal(spansText([[0, 1440]]), '00:00–24:00');
  assert.equal(clock(615), '10:15');
});

test('a week is told as runs of weekdays with the same times', () => {
  const week = around();
  week[5] = [[600, 1200]];
  week[6] = [];
  assert.deepEqual(weekRuns(week), ['Mon–Fri 00:00–24:00', 'Sat 10:00–20:00', 'Sun Closed']);
  assert.deepEqual(weekRuns(around()), ['Mon–Sun 00:00–24:00']);
});

test('dates are counted in days without a time zone', () => {
  assert.equal(addDays('2027-02-28', 1), '2027-03-01');
  assert.equal(addDays('2027-03-01', -1), '2027-02-28');
  assert.equal(dayNumber('2027-03-02') - dayNumber('2027-03-01'), 1);
  assert.equal(weekdayOf('2027-03-01'), 0);
  assert.equal(weekdayOf('2027-03-07'), 6);
  assert.equal(dayOfMonth('2027-03-09'), 9);
  assert.equal(longDay('2027-03-01'), 'Monday 1 March 2027');
});

test('months step over the year and stay in the supported years', () => {
  assert.equal(shiftMonth('2027-12', 1), '2028-01');
  assert.equal(shiftMonth('2027-01', -1), '2026-12');
  assert.equal(monthKey('2027-03-14'), '2027-03');
  assert.equal(monthTitle('2027-03'), 'March 2027');
  assert.equal(inSupportedYears('1999-12'), false);
  assert.equal(inSupportedYears('2000-01'), true);
  assert.equal(inSupportedYears('2101-01'), false);
});

test('a month grid starts under the weekday of its first day', () => {
  const days = ['2027-03-01', '2027-03-02'].map((date) => ({ date }));
  assert.equal(gridOf(days).length, 2);
  const wed = [{ date: '2027-09-01' }];
  assert.deepEqual(gridOf(wed), [null, null, { date: '2027-09-01' }]);
  assert.deepEqual(gridOf([]), []);
});

test('a closed day is one red bar and the word Closure; a day without time is Closed', () => {
  const open = { date: 'x', closed: false, open: [[600, 900]] };
  const closed = { date: 'x', closed: true, open: [] };
  const none = { date: 'x', closed: false, open: [] };
  assert.deepEqual(barOf(open), { open: [[600, 900]], cut: [] });
  assert.deepEqual(barOf(closed), { open: [], cut: [[0, 1440]] });
  assert.equal(dayWords(open), null);
  assert.equal(dayWords(closed), 'Closure');
  assert.equal(dayWords(none), 'Closed');
});

test('a new week starts from the one in force on its date, else around the clock', () => {
  const weeks = [{ from: '2027-01-01', week: [[[600, 700]]] }, { from: '2027-06-01', week: [[[800, 900]]] }];
  assert.deepEqual(weekToCopy(weeks, '2027-03-01'), [[[600, 700]]]);
  assert.deepEqual(weekToCopy(weeks, '2027-07-01'), [[[800, 900]]]);
  assert.deepEqual(weekToCopy(weeks, '2026-01-01'), around());
  const copy = copyWeek(around());
  copy[0][0][1] = 60;
  assert.equal(around()[0][0][1], 1440);
});

test('a week is saved when it is seven valid days and differs from the row', () => {
  assert.equal(weekOk(around()), true);
  assert.equal(weekOk(around().slice(1)), false);
  const bad = around();
  bad[2] = [[700, 700]];
  assert.equal(weekOk(bad), false);
  bad[2] = [[0, 1500]];
  assert.equal(weekOk(bad), false);
  const row = { id: 4, from: '2027-05-01', week: around() };
  assert.equal(weekChanged(row, around(), '2027-05-01'), false);
  assert.equal(weekChanged(row, around(), '2027-05-02'), true);
  const narrower = around();
  narrower[0] = [[600, 900]];
  assert.equal(weekChanged(row, narrower, '2027-05-01'), true);
  // Spans that join are the same week.
  const split = around();
  split[0] = [[0, 700], [700, 1440]];
  assert.equal(weekChanged(row, split, '2027-05-01'), false);
  assert.equal(weekChanged(null, around(), '2027-05-01'), true);
});

test('the bodies a week sends', () => {
  const split = around();
  split[0] = [[0, 700], [700, 1440]];
  assert.deepEqual(weekBody(null, { from: '2027-05-01', week: split }), { kind: 'week-make', start_date: '2027-05-01', week: around() });
  assert.deepEqual(weekBody({ id: 4 }, { from: '2027-05-01', week: around() }),
    { kind: 'week-change', id: 4, start_date: '2027-05-01', week: around() });
  assert.deepEqual(removeWeekBody({ id: 4 }), { kind: 'week-remove', id: 4 });
  assert.equal(editable({ state: 'coming' }), true);
  assert.equal(editable({ state: 'in-force' }), false);
  assert.equal(editable({ state: 'past' }), false);
});

test('a closure needs both dates, the last not before the first and not over', () => {
  assert.equal(closureOk('2027-03-02', '2027-03-04', '2027-03-01'), true);
  assert.equal(closureOk('2027-03-02', '2027-03-01', '2027-03-01'), false);
  assert.equal(closureOk('2027-02-01', '2027-02-02', '2027-03-01'), false);
  assert.equal(closureOk('', '2027-03-04', '2027-03-01'), false);
  assert.equal(closureOk('2027-03-01', '2027-03-01', '2027-03-01'), true);
});

test('the bodies a closure sends and when it differs', () => {
  const draft = { first: '2027-03-02', last: '2027-03-03', reason: 'repair' };
  assert.deepEqual(closureBody(null, draft), { kind: 'closure-make', first_date: '2027-03-02', last_date: '2027-03-03', reason: 'repair' });
  assert.deepEqual(closureBody({ id: 9 }, draft), { kind: 'closure-change', id: 9, first_date: '2027-03-02', last_date: '2027-03-03', reason: 'repair' });
  assert.deepEqual(removeClosureBody({ id: 9 }), { kind: 'closure-remove', id: 9 });
  assert.equal(closureChanged({ first: '2027-03-02', last: '2027-03-03', reason: 'repair' }, draft), false);
  assert.equal(closureChanged({ first: '2027-03-02', last: '2027-03-03', reason: '' }, draft), true);
  assert.equal(closureChanged(null, draft), true);
  assert.equal(closureRange({ first: '2027-03-02', last: '2027-03-02' }), '2027-03-02');
  assert.equal(closureRange({ first: '2027-03-02', last: '2027-03-04' }), '2027-03-02 to 2027-03-04');
  assert.equal(reasonWords(''), 'None');
  assert.equal(reasonWords('holiday'), 'Holiday');
  assert.equal(reasonWords('vacation'), 'Vacation');
});

test('holidays over or closed are fixed; a Sunday is left out unless ticked', () => {
  const list = [
    { date: '2027-01-01', weekday: 4, over: true, closed: false },
    { date: '2027-03-25', weekday: 3, over: false, closed: false },
    { date: '2027-03-28', weekday: 6, over: false, closed: false },
    { date: '2027-03-29', weekday: 0, over: false, closed: true },
    { date: '2027-05-17', weekday: 0, over: false, closed: false },
  ];
  assert.equal(fixedHoliday(list[0]), true);
  assert.equal(fixedHoliday(list[3]), true);
  assert.equal(fixedHoliday(list[1]), false);
  assert.deepEqual([...tickedHolidays(list)], ['2027-03-25', '2027-05-17']);
  assert.deepEqual(holidaysBody(2027, new Set(['2027-03-28', '2027-01-01', '2027-03-29', '2027-05-17']), list),
    { kind: 'holidays', year: 2027, dates: ['2027-03-28', '2027-05-17'] });
  assert.equal(holidayLabel(1), 'Add 1 closure');
  assert.equal(holidayLabel(3), 'Add 3 closures');
});

const preview = {
  bookings: 3, members: 2, refund: [{ currency: 'NOK', amount: 25000 }, { currency: 'EUR', amount: 1000 }], token: 't',
  listed: [{ id: 1 }, { id: 2 }],
};

test('a preview that cancels nothing saves at once; one that cancels asks', () => {
  assert.equal(noChange({ bookings: 0 }), true);
  assert.equal(noChange(preview), false);
  assert.equal(noChange(null), true);
  assert.equal(moreThanListed(preview), 1);
  assert.equal(confirmLabel('save', preview), 'Save and cancel');
  assert.equal(confirmLabel('remove', preview), 'Remove and cancel');
  assert.equal(confirmLabel('remove', { bookings: 0 }), 'Remove');
  assert.deepEqual(refundLines(preview, (amount, currency) => `${amount} ${currency}`), ['25000 NOK', '1000 EUR']);
});

test('a save says how many it cancelled and how many are left to finish', () => {
  assert.equal(savedWords({ cancelled: 0, unfinished: [] }), 'Saved.');
  assert.equal(savedWords({ cancelled: 2, unfinished: [] }), 'Saved. 2 cancelled.');
  assert.equal(savedWords({ cancelled: 1, unfinished: [{ number: 5, code: 'busy' }] }), 'Saved. 1 cancelled, 1 to finish.');
  assert.equal(byHandWords({ reference: 'R-2', since: '2027-03-01 10:00' }), 'Booking R-2: refund by hand since 2027-03-01 10:00');
  assert.match(unfinishedWords({ code: 'recovery' }), /recovery/);
  assert.match(unfinishedWords({ code: 'other' }), /by hand/);
  assert.equal(owedWords({ reference: 'R-1', amount: 400, currency: 'NOK', since: '2027-03-01 10:00' }, (a, c) => `${a} ${c}`),
    'Booking R-1: 400 NOK since 2027-03-01 10:00');
});

test('a refusal says why in plain words', () => {
  const refused = (code, extra = {}) => ({ kind: 'refused', status: 409, code, body: { error: code, ...extra } });
  assert.match(availabilityFailure(refused('changed')), /changed since/);
  assert.match(availabilityFailure(refused('past')), /passed/);
  assert.match(availabilityFailure(refused('not-coming')), /not started/);
  assert.match(availabilityFailure(refused('date-taken')), /Another weekly/);
  assert.match(availabilityFailure(refused('ends-first')), /last date/);
  assert.match(availabilityFailure(refused('invalid-reason')), /reason/);
  assert.match(availabilityFailure(refused('invalid-span')), /span/);
  assert.match(availabilityFailure(refused('busy')), /Another change/);
  assert.match(availabilityFailure({ kind: 'offline' }), /could not be reached/);
  assert.match(availabilityFailure({ kind: 'forbidden', status: 403 }), /out of date/);
});
