// SPDX-License-Identifier: AGPL-3.0-only
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { accountRows, booksAs, bookingView, cancelMail, dayGroups, initialsOf, mineView, readMine, whoRows } from './mine.js';

const item = (extra = {}) => ({
  container: { id: 7, number: 3 }, spots: [], asset: 'Cabin', type: 'Overnight stay', category: 'Member', places: 1,
  start: '2026-11-03T15:00:00+01:00', end: '2026-11-04T11:00:00+01:00', total: 80000, state: 'confirmed',
  order: { id: '5d7f0c3e-0000-4000-8000-000000000001', reference: 'K7Q4MD' }, ...extra,
});

test('bookingView: type, date, time, category, asset, price and order', () => {
  const view = bookingView(item(), 'NOK');
  assert.equal(view.type, 'Overnight stay');
  assert.equal(view.date, 'Tuesday 3 November');
  assert.equal(view.time, '15:00–11:00 next day');
  assert.equal(view.as, 'Member');
  assert.equal(view.asset, 'Cabin');
  assert.equal(view.order, 'K7Q4MD');
  assert.equal(view.orderId, '5d7f0c3e-0000-4000-8000-000000000001');
  assert.equal(view.cancelled, null);
  assert.match(view.price, /800/);
  assert.equal(view.night, true);
  assert.equal(bookingView(item({ end: '2026-11-03T18:00:00+01:00' }), 'NOK').night, false);
});

test('bookingView: a cancelled booking says so; places only when more than one', () => {
  assert.equal(bookingView(item({ state: 'cancelled' }), 'NOK').cancelled, 'Cancelled');
  assert.equal(bookingView(item({ places: 3, spot_word: 'bed' }), 'NOK').places, '3 × bed');
  assert.equal(bookingView(item({ places: 1 }), 'NOK').places, null);
});

test('bookingView: no price is No charge; a line is never marked', () => {
  const view = bookingView(item({ total: 0, state: 'cancelled' }), 'NOK');
  assert.equal(view.price, 'No charge');
  assert.equal(view.marked, null);
});

test('mineView: keeps the server\'s order in both lists', () => {
  const a = item({ start: '2026-11-01T10:00:00+01:00' });
  const b = item({ start: '2026-11-02T10:00:00+01:00' });
  const view = mineView({ coming: [a, b], earlier: [b, a] }, 'NOK');
  assert.deepEqual(view.coming.map((l) => l.date), ['Sunday 1 November', 'Monday 2 November']);
  assert.deepEqual(view.earlier.map((l) => l.date), ['Monday 2 November', 'Sunday 1 November']);
  assert.equal(view.nothingComing, false);
  assert.equal(new Set([...view.coming, ...view.earlier].map((l) => l.key)).size, 4);
});

test('mineView: empty lists, earlier only, and a missing answer: nothing coming', () => {
  assert.deepEqual(mineView({ coming: [], earlier: [] }, 'NOK'), { coming: [], earlier: [], nothingComing: true, comingDays: [], earlierDays: [] });
  const only = mineView({ coming: [], earlier: [item()] }, 'NOK');
  assert.equal(only.nothingComing, true);
  assert.equal(only.earlier.length, 1);
  assert.equal(mineView({ coming: [item()], earlier: [] }, 'NOK').nothingComing, false);
  assert.equal(mineView(null, 'NOK').nothingComing, true);
});

test('mineView: two bookings of one container at one start in one order get their own keys', () => {
  const view = mineView({ coming: [item(), item({ category: 'Work' })], earlier: [item()] }, 'NOK');
  assert.equal(new Set([...view.coming, ...view.earlier].map((l) => l.key)).size, 3);
});

test('readMine: the answer, or the kind of failure', () => {
  assert.deepEqual(readMine({ ok: true, data: { coming: [], earlier: [] } }), { kind: 'ready', data: { coming: [], earlier: [] } });
  assert.deepEqual(readMine({ kind: 'signed-out' }), { kind: 'signed-out' });
  assert.deepEqual(readMine({ kind: 'offline' }), { kind: 'offline' });
  assert.deepEqual(readMine({ kind: 'refused', status: 500 }), { kind: 'refused' });
});

test('initialsOf: two letters at most', () => {
  assert.equal(initialsOf('Alex Rivers'), 'AR');
  assert.equal(initialsOf('  sam  '), 'S');
  assert.equal(initialsOf('Mary Jane Watson'), 'MJ');
  assert.equal(initialsOf(''), '');
  assert.equal(initialsOf(undefined), '');
});

test('booksAs: the categories the member\'s role has, in the organization\'s order', () => {
  assert.equal(booksAs({ categories: [{ id: 1, name: 'Private' }, { id: 2, name: 'Work' }] }), 'Books as Private, Work');
  assert.equal(booksAs({ categories: [{ id: 1, name: 'Private' }] }), 'Books as Private');
  assert.equal(booksAs({ categories: ['Private', 'Work', 'Guest'].map((name, id) => ({ id, name })) }), 'Books as Private, Work, Guest');
  assert.equal(booksAs({ categories: [] }), null);
  assert.equal(booksAs(null), null);
});

test('bookingView: the price is in the booking\'s own currency, else the one given', () => {
  assert.match(bookingView(item({ total: 12550, currency: 'EUR' }), 'NOK').price, /€$/);
  assert.match(bookingView(item({ total: 12550 }), 'NOK').price, /kr$/);
  assert.match(mineView({ coming: [item({ total: 12550, currency: 'DKK' })], earlier: [] }).coming[0].price, /DKK$/);
});

test('dayGroups: a date once, its lines in time order', () => {
  const lines = mineView({ coming: [
    item({ start: '2026-10-21T20:00:00+02:00', end: '2026-10-21T22:00:00+02:00' }),
    item({ start: '2026-10-21T08:00:00+02:00', end: '2026-10-21T10:00:00+02:00' }),
    item({ start: '2026-10-22T08:00:00+02:00', end: '2026-10-22T10:00:00+02:00' }),
  ] }, 'NOK').comingDays;
  assert.equal(lines.length, 2);
  assert.equal(lines[0].date, 'Wednesday 21 October');
  assert.deepEqual(lines[0].lines.map((one) => one.time), ['08:00–10:00', '20:00–22:00']);
  assert.equal(lines[1].lines.length, 1);
  assert.deepEqual(dayGroups([]), []);
});

test('cancelMail: Randi\'s subject and body in a mailto: address', () => {
  const line = mineView({ coming: [item({ type: 'Hall hire', asset: 'Hall',
    start: '2026-10-21T20:00:00+02:00', end: '2026-10-21T22:00:00+02:00' })] }, 'NOK').coming[0];
  const href = cancelMail(line, 'office@example.com', 'Alex Rivers');
  const [head, query] = href.split('?');
  assert.equal(head, 'mailto:office@example.com');
  const params = new URLSearchParams(query);
  assert.equal(params.get('subject'), 'Please cancel my booking K7Q4MD');
  assert.equal(params.get('body'),
    'Hi! Could you cancel my booking K7Q4MD: Hall hire, Hall, Wednesday 21 October, 20:00–22:00? Thank you! Alex Rivers');
  assert.ok(!query.includes(' '));
});

test('cancelMail: an address with ? & or # stays the address', () => {
  const line = mineView({ coming: [item()] }, 'NOK').coming[0];
  const href = cancelMail(line, 'a?b&c#d%e@example.com', 'Alex');
  assert.ok(href.startsWith('mailto:a%3Fb%26c%23d%25e@example.com?subject='));
  assert.equal(decodeURIComponent(href.slice(7, href.indexOf('?'))), 'a?b&c#d%e@example.com');
});

test('cancelMail: none without a contact address or for a cancelled booking', () => {
  const line = mineView({ coming: [item()] }, 'NOK').coming[0];
  assert.equal(cancelMail(line, '', 'Alex'), null);
  assert.equal(cancelMail(line, undefined, 'Alex'), null);
  const cancelled = mineView({ coming: [item({ state: 'cancelled' })] }, 'NOK').coming[0];
  assert.equal(cancelMail(cancelled, 'office@example.com', 'Alex'), null);
});

test('whoRows: the member\'s own data; a missing value leaves its row out', () => {
  const rows = whoRows({ member_role: 'Standard', member_email: 'alex@example.com', member_phone: '',
    member_memberships: [{ identifier: 'H0101', address: 'Fjordgata 1, 0001 Oslo' }, { identifier: 'H0102', address: '' }] });
  assert.deepEqual(rows.map((row) => [row.label, row.values]), [
    ['Membership', ['H0101', 'H0102']], ['Email', ['alex@example.com']],
    ['Address', ['Fjordgata 1, 0001 Oslo']], ['Role', ['Standard']],
  ]);
  assert.deepEqual(whoRows({}), []);
});

test('accountRows: Admin only for an administrator, then Sign out', () => {
  assert.deepEqual(accountRows(true), [{ key: 'admin', href: '/admin/' }, { key: 'sign-out' }]);
  assert.deepEqual(accountRows(false), [{ key: 'sign-out' }]);
  assert.deepEqual(accountRows(undefined), [{ key: 'sign-out' }]);
});

test('bookingView: the booking conditions its order accepted are linked; an older order says none was recorded', async () => {
  const { conditionsHref } = await import('./mine.js');
  const id = '5d7f0c3e-0000-4000-8000-000000000001';
  assert.equal(conditionsHref(id), `/book/order/${id}/conditions/`);
  const recorded = bookingView(item({ order: { id, reference: 'K7Q4MD', conditions: true } }), 'NOK');
  assert.equal(recorded.conditionsHref, `/book/order/${id}/conditions/`);
  assert.equal(bookingView(item({ order: { id, reference: 'K7Q4MD', conditions: false } }), 'NOK').conditionsHref, null);
  assert.equal(bookingView(item(), 'NOK').conditionsHref, null);
});
