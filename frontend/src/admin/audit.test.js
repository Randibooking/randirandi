// SPDX-License-Identifier: AGPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { addDays, auditQuery, todayIn } from './audit.js';

const open = { when: 'all', from: '2026-03-01', to: '2026-03-07', by: 'all', action: 'all', q: '', dir: 'desc', page: 0 };

test('no filter asks for the newest first, page 1', () => {
  assert.deepEqual(auditQuery(open), {
    by: undefined, action: undefined, q: undefined, order: 'newest', page: 1,
  });
});

test('one day sends that day as both ends', () => {
  const query = auditQuery({ ...open, when: 'day' });
  assert.equal(query.from, '2026-03-01');
  assert.equal(query.to, '2026-03-01');
});

test('a period sends both dates, whichever way round', () => {
  const forward = auditQuery({ ...open, when: 'period' });
  assert.deepEqual([forward.from, forward.to], ['2026-03-01', '2026-03-07']);
  const back = auditQuery({ ...open, when: 'period', from: '2026-03-09', to: '2026-03-02' });
  assert.deepEqual([back.from, back.to], ['2026-03-02', '2026-03-09']);
});

test('administrator, action and search are sent only when chosen', () => {
  const query = auditQuery({ ...open, by: '7', action: 'exported member', q: '  Alex ', dir: 'asc', page: 2 });
  assert.deepEqual(query, {
    by: '7', action: 'exported member', q: 'Alex', order: 'oldest', page: 3,
  });
  assert.equal(auditQuery({ ...open, q: '   ' }).q, undefined);
});

test('dates in a time zone and calendar days', () => {
  const late = new Date('2026-01-15T23:30:00Z');
  assert.equal(todayIn('Europe/Oslo', late), '2026-01-16');
  assert.equal(todayIn('America/New_York', late), '2026-01-15');
  assert.equal(addDays('2026-03-01', -6), '2026-02-23');
  assert.equal(addDays('2026-12-31', 1), '2027-01-01');
  assert.equal(addDays('2028-02-28', 1), '2028-02-29');
});
